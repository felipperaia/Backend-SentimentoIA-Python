import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import settings
from app.database import get_db
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService

logger = logging.getLogger(__name__)


class NpsService:
    """Serviço NPS para medir satisfação dos serviços do sistema."""

    @staticmethod
    def _min_interactions_met(*, db, user_id: str | None) -> bool:
        min_interactions = max(0, int(getattr(settings, "NPS_MIN_INTERACTIONS", 0) or 0))
        if min_interactions <= 0:
            return True

        if not user_id:
            return False

        completed_searches = int(
            db.search_jobs.count_documents(
                {
                    "user_id": user_id,
                    "status": "completed",
                }
            )
        )
        return completed_searches >= min_interactions

    @staticmethod
    def submit_response(
        *,
        user_id: str | None,
        session_id: str,
        module_key: str,
        score: int,
        comment: str | None = None,
        route: str | None = None,
        context_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        if not 0 <= score <= 10:
            raise ValueError("Score NPS deve ser entre 0 e 10")

        now = utcnow()
        nps_id = f"nps_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

        doc = {
            "_id": nps_id,
            "nps_id": nps_id,
            "user_id": user_id,
            "session_id": session_id,
            "module_key": (module_key or "geral").strip().lower(),
            "score": score,
            "comment": (comment or "").strip()[:1000] or None,
            "shown_at": now,
            "answered_at": now,
            "dismissed_at": None,
            "app_version": "2.0.0",
            "route": route,
            "context_metadata": context_metadata or {},
            "created_at": now,
        }
        db.nps_responses.insert_one(doc)
        logger.info("NPS registrado: score=%d module=%s user=%s", score, module_key, user_id or "anon")
        return SearchService.serialize(doc)

    @staticmethod
    def submit_dismiss(
        *,
        user_id: str | None,
        session_id: str,
        module_key: str,
        route: str | None = None,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        now = utcnow()
        nps_id = f"nps_dismiss_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

        doc = {
            "_id": nps_id,
            "nps_id": nps_id,
            "user_id": user_id,
            "session_id": session_id,
            "module_key": (module_key or "geral").strip().lower(),
            "score": None,
            "comment": None,
            "shown_at": now,
            "answered_at": None,
            "dismissed_at": now,
            "app_version": "2.0.0",
            "route": route,
            "context_metadata": {},
            "created_at": now,
        }
        db.nps_responses.insert_one(doc)
        return SearchService.serialize(doc)

    @staticmethod
    def should_show_nps(user_id: str | None, session_id: str) -> dict[str, Any]:
        """Mostra NPS apenas em trigger pos-busca, com cooldown e sessao inedita."""
        result: dict[str, Any] = {"should_show": False, "trigger": None}

        if not settings.NPS_ENABLED:
            return result

        db = get_db()
        if db is None:
            return result

        if not NpsService._min_interactions_met(db=db, user_id=user_id):
            return result

        session_query: dict[str, Any] = {"session_id": session_id}
        if user_id:
            session_query["user_id"] = user_id
        if db.nps_responses.find_one(session_query):
            return result

        recent_search_cutoff = utcnow() - timedelta(hours=24)
        search_query: dict[str, Any] = {
            "status": "completed",
            "$or": [
                {"updated_at": {"$gte": recent_search_cutoff}},
                {"created_at": {"$gte": recent_search_cutoff}},
            ],
        }
        if user_id:
            search_query["user_id"] = user_id

        search_doc = db.search_jobs.find_one(search_query, sort=[("updated_at", -1), ("created_at", -1)])
        if not search_doc:
            return result

        history_query: dict[str, Any] = {"user_id": user_id} if user_id else {"session_id": {"$ne": session_id}}
        last_response = db.nps_responses.find_one(history_query, sort=[("created_at", -1)])
        search_timestamp = search_doc.get("updated_at") or search_doc.get("created_at")
        last_created_at = last_response.get("created_at") if isinstance(last_response, dict) else None

        cooldown_cutoff = utcnow() - timedelta(days=max(1, int(settings.NPS_COOLDOWN_DAYS or 7)))
        if isinstance(last_created_at, datetime) and last_created_at > cooldown_cutoff:
            return result

        if isinstance(last_created_at, datetime) and isinstance(search_timestamp, datetime):
            if search_timestamp <= last_created_at:
                return result

        if isinstance(search_timestamp, datetime) and search_timestamp < recent_search_cutoff:
            return result

        return {"should_show": True, "trigger": "post_search"}

    @staticmethod
    def get_metrics(
        period_days: int | None = None,
        module_key: str | None = None,
    ) -> dict[str, Any]:
        """Calcula métricas NPS: promotores, neutros, detratores e score."""
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        query: dict[str, Any] = {"score": {"$ne": None}}
        if period_days and period_days > 0:
            cutoff = utcnow() - timedelta(days=period_days)
            query["created_at"] = {"$gte": cutoff}
        if module_key:
            query["module_key"] = module_key.strip().lower()

        responses = list(db.nps_responses.find(query, {"score": 1, "module_key": 1, "comment": 1, "created_at": 1}))
        total = len(responses)

        if total == 0:
            return {
                "total_responses": 0,
                "promoters": 0,
                "passives": 0,
                "detractors": 0,
                "nps_score": 0,
                "average_score": 0,
                "by_module": {},
                "recent_comments": [],
            }

        promoters = sum(1 for r in responses if (r.get("score") or 0) >= 9)
        passives = sum(1 for r in responses if 7 <= (r.get("score") or 0) <= 8)
        detractors = sum(1 for r in responses if (r.get("score") or 0) <= 6)
        nps_score = round(((promoters - detractors) / total) * 100, 1) if total else 0
        avg_score = round(sum(r.get("score", 0) for r in responses) / total, 2)

        # Por módulo
        by_module: dict[str, dict[str, Any]] = {}
        for r in responses:
            mk = r.get("module_key", "geral")
            if mk not in by_module:
                by_module[mk] = {"total": 0, "sum_score": 0, "promoters": 0, "detractors": 0}
            by_module[mk]["total"] += 1
            by_module[mk]["sum_score"] += r.get("score", 0)
            if (r.get("score") or 0) >= 9:
                by_module[mk]["promoters"] += 1
            elif (r.get("score") or 0) <= 6:
                by_module[mk]["detractors"] += 1

        for mk, data in by_module.items():
            t = data["total"]
            data["average"] = round(data.pop("sum_score") / t, 2) if t else 0
            data["nps_score"] = round(((data["promoters"] - data["detractors"]) / t) * 100, 1) if t else 0

        # Comentários recentes
        recent = [
            {
                "comment": r.get("comment"),
                "score": r.get("score"),
                "module_key": r.get("module_key"),
                "created_at": r.get("created_at").isoformat() if hasattr(r.get("created_at"), "isoformat") else r.get("created_at"),
            }
            for r in sorted(responses, key=lambda x: x.get("created_at", ""), reverse=True)[:10]
            if r.get("comment")
        ]

        return {
            "total_responses": total,
            "promoters": promoters,
            "passives": passives,
            "detractors": detractors,
            "nps_score": nps_score,
            "average_score": avg_score,
            "by_module": by_module,
            "recent_comments": recent,
        }
