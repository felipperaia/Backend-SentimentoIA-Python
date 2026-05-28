from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

from app.config import settings
from app.database import get_db
from app.services.company_utils import normalize_company_filter, slugify_company
from app.services.enrichment_service import EnrichmentService
from app.services.llm_service import LLMService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


DB_UNAVAILABLE_ERROR = "Banco de dados indisponivel"
ALLOWED_LOCALES = {"pt-BR", "en-US"}
ALLOWED_THEMES = {"light", "dark"}
ALLOWED_PRIORITIES = {"high", "medium", "low"}
ALLOWED_URGENCIES = {"high", "medium", "low"}
ALLOWED_RESOLUTIONS = {"pending", "in_progress", "resolved"}
ALLOWED_STATUSES = {"open", "in_progress", "resolved"}


class InsightGenerationError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code or "insight_generation_error")
        self.message = str(message or "Nao foi possivel gerar insight")
        self.details = details or {}


class InsightService:
    @staticmethod
    def _to_json_compatible(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): InsightService._to_json_compatible(item) for key, item in value.items()}
        if isinstance(value, list):
            return [InsightService._to_json_compatible(item) for item in value]
        return value

    @staticmethod
    def _filters_hash(filters: dict[str, Any] | None = None) -> str:
        payload = InsightService._to_json_compatible(filters or {})
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _insight_cache_key(
        *,
        user_id: str,
        company_slug: str,
        period_from: datetime | None,
        period_to: datetime | None,
        filters_hash: str,
    ) -> str:
        period_from_iso = period_from.isoformat() if isinstance(period_from, datetime) else ""
        period_to_iso = period_to.isoformat() if isinstance(period_to, datetime) else ""
        seed = f"{user_id}|{company_slug}|{period_from_iso}|{period_to_iso}|{filters_hash}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @staticmethod
    def _effective_period_range(
        *,
        period_from: datetime | None,
        period_to: datetime | None,
        period_days: int | None,
    ) -> tuple[datetime | None, datetime | None]:
        if isinstance(period_from, datetime) or isinstance(period_to, datetime):
            return period_from, period_to

        if period_days and int(period_days) > 0:
            now = utcnow()
            return (now - timedelta(days=int(period_days))).replace(microsecond=0), now.replace(microsecond=0)

        return None, None

    @staticmethod
    def _default_dashboard_settings(user_id: str) -> dict[str, Any]:
        now = utcnow()
        return {
            "user_id": user_id,
            "locale": "pt-BR",
            "theme": "light",
            "llm_trigger_min_comments": max(1, int(settings.LLM_TRIGGER_MIN_COMMENTS)),
            "auto_archive_insights": False,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _get_or_create_dashboard_settings(user_id: str) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)
        defaults = InsightService._default_dashboard_settings(user_id=user_id)
        current = db.dashboard_settings.find_one_and_update(
            {"user_id": user_id},
            {"$setOnInsert": defaults},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if not current:
            # Consulta adicional defensiva para cenarios raros de retorno nulo.
            current = db.dashboard_settings.find_one({"user_id": user_id})
        if not current:
            raise RuntimeError("Falha ao inicializar dashboard_settings")
        return current

    @staticmethod
    def _serialize_settings(settings_doc: dict[str, Any]) -> dict[str, Any]:
        locale = str(settings_doc.get("locale") or "pt-BR").strip()
        if locale not in ALLOWED_LOCALES:
            locale = "pt-BR"

        theme = str(settings_doc.get("theme") or "light").strip().lower()
        if theme not in ALLOWED_THEMES:
            theme = "light"

        try:
            threshold = int(settings_doc.get("llm_trigger_min_comments", settings.LLM_TRIGGER_MIN_COMMENTS))
        except (TypeError, ValueError):
            threshold = int(settings.LLM_TRIGGER_MIN_COMMENTS)

        return {
            "theme": theme,
            "locale": locale,
            "llm_trigger_min_comments": max(1, threshold),
            "updated_at": settings_doc.get("updated_at").isoformat()
            if isinstance(settings_doc.get("updated_at"), datetime)
            else settings_doc.get("updated_at"),
        }

    @staticmethod
    def get_user_settings(user_id: str) -> dict[str, Any]:
        settings_doc = InsightService._get_or_create_dashboard_settings(user_id=user_id)
        return InsightService._serialize_settings(settings_doc)

    @staticmethod
    def update_user_settings(
        user_id: str,
        *,
        locale: str,
        theme: str,
        llm_trigger_min_comments: int,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        normalized_locale = (locale or "pt-BR").strip()
        normalized_theme = (theme or "light").strip().lower()
        normalized_threshold = max(1, int(llm_trigger_min_comments))

        if normalized_locale not in ALLOWED_LOCALES:
            raise ValueError("Locale invalido. Use pt-BR ou en-US")
        if normalized_theme not in ALLOWED_THEMES:
            raise ValueError("Tema invalido. Use light ou dark")

        updated = db.dashboard_settings.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "locale": normalized_locale,
                    "theme": normalized_theme,
                    "llm_trigger_min_comments": normalized_threshold,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {
                    "user_id": user_id,
                    "auto_archive_insights": False,
                    "created_at": utcnow(),
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            raise RuntimeError("Falha ao atualizar settings")
        return InsightService._serialize_settings(updated)

    @staticmethod
    def get_threshold(user_id: str) -> int:
        settings_doc = InsightService._get_or_create_dashboard_settings(user_id=user_id)
        try:
            threshold = int(settings_doc.get("llm_trigger_min_comments", settings.LLM_TRIGGER_MIN_COMMENTS))
        except (TypeError, ValueError):
            threshold = int(settings.LLM_TRIGGER_MIN_COMMENTS)
        return max(1, threshold)

    @staticmethod
    def _normalize_priority(value: str | None, fallback: str = "medium") -> str:
        candidate = str(value or "").strip().lower().replace(" ", "_")
        if candidate in {"alta", "high", "critica", "critical"}:
            return "high"
        if candidate in {"media", "medium", "moderada", "moderate"}:
            return "medium"
        if candidate in {"baixa", "low", "ok"}:
            return "low"
        return fallback if fallback in ALLOWED_PRIORITIES else "medium"

    @staticmethod
    def _normalize_urgency(value: str | None, fallback: str = "medium") -> str:
        candidate = str(value or "").strip().lower().replace(" ", "_")
        if candidate in {"alta", "high", "critica", "critical", "urgent", "urgente"}:
            return "high"
        if candidate in {"media", "medium", "moderada", "moderate"}:
            return "medium"
        if candidate in {"baixa", "low", "ok"}:
            return "low"
        return fallback if fallback in ALLOWED_URGENCIES else "medium"

    @staticmethod
    def _normalize_resolution(value: str | None, fallback: str = "pending") -> str:
        candidate = str(value or "").strip().lower().replace(" ", "_")
        if candidate in {"resolvido", "resolved", "done", "finalizado", "concluido", "concluído"}:
            return "resolved"
        if candidate in {"in_progress", "em_andamento", "processing", "working", "ongoing"}:
            return "in_progress"
        if candidate in {"pendente", "pending", "open", "novo", "new"}:
            return "pending"
        return fallback if fallback in ALLOWED_RESOLUTIONS else "pending"

    @staticmethod
    def _normalize_status(value: str | None, fallback: str = "open") -> str:
        candidate = str(value or "").strip().lower().replace(" ", "_")
        if candidate in {"open", "aberto", "new", "novo", "pending"}:
            return "open"
        if candidate in {"in_progress", "em_andamento", "processing", "working", "ongoing"}:
            return "in_progress"
        if candidate in {"resolved", "resolvido", "closed", "concluido", "concluído", "done"}:
            return "resolved"
        return fallback if fallback in ALLOWED_STATUSES else "open"

    @staticmethod
    def _priority_rank(priority: str) -> int:
        mapping = {"low": 0, "medium": 1, "high": 2}
        return mapping.get(priority, 1)

    @staticmethod
    def _merge_priority(objective_priority: str, ai_priority: str) -> str:
        return objective_priority if InsightService._priority_rank(objective_priority) >= InsightService._priority_rank(ai_priority) else ai_priority

    @staticmethod
    def _resolve_context(
        user_id: str,
        context_id: str,
        context_type: str | None = None,
    ) -> tuple[str, str]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        safe_context_id = str(context_id or "").strip()
        if not safe_context_id:
            raise ValueError("Contexto de insight invalido")

        if context_type == "batch":
            if db.comment_batches.find_one({"user_id": user_id, "batch_id": safe_context_id}):
                return "batch", safe_context_id
            raise ValueError("Batch nao encontrado")

        if context_type == "search":
            if db.search_jobs.find_one({"user_id": user_id, "search_id": safe_context_id}):
                return "search", safe_context_id
            if db.mentions.find_one({"user_id": user_id, "search_id": safe_context_id}):
                return "search", safe_context_id
            raise ValueError("Busca nao encontrada")

        if db.comment_batches.find_one({"user_id": user_id, "batch_id": safe_context_id}):
            return "batch", safe_context_id
        if db.search_jobs.find_one({"user_id": user_id, "search_id": safe_context_id}):
            return "search", safe_context_id
        if db.mentions.find_one({"user_id": user_id, "search_id": safe_context_id}):
            return "search", safe_context_id
        if db.mentions.find_one({"user_id": user_id, "batch_id": safe_context_id}):
            return "batch", safe_context_id

        raise ValueError("Contexto de insight nao encontrado")

    @staticmethod
    def _latest_context(user_id: str) -> tuple[str, str]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        latest_batch = db.comment_batches.find_one(
            {"user_id": user_id},
            sort=[("updated_at", -1), ("created_at", -1)],
        )
        latest_search = db.search_jobs.find_one(
            {"user_id": user_id, "status": "completed"},
            sort=[("updated_at", -1), ("created_at", -1)],
        )

        if latest_batch and latest_search:
            batch_updated_at = latest_batch.get("updated_at") or latest_batch.get("created_at") or datetime.min
            search_updated_at = latest_search.get("updated_at") or latest_search.get("created_at") or datetime.min
            if search_updated_at >= batch_updated_at:
                return "search", str(latest_search.get("search_id"))
            return "batch", str(latest_batch.get("batch_id"))

        if latest_search:
            return "search", str(latest_search.get("search_id"))
        if latest_batch:
            return "batch", str(latest_batch.get("batch_id"))

        raise ValueError("Nenhum dado recente encontrado para gerar insight")

    @staticmethod
    def _context_mentions_count(user_id: str, context_type: str, context_id: str) -> int:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        if context_type == "batch":
            return int(
                db.mentions.count_documents(
                    {"user_id": user_id, "batch_id": context_id, "status": "processed"}
                )
            )
        return int(db.mentions.count_documents({"user_id": user_id, "search_id": context_id}))

    @staticmethod
    def enqueue_job_if_threshold_reached(
        user_id: str,
        context_id: str,
        trigger: str = "auto",
        force: bool = False,
        context_type: str | None = None,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        try:
            resolved_context_type, resolved_context_id = InsightService._resolve_context(
                user_id=user_id,
                context_id=context_id,
                context_type=context_type,
            )
        except ValueError as exc:
            return {"queued": False, "reason": str(exc)}

        threshold = InsightService.get_threshold(user_id=user_id)
        processed_count = InsightService._context_mentions_count(
            user_id=user_id,
            context_type=resolved_context_type,
            context_id=resolved_context_id,
        )
        if not force and processed_count < threshold:
            return {
                "queued": False,
                "reason": "threshold_not_met",
                "threshold": threshold,
                "processed_count": processed_count,
            }

        active_job = db.insight_jobs.find_one(
            {
                "user_id": user_id,
                "context_id": resolved_context_id,
                "context_type": resolved_context_type,
                "status": {"$in": ["queued", "processing"]},
            }
        )
        if active_job:
            return {"queued": False, "reason": "active_job_exists", "job_id": active_job.get("job_id")}

        if trigger == "auto" and not force:
            active_insight = db.insights.find_one(
                {
                    "user_id": user_id,
                    "context_id": resolved_context_id,
                    "context_type": resolved_context_type,
                    "archived": False,
                }
            )
            if active_insight:
                return {
                    "queued": False,
                    "reason": "active_insight_exists",
                    "insight_id": active_insight.get("insight_id") or active_insight.get("_id"),
                }

        now = utcnow()
        job_id = f"ijob_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        job_doc = {
            "_id": job_id,
            "job_id": job_id,
            "user_id": user_id,
            "context_id": resolved_context_id,
            "context_type": resolved_context_type,
            "batch_id": resolved_context_id if resolved_context_type == "batch" else None,
            "search_id": resolved_context_id if resolved_context_type == "search" else None,
            "trigger": trigger,
            "status": "queued",
            "threshold": threshold,
            "processed_count_at_enqueue": processed_count,
            "created_at": now,
            "updated_at": now,
        }
        db.insight_jobs.insert_one(job_doc)
        return {
            "queued": True,
            "job_id": job_id,
            "threshold": threshold,
            "processed_count": processed_count,
            "context_id": resolved_context_id,
            "context_type": resolved_context_type,
        }

    @staticmethod
    def enqueue_jobs_for_ready_batches(limit: int = 100) -> dict[str, int]:
        db = get_db()
        if db is None:
            return {"checked": 0, "queued": 0}

        batches = list(
            db.comment_batches.find(
                {
                    "status": {"$in": ["processed", "processed_with_errors"]},
                    "processed_count": {"$gt": 0},
                }
            )
            .sort("updated_at", -1)
            .limit(max(1, limit))
        )

        queued = 0
        for batch in batches:
            result = InsightService.enqueue_job_if_threshold_reached(
                user_id=str(batch.get("user_id", "")),
                context_id=str(batch.get("batch_id", "")),
                trigger="auto",
                force=False,
                context_type="batch",
            )
            if result.get("queued"):
                queued += 1

        return {"checked": len(batches), "queued": queued}

    @staticmethod
    def _build_snapshot(user_id: str, context_id: str, context_type: str = "batch") -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        if context_type == "batch":
            batch = db.comment_batches.find_one({"user_id": user_id, "batch_id": context_id})
            if not batch:
                raise ValueError("Batch nao encontrado")

            mentions = list(
                db.mentions.find(
                    {
                        "user_id": user_id,
                        "batch_id": context_id,
                        "status": "processed",
                    },
                    {"raw": 0},
                ).sort("urgency_score", -1)
            )
            brand_name = batch.get("brand") or "indefinida"
        else:
            search_job = db.search_jobs.find_one(
                {"user_id": user_id, "search_id": context_id, "status": "completed"}
            )
            mentions = list(
                db.mentions.find(
                    {
                        "user_id": user_id,
                        "search_id": context_id,
                    },
                    {"raw": 0},
                ).sort("mention_rank_score", -1)
            )
            brand_name = (
                (search_job or {}).get("query")
                or (mentions[0].get("query") if mentions else None)
                or (mentions[0].get("entity") if mentions else None)
                or "indefinida"
            )

        company_name = str(brand_name or "indefinida")
        company_slug = normalize_company_filter(
            company_slug=(search_job or {}).get("company_slug") if context_type == "search" else batch.get("company_slug") if context_type == "batch" else None,
            company_id=company_name,
        ) or slugify_company(company_name)

        if not mentions:
            raise ValueError("Contexto sem mencoes processadas")

        metrics = EnrichmentService.aggregate(mentions)
        critical_terms_counter: Counter[str] = Counter()
        for mention in mentions:
            for term in mention.get("critical_terms") or []:
                critical_terms_counter[str(term)] += 1

        sentiment_distribution = metrics.get("sentiment_distribution") or {}
        positive_count = int(sentiment_distribution.get("positivo", sentiment_distribution.get("positive", 0)) or 0)
        negative_count = int(sentiment_distribution.get("negativo", sentiment_distribution.get("negative", 0)) or 0)
        neutral_count = int(sentiment_distribution.get("neutro", sentiment_distribution.get("neutral", 0)) or 0)

        top_themes = list((metrics.get("top_aspects") or {}).keys())[:5]

        negative_mentions = [
            mention
            for mention in mentions
            if str(mention.get("sentiment") or "").lower() in {"negativo", "negative"}
        ]
        negative_mentions.sort(
            key=lambda item: (
                1 if str(item.get("criticality") or "").lower() in {"alta", "high", "critical"} else 0,
                float(item.get("urgency_score") or 0),
            ),
            reverse=True,
        )
        top_negative_texts = [str(item.get("text") or "")[:500] for item in negative_mentions[:10] if item.get("text")]

        positive_mentions = [
            mention
            for mention in mentions
            if str(mention.get("sentiment") or "").lower() in {"positivo", "positive"}
        ]
        positive_mentions.sort(key=lambda item: float(item.get("reputation_score") or 0), reverse=True)
        top_positive_texts = [str(item.get("text") or "")[:500] for item in positive_mentions[:5] if item.get("text")]

        source_distribution = Counter(str(mention.get("source") or "unknown") for mention in mentions)
        criticality_distribution = Counter(str(mention.get("criticality") or "unknown") for mention in mentions)

        published = [m.get("published_at") for m in mentions if isinstance(m.get("published_at"), datetime)]
        if published:
            period_from = min(published)
            period_to = max(published)
            date_range = {
                "start": period_from.isoformat(),
                "end": period_to.isoformat(),
            }
            period = f"{period_from.date().isoformat()}/{period_to.date().isoformat()}"
        else:
            period_from = None
            period_to = None
            date_range = {"start": None, "end": None}
            period = "indefinido"

        max_samples = max(1, int(settings.LLM_MAX_SAMPLE_MENTIONS))
        sample_mentions = [
            {
                "text": (mention.get("text") or "")[:500],
                "sentiment": mention.get("sentiment"),
                "criticality": mention.get("criticality"),
                "urgency_score": mention.get("urgency_score"),
                "source": mention.get("source"),
            }
            for mention in mentions[:max_samples]
        ]

        source_references = [
            {
                "mention_id": str(mention.get("_id") or ""),
                "source": str(mention.get("source") or "unknown"),
                "url": str(mention.get("url") or ""),
                "canonical_url": str(mention.get("canonical_url") or ""),
                "published_at": mention.get("published_at").isoformat()
                if isinstance(mention.get("published_at"), datetime)
                else mention.get("published_at"),
            }
            for mention in mentions[:50]
        ]

        snapshot = {
            "batch_id": context_id if context_type == "batch" else None,
            "search_id": context_id if context_type == "search" else None,
            "context_id": context_id,
            "context_type": context_type,
            "brand": brand_name,
            "company_name": company_name,
            "company_slug": company_slug,
            "query": brand_name,
            "period": period,
            "period_from": period_from,
            "period_to": period_to,
            "date_range": date_range,
            "total_mentions": len(mentions),
            "total_comments": len(mentions),
            "positive_count": positive_count,
            "negative_count": negative_count,
            "neutral_count": neutral_count,
            "sentiment_distribution": sentiment_distribution,
            "critical_mentions": int(metrics.get("critical_mentions", 0)),
            "average_urgency": float(metrics.get("average_urgency", 0)),
            "top_themes": top_themes,
            "top_negative_texts": top_negative_texts,
            "top_positive_texts": top_positive_texts,
            "source_distribution": dict(source_distribution),
            "criticality_distribution": dict(criticality_distribution),
            "source_references": source_references,
            "sample_mentions": sample_mentions,
            "top_aspects": list((metrics.get("top_aspects") or {}).keys())[:10],
            "top_critical_terms": [term for term, _ in critical_terms_counter.most_common(10)],
        }

        required_defaults = {
            "batch_id": None,
            "search_id": None,
            "context_id": context_id,
            "context_type": context_type,
            "brand": "indefinida",
            "company_name": "indefinida",
            "company_slug": None,
            "query": "indefinida",
            "period": "indefinido",
            "period_from": None,
            "period_to": None,
            "date_range": {"start": None, "end": None},
            "total_mentions": 0,
            "total_comments": 0,
            "positive_count": 0,
            "negative_count": 0,
            "neutral_count": 0,
            "sentiment_distribution": {},
            "critical_mentions": 0,
            "average_urgency": 0.0,
            "top_themes": [],
            "top_negative_texts": [],
            "top_positive_texts": [],
            "source_distribution": {},
            "criticality_distribution": {},
            "source_references": [],
            "sample_mentions": [],
            "top_aspects": [],
            "top_critical_terms": [],
        }
        required_defaults.update(snapshot)
        return required_defaults

    @staticmethod
    def _build_snapshot_for_company_filters(
        *,
        user_id: str,
        company_slug: str,
        period_from: datetime | None,
        period_to: datetime | None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        query: dict[str, Any] = {
            "user_id": user_id,
            "company_slug": company_slug,
        }

        published_filter: dict[str, Any] = {}
        if isinstance(period_from, datetime):
            published_filter["$gte"] = period_from
        if isinstance(period_to, datetime):
            published_filter["$lte"] = period_to
        if published_filter:
            query["published_at"] = published_filter

        source_filters = (filters or {}).get("sources")
        if isinstance(source_filters, list) and source_filters:
            normalized_sources = [str(item).strip().lower() for item in source_filters if str(item).strip()]
            if normalized_sources:
                query["source"] = {"$in": normalized_sources}

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("published_at", -1)
            .limit(5000)
        )

        if not mentions:
            raise ValueError("Nao ha mencoes no periodo para gerar insight")

        metrics = EnrichmentService.aggregate(mentions)
        sentiment_distribution = metrics.get("sentiment_distribution") or {}
        positive_count = int(sentiment_distribution.get("positivo", sentiment_distribution.get("positive", 0)) or 0)
        negative_count = int(sentiment_distribution.get("negativo", sentiment_distribution.get("negative", 0)) or 0)
        neutral_count = int(sentiment_distribution.get("neutro", sentiment_distribution.get("neutral", 0)) or 0)

        critical_terms_counter: Counter[str] = Counter()
        for mention in mentions:
            for term in mention.get("critical_terms") or []:
                critical_terms_counter[str(term)] += 1

        negative_mentions = [
            mention
            for mention in mentions
            if str(mention.get("sentiment") or "").lower() in {"negativo", "negative"}
        ]
        negative_mentions.sort(
            key=lambda item: (
                1 if str(item.get("criticality") or "").lower() in {"alta", "high", "critical"} else 0,
                float(item.get("urgency_score") or 0),
            ),
            reverse=True,
        )
        top_negative_texts = [str(item.get("text") or "")[:500] for item in negative_mentions[:10] if item.get("text")]

        positive_mentions = [
            mention
            for mention in mentions
            if str(mention.get("sentiment") or "").lower() in {"positivo", "positive"}
        ]
        positive_mentions.sort(key=lambda item: float(item.get("reputation_score") or 0), reverse=True)
        top_positive_texts = [str(item.get("text") or "")[:500] for item in positive_mentions[:5] if item.get("text")]

        source_distribution = Counter(str(mention.get("source") or "unknown") for mention in mentions)
        criticality_distribution = Counter(str(mention.get("criticality") or "unknown") for mention in mentions)

        available_dates = [
            mention.get("published_at") or mention.get("created_at")
            for mention in mentions
            if isinstance(mention.get("published_at") or mention.get("created_at"), datetime)
        ]
        inferred_period_from = min(available_dates) if available_dates else None
        inferred_period_to = max(available_dates) if available_dates else None
        effective_period_from = period_from or inferred_period_from
        effective_period_to = period_to or inferred_period_to

        if effective_period_from and effective_period_to:
            date_range = {
                "start": effective_period_from.isoformat(),
                "end": effective_period_to.isoformat(),
            }
            period = f"{effective_period_from.date().isoformat()}/{effective_period_to.date().isoformat()}"
        else:
            date_range = {"start": None, "end": None}
            period = "indefinido"

        max_samples = max(1, int(settings.LLM_MAX_SAMPLE_MENTIONS))
        sample_mentions = [
            {
                "text": (mention.get("text") or "")[:500],
                "sentiment": mention.get("sentiment"),
                "criticality": mention.get("criticality"),
                "urgency_score": mention.get("urgency_score"),
                "source": mention.get("source"),
            }
            for mention in mentions[:max_samples]
        ]

        source_references = [
            {
                "mention_id": str(mention.get("_id") or ""),
                "source": str(mention.get("source") or "unknown"),
                "url": str(mention.get("url") or ""),
                "canonical_url": str(mention.get("canonical_url") or ""),
                "published_at": mention.get("published_at").isoformat()
                if isinstance(mention.get("published_at"), datetime)
                else mention.get("published_at"),
            }
            for mention in mentions[:50]
        ]

        company_name = str(mentions[0].get("company_name") or mentions[0].get("query") or company_slug)

        return {
            "batch_id": None,
            "search_id": None,
            "context_id": None,
            "context_type": "company_period",
            "brand": company_name,
            "company_name": company_name,
            "company_slug": company_slug,
            "query": company_name,
            "period": period,
            "period_from": effective_period_from,
            "period_to": effective_period_to,
            "date_range": date_range,
            "total_mentions": len(mentions),
            "total_comments": len(mentions),
            "positive_count": positive_count,
            "negative_count": negative_count,
            "neutral_count": neutral_count,
            "sentiment_distribution": sentiment_distribution,
            "critical_mentions": int(metrics.get("critical_mentions", 0)),
            "average_urgency": float(metrics.get("average_urgency", 0)),
            "top_themes": list((metrics.get("top_aspects") or {}).keys())[:5],
            "top_negative_texts": top_negative_texts,
            "top_positive_texts": top_positive_texts,
            "source_distribution": dict(source_distribution),
            "criticality_distribution": dict(criticality_distribution),
            "source_references": source_references,
            "sample_mentions": sample_mentions,
            "top_aspects": list((metrics.get("top_aspects") or {}).keys())[:10],
            "top_critical_terms": [term for term, _ in critical_terms_counter.most_common(10)],
        }

    @staticmethod
    def _build_operational_fields(snapshot: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
        total_comments = max(1, int(snapshot.get("total_comments", 0) or 0))
        average_urgency = float(snapshot.get("average_urgency", 0) or 0)
        critical_mentions = int(snapshot.get("critical_mentions", 0) or 0)
        sentiment_distribution = snapshot.get("sentiment_distribution") or {}
        negative_mentions = int(
            sentiment_distribution.get("negativo", sentiment_distribution.get("negative", 0)) or 0
        )

        critical_ratio = min(1.0, critical_mentions / total_comments)
        negative_ratio = min(1.0, negative_mentions / total_comments)
        objective_score = min(1.0, (average_urgency * 0.5) + (critical_ratio * 0.3) + (negative_ratio * 0.2))

        if objective_score >= 0.66:
            objective_priority = "high"
        elif objective_score >= 0.40:
            objective_priority = "medium"
        else:
            objective_priority = "low"

        if average_urgency >= 0.70:
            objective_urgency = "high"
        elif average_urgency >= 0.40:
            objective_urgency = "medium"
        else:
            objective_urgency = "low"

        ai_priority = InsightService._normalize_priority(str(analysis.get("priority") or ""), fallback=objective_priority)
        ai_urgency = InsightService._normalize_urgency(str(analysis.get("urgency") or ""), fallback=objective_urgency)

        final_priority = InsightService._merge_priority(objective_priority, ai_priority)
        final_urgency = InsightService._merge_priority(objective_urgency, ai_urgency)

        resolution = InsightService._normalize_resolution(str(analysis.get("resolution") or ""), fallback="pending")
        default_status = "resolved" if resolution == "resolved" else "open"
        status = InsightService._normalize_status(str(analysis.get("status") or ""), fallback=default_status)

        root_cause = str(analysis.get("root_cause") or "").strip()
        if not root_cause:
            critical_terms = snapshot.get("top_critical_terms") or []
            if critical_terms:
                root_cause = f"Padroes criticos detectados: {', '.join(critical_terms[:3])}"
            else:
                root_cause = "Causa raiz em investigacao"

        recommended_action = str(analysis.get("recommended_action") or "").strip()
        if not recommended_action:
            action_list = analysis.get("recommended_actions") or []
            recommended_action = str(action_list[0]) if action_list else "Priorizar tratativas de alta severidade."

        company_name = str(snapshot.get("company_name") or snapshot.get("brand") or "indefinida")
        company_slug = normalize_company_filter(
            company_slug=str(snapshot.get("company_slug") or "") or None,
            company_id=company_name,
        )

        return {
            "company": str(snapshot.get("brand") or "indefinida"),
            "company_name": company_name,
            "company_slug": company_slug,
            "period_from": snapshot.get("period_from"),
            "period_to": snapshot.get("period_to"),
            "priority": final_priority,
            "urgency": final_urgency,
            "root_cause": root_cause,
            "recommended_action": recommended_action,
            "status": status,
            "resolution": resolution,
            "timestamp": utcnow().isoformat(),
            "priority_score": round(objective_score, 4),
        }

    @staticmethod
    async def process_queued_jobs(limit: int = 3) -> dict[str, int]:
        db = get_db()
        if db is None:
            return {"picked": 0, "completed": 0, "failed": 0}

        jobs = list(
            db.insight_jobs.find({"status": "queued"}).sort("created_at", 1).limit(max(1, limit))
        )

        completed = 0
        failed = 0

        for job in jobs:
            job_id = str(job.get("job_id") or job.get("_id"))
            user_id = str(job.get("user_id", ""))
            context_type = str(job.get("context_type") or ("batch" if job.get("batch_id") else "search"))
            context_id = str(job.get("context_id") or job.get("batch_id") or job.get("search_id") or "")

            claimed = db.insight_jobs.update_one(
                {"_id": job_id, "status": "queued"},
                {"$set": {"status": "processing", "updated_at": utcnow()}},
            )
            if claimed.modified_count == 0:
                continue

            try:
                if not context_id:
                    raise ValueError("contexto do job invalido")

                snapshot = InsightService._build_snapshot(
                    user_id=user_id,
                    context_id=context_id,
                    context_type=context_type,
                )
                analysis_raw = await LLMService.analyze_snapshot(snapshot=snapshot, user_id=user_id)
                if not isinstance(analysis_raw, dict):
                    raise RuntimeError("Resposta da LLM invalida para geracao de insight")

                llm_unavailable = False
                analysis = LLMService.normalize_analysis(analysis_raw)
                analysis["llm_unavailable"] = llm_unavailable

                processed_count_now = InsightService._context_mentions_count(
                    user_id=user_id,
                    context_type=context_type,
                    context_id=context_id,
                )

                now = utcnow()
                insight_id = f"insight_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
                audit_metadata = {
                    "generated_at": now.isoformat(),
                    "provider": "ollama",
                    "model": str(getattr(settings, "LLM_MODEL_EFFECTIVE", "") or settings.OLLAMA_MODEL or ""),
                    "threshold": int(job.get("threshold") or InsightService.get_threshold(user_id=user_id)),
                    "processed_count_at_enqueue": int(job.get("processed_count_at_enqueue") or 0),
                    "processed_count_at_generation": processed_count_now,
                    "job_trigger": str(job.get("trigger") or "auto"),
                    "deterministic_rules_version": "priority-v1",
                    "llm_unavailable": llm_unavailable,
                }
                operational_fields = InsightService._build_operational_fields(snapshot=snapshot, analysis=analysis)
                insight_doc = {
                    "_id": insight_id,
                    "insight_id": insight_id,
                    "job_id": job_id,
                    "user_id": user_id,
                    "batch_id": context_id if context_type == "batch" else None,
                    "search_id": context_id if context_type == "search" else None,
                    "context_id": context_id,
                    "context_type": context_type,
                    "trigger": job.get("trigger", "auto"),
                    "archived": False,
                    **operational_fields,
                    "source_references": snapshot.get("source_references", []),
                    "source_distribution": snapshot.get("source_distribution", {}),
                    "audit_metadata": audit_metadata,
                    "snapshot": snapshot,
                    "executive_summary": analysis.get("executive_summary") or "Analise indisponivel no momento.",
                    "sentiment_overview": analysis.get("sentiment_overview") or "Resumo de sentimento indisponivel.",
                    "risks": analysis.get("risks") if isinstance(analysis.get("risks"), list) else [],
                    "opportunities": analysis.get("opportunities") if isinstance(analysis.get("opportunities"), list) else [],
                    "recommended_actions": analysis.get("recommended_actions") if isinstance(analysis.get("recommended_actions"), list) else [],
                    "decision_guidance": analysis.get("decision_guidance") or "Direcionamento indisponivel.",
                    "trend": analysis.get("trend") or "stable",
                    "llm_payload": {
                        **analysis,
                        "raw": analysis_raw,
                    },
                    "llm_unavailable": llm_unavailable,
                    "created_at": now,
                    "updated_at": now,
                }
                db.insights.insert_one(insight_doc)

                db.insight_jobs.update_one(
                    {"_id": job_id},
                    {
                        "$set": {
                            "status": "completed",
                            "insight_id": insight_id,
                            "updated_at": utcnow(),
                        }
                    },
                )
                completed += 1
            except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                db.insight_jobs.update_one(
                    {"_id": job_id},
                    {
                        "$set": {
                            "status": "failed",
                            "error_message": str(exc)[:500],
                            "updated_at": utcnow(),
                        }
                    },
                )
                failed += 1

        return {"picked": len(jobs), "completed": completed, "failed": failed}

    @staticmethod
    def list_insights(
        user_id: str,
        include_archived: bool = False,
        limit: int = 50,
        batch_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        priority: str | None = None,
        resolution: str | None = None,
    ) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        conditions: list[dict[str, Any]] = [{"user_id": user_id}]
        if not include_archived:
            conditions.append({"archived": False})

        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)
        if not normalized_company_slug:
            return []

        conditions.append({"company_slug": normalized_company_slug})

        if (
            isinstance(period_from, datetime)
            and isinstance(period_to, datetime)
            and period_from > period_to
        ):
            return []

        if batch_id:
            conditions.append(
                {
                    "$or": [
                        {"batch_id": batch_id},
                        {"search_id": batch_id},
                        {"context_id": batch_id},
                    ]
                }
            )

        if period_from:
            conditions.append({"period_to": {"$gte": period_from}})
        if period_to:
            conditions.append({"period_from": {"$lte": period_to}})

        if priority:
            conditions.append({"priority": InsightService._normalize_priority(priority, fallback="medium")})
        if resolution:
            conditions.append({"resolution": InsightService._normalize_resolution(resolution, fallback="pending")})

        query: dict[str, Any] = {"$and": conditions}

        items = list(
            db.insights.find(query).sort("created_at", -1).limit(max(1, min(limit, 200)))
        )
        return SearchService.serialize_many(items)

    @staticmethod
    async def generate_insight(
        user_id: str,
        *,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        period_days: int | None = None,
        force: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)
        if not normalized_company_slug:
            raise ValueError("company_slug e obrigatorio para gerar insight")

        effective_period_from, effective_period_to = InsightService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )

        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            raise ValueError("Faixa de datas invalida: from deve ser menor ou igual a to")

        safe_filters = {"scope": "insight", **(filters or {})}
        filters_hash = InsightService._filters_hash(safe_filters)
        cache_key = InsightService._insight_cache_key(
            user_id=user_id,
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
            filters_hash=filters_hash,
        )

        existing = db.insights.find_one(
            {
                "user_id": user_id,
                "cache_key": cache_key,
                "archived": False,
            },
            sort=[("updated_at", -1), ("created_at", -1)],
        )

        if existing and not force:
            return SearchService.serialize(existing)

        try:
            snapshot = InsightService._build_snapshot_for_company_filters(
                user_id=user_id,
                company_slug=normalized_company_slug,
                period_from=effective_period_from,
                period_to=effective_period_to,
                filters=safe_filters,
            )
        except ValueError as exc:
            raise InsightGenerationError(
                code="empty_scope",
                message="Nao ha mencoes para a empresa e faixa de datas informadas.",
                details={
                    "company_slug": normalized_company_slug,
                    "period_from": effective_period_from.isoformat() if isinstance(effective_period_from, datetime) else None,
                    "period_to": effective_period_to.isoformat() if isinstance(effective_period_to, datetime) else None,
                },
            ) from exc

        analysis_raw = await LLMService.analyze_snapshot(snapshot=snapshot, user_id=user_id)
        if not isinstance(analysis_raw, dict):
            raise RuntimeError("Resposta da LLM invalida para geracao de insight")

        llm_unavailable = False
        analysis = LLMService.normalize_analysis(analysis_raw)
        analysis["llm_unavailable"] = llm_unavailable

        now = utcnow()
        operational_fields = InsightService._build_operational_fields(snapshot=snapshot, analysis=analysis)

        if existing:
            insight_id = str(existing.get("insight_id") or existing.get("_id") or "")
        else:
            insight_id = f"insight_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

        update_payload = {
            "insight_id": insight_id,
            "user_id": user_id,
            "context_id": cache_key,
            "context_type": "company_period",
            "trigger": "manual",
            "archived": False,
            "cache_scope": "company_period",
            "cache_key": cache_key,
            "filters_hash": filters_hash,
            "filters_payload": safe_filters,
            "company_slug": normalized_company_slug,
            "period_from": effective_period_from,
            "period_to": effective_period_to,
            **operational_fields,
            "source_references": snapshot.get("source_references", []),
            "source_distribution": snapshot.get("source_distribution", {}),
            "snapshot": snapshot,
            "executive_summary": analysis.get("executive_summary") or "Analise indisponivel no momento.",
            "sentiment_overview": analysis.get("sentiment_overview") or "Resumo de sentimento indisponivel.",
            "risks": analysis.get("risks") if isinstance(analysis.get("risks"), list) else [],
            "opportunities": analysis.get("opportunities") if isinstance(analysis.get("opportunities"), list) else [],
            "recommended_actions": analysis.get("recommended_actions") if isinstance(analysis.get("recommended_actions"), list) else [],
            "decision_guidance": analysis.get("decision_guidance") or "Direcionamento indisponivel.",
            "trend": analysis.get("trend") or "stable",
            "llm_payload": {
                **analysis,
                "raw": analysis_raw,
            },
            "llm_unavailable": llm_unavailable,
            "updated_at": now,
        }

        if existing:
            db.insights.update_one(
                {"_id": existing.get("_id"), "user_id": user_id},
                {"$set": update_payload},
            )
            saved = db.insights.find_one({"_id": existing.get("_id"), "user_id": user_id})
        else:
            insight_doc = {
                "_id": insight_id,
                "created_at": now,
                **update_payload,
            }
            db.insights.insert_one(insight_doc)
            saved = insight_doc

        if not saved:
            raise RuntimeError("Insight nao foi gerado")

        return SearchService.serialize(saved)

    @staticmethod
    async def regenerate_insight(user_id: str, insight_id: str) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        current = db.insights.find_one({"_id": insight_id, "user_id": user_id})
        if not current:
            raise ValueError("Insight nao encontrado")

        normalized_company_slug = normalize_company_filter(
            company_slug=str(current.get("company_slug") or "") or None,
            company_id=str(current.get("company_name") or current.get("company") or "") or None,
        )
        if not normalized_company_slug:
            raise RuntimeError("Insight sem company_slug para regeneracao")

        filters_payload = current.get("filters_payload") if isinstance(current.get("filters_payload"), dict) else {}
        return await InsightService.generate_insight(
            user_id=user_id,
            company_slug=normalized_company_slug,
            period_from=current.get("period_from") if isinstance(current.get("period_from"), datetime) else None,
            period_to=current.get("period_to") if isinstance(current.get("period_to"), datetime) else None,
            force=True,
            filters=filters_payload,
        )

    @staticmethod
    def archive_insight(user_id: str, insight_id: str) -> bool:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        result = db.insights.update_one(
            {"_id": insight_id, "user_id": user_id},
            {
                "$set": {
                    "archived": True,
                    "archived_at": utcnow(),
                    "updated_at": utcnow(),
                }
            },
        )
        return result.modified_count > 0

    @staticmethod
    def delete_insight(user_id: str, insight_id: str) -> bool:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        result = db.insights.delete_one({"_id": insight_id, "user_id": user_id})
        return result.deleted_count > 0
