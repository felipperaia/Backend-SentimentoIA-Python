import logging
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.database import get_db
from app.services.collector_service import CollectorService
from app.services.enrichment_service import EnrichmentService
from app.services.llm_service import LLMService
from app.services.normalization_service import make_search_id, utcnow

logger = logging.getLogger(__name__)


class SearchService:
    """Pipeline principal do produto.

    Fluxo:
    1. Coleta dados reais.
    2. Normaliza.
    3. Enriquece cada menção.
    4. Salva no MongoDB por search_id.
    5. Chama LLM para resumo/decisão.
    6. Gera alertas.
    """

    @staticmethod
    async def run_search(
        *,
        user_id: str,
        query: str,
        sources: list[str],
        period_days: int = 30,
        locality: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        db = get_db()
        now = utcnow()

        # Cache inteligente: se busca igual existir dentro do TTL, retorna o resultado salvo.
        cache_key = {
            "user_id": user_id,
            "query": query.strip().lower(),
            "sources": sorted([s.lower() for s in sources]),
            "period_days": int(period_days),
            "locality": locality or "",
        }

        if use_cache:
            ttl_start = now - timedelta(minutes=settings.CACHE_TTL_MINUTES)
            cached = db.search_jobs.find_one(
                {
                    **cache_key,
                    "created_at": {"$gte": ttl_start},
                    "status": "completed",
                },
                sort=[("created_at", -1)],
            )
            if cached:
                mentions = list(db.mentions.find({"search_id": cached["search_id"]}, {"raw": 0}).sort("published_at", -1))
                return {
                    "search_id": cached["search_id"],
                    "query": query,
                    "cached": True,
                    "total": len(mentions),
                    "mentions": SearchService.serialize_many(mentions),
                    "metrics": cached.get("metrics", {}),
                    "llm_analysis": cached.get("llm_analysis", {}),
                    "errors": cached.get("errors", []),
                }

        search_id = make_search_id()

        db.search_jobs.insert_one({
            "search_id": search_id,
            **cache_key,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "errors": [],
        })

        collected, errors = await CollectorService.collect(query, sources, period_days=period_days, locality=locality)

        enriched_mentions: list[dict[str, Any]] = []
        seen = set()
        cutoff = now - timedelta(days=max(1, int(period_days)))

        for mention in collected:
            published_at = mention.get("published_at")
            if isinstance(published_at, datetime):
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=cutoff.tzinfo)
                    mention["published_at"] = published_at
                if published_at < cutoff:
                    continue

            # Deduplicação simples por fonte + texto + autor.
            fingerprint = (
                mention.get("source"),
                (mention.get("author") or "").lower(),
                (mention.get("text") or "")[:160].lower(),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            enrichment = EnrichmentService.analyze_mention(mention["text"], mention.get("rating"))
            mention.update(enrichment)
            mention.update({
                "search_id": search_id,
                "user_id": user_id,
                "query": query,
                "created_at": utcnow(),
            })
            enriched_mentions.append(mention)

        if enriched_mentions:
            db.mentions.insert_many(enriched_mentions)

        metrics = EnrichmentService.aggregate(enriched_mentions)
        llm_analysis = await LLMService.analyze_mentions(query, enriched_mentions)

        alerts = SearchService.generate_alerts(user_id, search_id, query, enriched_mentions, metrics, llm_analysis)
        if alerts:
            db.alerts.insert_many(alerts)

        db.search_jobs.update_one(
            {"search_id": search_id},
            {
                "$set": {
                    "status": "completed",
                    "updated_at": utcnow(),
                    "total": len(enriched_mentions),
                    "metrics": metrics,
                    "llm_analysis": llm_analysis,
                    "errors": errors,
                }
            },
        )

        return {
            "search_id": search_id,
            "query": query,
            "cached": False,
            "total": len(enriched_mentions),
            "mentions": SearchService.serialize_many(enriched_mentions),
            "metrics": metrics,
            "llm_analysis": llm_analysis,
            "alerts": SearchService.serialize_many(alerts),
            "errors": errors,
        }

    @staticmethod
    def generate_alerts(
        user_id: str,
        search_id: str,
        query: str,
        mentions: list[dict[str, Any]],
        metrics: dict[str, Any],
        llm_analysis: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Gera alertas internos a partir de criticidade e score."""
        alerts: list[dict[str, Any]] = []
        now = utcnow()

        critical_mentions = [m for m in mentions if m.get("criticality") == "alta"]

        if critical_mentions:
            alerts.append({
                "user_id": user_id,
                "search_id": search_id,
                "query": query,
                "type": "critical_mentions",
                "severity": "alta",
                "title": f"{len(critical_mentions)} menções críticas detectadas",
                "message": "Existem menções com risco reputacional alto. Priorize resposta operacional.",
                "is_read": False,
                "created_at": now,
            })

        if metrics.get("reputation_score", 100) < 45 and mentions:
            alerts.append({
                "user_id": user_id,
                "search_id": search_id,
                "query": query,
                "type": "low_reputation_score",
                "severity": "alta",
                "title": "Score de reputação baixo",
                "message": f"Score atual: {metrics.get('reputation_score')}. Revise riscos e recomendações.",
                "is_read": False,
                "created_at": now,
            })

        if llm_analysis.get("risks"):
            alerts.append({
                "user_id": user_id,
                "search_id": search_id,
                "query": query,
                "type": "llm_risk",
                "severity": "media",
                "title": "Riscos estratégicos identificados pela IA",
                "message": "; ".join(llm_analysis.get("risks", [])[:3]),
                "is_read": False,
                "created_at": now,
            })

        return alerts

    @staticmethod
    def dashboard(user_id: str, search_id: str | None = None) -> dict[str, Any]:
        """Retorna dashboard usando search_id para não misturar buscas."""
        db = get_db()

        if not search_id:
            last = db.search_jobs.find_one({"user_id": user_id, "status": "completed"}, sort=[("created_at", -1)])
            if not last:
                return {"search_id": None, "metrics": {}, "mentions": [], "alerts": [], "llm_analysis": {}}
            search_id = last["search_id"]

        job = db.search_jobs.find_one({"user_id": user_id, "search_id": search_id}) or {}
        mentions = list(db.mentions.find({"user_id": user_id, "search_id": search_id}, {"raw": 0}).sort("published_at", -1))
        alerts = list(db.alerts.find({"user_id": user_id, "search_id": search_id}).sort("created_at", -1))

        return {
            "search_id": search_id,
            "query": job.get("query"),
            "metrics": job.get("metrics") or EnrichmentService.aggregate(mentions),
            "llm_analysis": job.get("llm_analysis", {}),
            "mentions": SearchService.serialize_many(mentions),
            "alerts": SearchService.serialize_many(alerts),
            "errors": job.get("errors", []),
        }

    @staticmethod
    def history(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        db = get_db()
        jobs = list(db.search_jobs.find({"user_id": user_id}).sort("created_at", -1).limit(limit))
        return SearchService.serialize_many(jobs)

    @staticmethod
    def serialize_many(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [SearchService.serialize(i) for i in items]

    @staticmethod
    def serialize(item: dict[str, Any]) -> dict[str, Any]:
        output = {}
        for k, v in item.items():
            if k == "_id":
                output["id"] = str(v)
            elif hasattr(v, "isoformat"):
                output[k] = v.isoformat()
            else:
                output[k] = v
        return output
