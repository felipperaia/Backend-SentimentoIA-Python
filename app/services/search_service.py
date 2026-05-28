import logging
from datetime import datetime
from typing import Any

from app.database import get_db
from app.services.enrichment_service import EnrichmentService
from app.services.normalization_service import canonicalize_url, utcnow

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
    def _normalize_error_message(message: Any) -> str:
        text = str(message or "").strip()
        lowered = text.lower()
        if not text:
            return "Falha temporaria no processamento"

        # Nunca expor detalhes internos de stack/upstream/model/gateway.
        blocked_markers = [
            "traceback",
            "exception",
            "stack",
            "http://",
            "https://",
            "gateway",
            "upstream",
            "ollama",
            "model",
        ]
        if any(marker in lowered for marker in blocked_markers):
            return "Falha temporaria no processamento"

        if "timeout" in lowered or "timed out" in lowered:
            return "Tempo limite excedido no processamento"
        if "429" in lowered or "rate limit" in lowered or "limite" in lowered:
            return "Limite temporario do processamento atingido"

        return text[:220]

    @staticmethod
    def _sanitize_error_entry(entry: Any) -> dict[str, Any]:
        if isinstance(entry, dict):
            source = str(entry.get("source") or "unknown").strip().lower() or "unknown"
            message = SearchService._normalize_error_message(entry.get("error") or entry.get("detail") or "")
            reason = str(entry.get("reason") or "").strip().lower()
            timeout = bool(entry.get("timeout", False)) or reason == "timeout"

            if timeout:
                message = "Tempo limite excedido no processamento"
            if not reason:
                if timeout:
                    reason = "timeout"
                elif "limite" in message.lower():
                    reason = "rate_limited"
                else:
                    reason = "temporary_failure"

            return {
                "source": source,
                "error": message,
                "reason": reason,
                "timeout": timeout,
            }

        return {
            "source": "unknown",
            "error": SearchService._normalize_error_message(entry),
            "reason": "temporary_failure",
            "timeout": False,
        }

    @staticmethod
    def _sanitize_errors(errors: Any) -> list[dict[str, Any]]:
        if not isinstance(errors, list):
            return []
        return [SearchService._sanitize_error_entry(item) for item in errors]

    @staticmethod
    def _build_status_summary(
        *,
        requested_sources: list[str],
        mentions: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_requested: list[str] = []
        for source in requested_sources:
            normalized = str(source or "").strip().lower()
            if normalized and normalized not in normalized_requested:
                normalized_requested.append(normalized)

        mention_counts: dict[str, int] = {}
        for mention in mentions:
            source = str(mention.get("source") or "").strip().lower()
            if not source:
                continue
            mention_counts[source] = mention_counts.get(source, 0) + 1

        errors_by_source: dict[str, list[dict[str, Any]]] = {}
        timeout_sources: set[str] = set()
        unmapped_error_count = 0
        for error in errors:
            source = str(error.get("source") or "unknown").strip().lower() or "unknown"
            if source in {"unknown", "system"}:
                unmapped_error_count += 1
                continue

            errors_by_source.setdefault(source, []).append(error)
            if bool(error.get("timeout", False)) or str(error.get("reason") or "") == "timeout":
                timeout_sources.add(source)

        if not normalized_requested:
            normalized_requested = sorted(set(mention_counts.keys()) | set(errors_by_source.keys()))

        source_status: list[dict[str, Any]] = []
        for source in normalized_requested:
            source_errors = errors_by_source.get(source, [])
            source_mentions = int(mention_counts.get(source, 0))
            first_error = source_errors[0] if source_errors else {}
            source_status.append(
                {
                    "source": source,
                    "ok": source_mentions > 0,
                    "count": source_mentions,
                    "error": first_error.get("error") if first_error else None,
                    "reason": first_error.get("reason") if first_error else None,
                    "timeout": any(bool(item.get("timeout", False)) for item in source_errors),
                }
            )

        sources_with_data = sum(1 for item in source_status if int(item.get("count", 0)) > 0)
        sources_failed = sum(1 for item in source_status if item.get("error")) + unmapped_error_count
        partial_success = sources_with_data > 0 and sources_failed > 0

        if partial_success:
            status = "partial_success"
            message = "Importacao concluida com falhas parciais em algumas fontes."
        elif sources_with_data == 0 and sources_failed > 0:
            status = "failed"
            message = "Importacao sem resultados devido a falhas nas fontes selecionadas."
        elif sources_with_data == 0:
            status = "empty"
            message = "Importacao concluida sem novos resultados."
        else:
            status = "success"
            message = "Importacao concluida com sucesso."

        if timeout_sources and status in {"partial_success", "failed"}:
            message = f"{message} Uma ou mais fontes atingiram tempo limite."

        return {
            "status": status,
            "partial_success": partial_success,
            "message": message,
            "sources_requested": len(normalized_requested),
            "sources_with_data": sources_with_data,
            "sources_failed": sources_failed,
            "timeout_sources": sorted(timeout_sources),
            "source_status": source_status,
            "unmapped_error_count": unmapped_error_count,
        }

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
        del user_id, query, sources, period_days, locality, use_cache
        raise RuntimeError(
            "Fluxo externo de scraping foi removido. Use /api/ingestion/comments para staging JSON e /api/search para importar do secundario ao primario."
        )

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

        critical_mentions = [
            m
            for m in mentions
            if str(m.get("criticality") or "").strip().lower() in {"alta", "critica", "crítica", "high", "critical"}
        ]

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

    @staticmethod
    def _dedupe_mentions_in_memory(mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique_items: list[dict[str, Any]] = []
        seen: set[str] = set()

        for mention in mentions:
            signature = SearchService._mention_signature(mention)
            dedupe_key = (
                signature.get("content_hash")
                or signature.get("canonical_url")
                or signature.get("text_fingerprint")
                or signature.get("external_id")
                or signature.get("fallback")
            )
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            unique_items.append(mention)

        return unique_items

    @staticmethod
    def _load_existing_signatures(
        *,
        db,
        user_id: str,
        mentions: list[dict[str, Any]],
    ) -> dict[str, set[str]]:
        signatures = {
            "external_id": set(),
            "text_fingerprint": set(),
            "content_hash": set(),
            "canonical_url": set(),
            "fallback": set(),
        }

        external_ids = sorted({str(item.get("external_id") or "").strip() for item in mentions if item.get("external_id")})
        text_fingerprints = sorted(
            {str(item.get("text_fingerprint") or "").strip() for item in mentions if item.get("text_fingerprint")}
        )
        content_hashes = sorted({str(item.get("content_hash") or "").strip() for item in mentions if item.get("content_hash")})
        canonical_urls = sorted(
            {
                normalized_url
                for item in mentions
                for normalized_url in [canonicalize_url(str(item.get("canonical_url") or item.get("url") or ""))]
                if normalized_url
            }
        )

        or_filters: list[dict[str, Any]] = []
        if external_ids:
            or_filters.append({"external_id": {"$in": external_ids}})
        if text_fingerprints:
            or_filters.append({"text_fingerprint": {"$in": text_fingerprints}})
        if content_hashes:
            or_filters.append({"content_hash": {"$in": content_hashes}})
        if canonical_urls:
            or_filters.append({"canonical_url": {"$in": canonical_urls}})

        if not or_filters:
            return signatures

        existing = db.mentions.find(
            {
                "user_id": user_id,
                "$or": or_filters,
            },
            {
                "external_id": 1,
                "text_fingerprint": 1,
                "content_hash": 1,
                "canonical_url": 1,
            },
        )

        for item in existing:
            for key in ["external_id", "text_fingerprint", "content_hash", "canonical_url"]:
                value = str(item.get(key) or "").strip()
                if value:
                    signatures[key].add(value)

        return signatures

    @staticmethod
    def _mention_signature(mention: dict[str, Any]) -> dict[str, str]:
        source = str(mention.get("source") or "").strip().lower()
        author = str(mention.get("author") or "").strip().lower()
        text = str(mention.get("text") or "").strip().lower()

        external_id = str(mention.get("external_id") or "").strip()
        text_fingerprint = str(mention.get("text_fingerprint") or "").strip()
        content_hash = str(mention.get("content_hash") or "").strip()
        canonical_url = canonicalize_url(str(mention.get("canonical_url") or mention.get("url") or ""))
        fallback = f"{source}|{author}|{text[:220]}"

        return {
            "external_id": external_id,
            "text_fingerprint": text_fingerprint,
            "content_hash": content_hash,
            "canonical_url": canonical_url,
            "fallback": fallback,
        }

    @staticmethod
    def _signature_exists(signature: dict[str, str], signatures: dict[str, set[str]]) -> bool:
        for key in ["external_id", "text_fingerprint", "content_hash", "canonical_url", "fallback"]:
            value = signature.get(key) or ""
            if value and value in signatures[key]:
                return True
        return False

    @staticmethod
    def _remember_signature(signature: dict[str, str], signatures: dict[str, set[str]]) -> None:
        for key in ["external_id", "text_fingerprint", "content_hash", "canonical_url", "fallback"]:
            value = signature.get(key) or ""
            if value:
                signatures[key].add(value)

    @staticmethod
    def _rank_mention(mention: dict[str, Any], enrichment: dict[str, Any]) -> float:
        source_weight = float(mention.get("source_priority") or 50)
        quality_component = float(mention.get("quality_score") or 0) * 30

        criticality = str(enrichment.get("criticality") or "").lower()
        criticality_bonus = 0.0
        if criticality == "alta":
            criticality_bonus = 20.0
        elif criticality == "media":
            criticality_bonus = 10.0

        recency_bonus = 0.0
        published_at = mention.get("published_at")
        if isinstance(published_at, datetime):
            now = utcnow()
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=now.tzinfo)
            age_days = max(0.0, (now - published_at).total_seconds() / 86400)
            recency_bonus = max(0.0, 15.0 - min(15.0, age_days))

        return round(source_weight + quality_component + criticality_bonus + recency_bonus, 3)
