from collections import Counter
from datetime import datetime
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

from app.config import settings
from app.database import get_db
from app.services.enrichment_service import EnrichmentService
from app.services.llm_service import LLMService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


DB_UNAVAILABLE_ERROR = "Banco de dados indisponivel"
ALLOWED_LOCALES = {"pt-BR", "en-US"}
ALLOWED_THEMES = {"light", "dark"}


class InsightService:
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
            # Fallback defensivo para cenarios raros de retorno nulo.
            current = db.dashboard_settings.find_one({"user_id": user_id})
        if not current:
            raise RuntimeError("Falha ao inicializar dashboard_settings")
        return current

    @staticmethod
    def _serialize_settings(settings_doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "theme": settings_doc.get("theme", "light"),
            "locale": settings_doc.get("locale", "pt-BR"),
            "llm_trigger_min_comments": int(
                settings_doc.get("llm_trigger_min_comments", settings.LLM_TRIGGER_MIN_COMMENTS)
            ),
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
    def enqueue_job_if_threshold_reached(
        user_id: str,
        batch_id: str,
        trigger: str = "auto",
        force: bool = False,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        batch = db.comment_batches.find_one({"user_id": user_id, "batch_id": batch_id})
        if not batch:
            return {"queued": False, "reason": "batch_not_found"}

        threshold = InsightService.get_threshold(user_id=user_id)
        processed_count = int(batch.get("processed_count", 0))
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
                "batch_id": batch_id,
                "status": {"$in": ["queued", "processing"]},
            }
        )
        if active_job:
            return {"queued": False, "reason": "active_job_exists", "job_id": active_job.get("job_id")}

        if trigger == "auto" and not force:
            active_insight = db.insights.find_one(
                {
                    "user_id": user_id,
                    "batch_id": batch_id,
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
            "batch_id": batch_id,
            "trigger": trigger,
            "status": "queued",
            "threshold": threshold,
            "processed_count_at_enqueue": processed_count,
            "created_at": now,
            "updated_at": now,
        }
        db.insight_jobs.insert_one(job_doc)
        return {"queued": True, "job_id": job_id, "threshold": threshold, "processed_count": processed_count}

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
                batch_id=str(batch.get("batch_id", "")),
                trigger="auto",
                force=False,
            )
            if result.get("queued"):
                queued += 1

        return {"checked": len(batches), "queued": queued}

    @staticmethod
    def _build_snapshot(user_id: str, batch_id: str) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        batch = db.comment_batches.find_one({"user_id": user_id, "batch_id": batch_id})
        if not batch:
            raise ValueError("Batch nao encontrado")

        mentions = list(
            db.mentions.find(
                {
                    "user_id": user_id,
                    "batch_id": batch_id,
                    "status": "processed",
                },
                {"raw": 0},
            ).sort("urgency_score", -1)
        )
        if not mentions:
            raise ValueError("Batch sem mencoes processadas")

        metrics = EnrichmentService.aggregate(mentions)

        critical_terms_counter: Counter[str] = Counter()
        for mention in mentions:
            for term in mention.get("critical_terms") or []:
                critical_terms_counter[term] += 1

        max_samples = max(1, int(settings.LLM_MAX_SAMPLE_MENTIONS))
        sample_mentions = []
        for mention in mentions[:max_samples]:
            sample_mentions.append(
                {
                    "text": (mention.get("text") or "")[:500],
                    "sentiment": mention.get("sentiment"),
                    "criticality": mention.get("criticality"),
                    "urgency_score": mention.get("urgency_score"),
                    "source": mention.get("source"),
                }
            )

        published = [m.get("published_at") for m in mentions if isinstance(m.get("published_at"), datetime)]
        if published:
            period = f"{min(published).date().isoformat()}/{max(published).date().isoformat()}"
        else:
            period = "indefinido"

        return {
            "batch_id": batch_id,
            "brand": batch.get("brand") or "indefinida",
            "period": period,
            "total_comments": len(mentions),
            "sentiment_distribution": metrics.get("sentiment_distribution", {}),
            "critical_mentions": int(metrics.get("critical_mentions", 0)),
            "average_urgency": float(metrics.get("average_urgency", 0)),
            "top_aspects": list((metrics.get("top_aspects") or {}).keys())[:10],
            "top_critical_terms": [term for term, _ in critical_terms_counter.most_common(10)],
            "sample_mentions": sample_mentions,
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
            batch_id = str(job.get("batch_id", ""))

            claimed = db.insight_jobs.update_one(
                {"_id": job_id, "status": "queued"},
                {"$set": {"status": "processing", "updated_at": utcnow()}},
            )
            if claimed.modified_count == 0:
                continue

            try:
                snapshot = InsightService._build_snapshot(user_id=user_id, batch_id=batch_id)
                analysis = await LLMService.analyze_snapshot(snapshot)

                now = utcnow()
                insight_id = f"insight_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
                insight_doc = {
                    "_id": insight_id,
                    "insight_id": insight_id,
                    "job_id": job_id,
                    "user_id": user_id,
                    "batch_id": batch_id,
                    "trigger": job.get("trigger", "auto"),
                    "archived": False,
                    "snapshot": snapshot,
                    "executive_summary": analysis.get("executive_summary"),
                    "sentiment_overview": analysis.get("sentiment_overview"),
                    "risks": analysis.get("risks", []),
                    "opportunities": analysis.get("opportunities", []),
                    "recommended_actions": analysis.get("recommended_actions", []),
                    "decision_guidance": analysis.get("decision_guidance"),
                    "trend": analysis.get("trend", "indefinido"),
                    "llm_payload": analysis,
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
    ) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        query: dict[str, Any] = {"user_id": user_id}
        if not include_archived:
            query["archived"] = False
        if batch_id:
            query["batch_id"] = batch_id

        items = list(
            db.insights.find(query).sort("created_at", -1).limit(max(1, min(limit, 200)))
        )
        return SearchService.serialize_many(items)

    @staticmethod
    async def generate_insight(
        user_id: str,
        batch_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        target_batch_id = batch_id
        if not target_batch_id:
            threshold = InsightService.get_threshold(user_id=user_id)
            candidate = db.comment_batches.find_one(
                {
                    "user_id": user_id,
                    "processed_count": {"$gte": threshold},
                },
                sort=[("updated_at", -1)],
            )
            if not candidate:
                raise ValueError("Nenhum batch atende o limiar para gerar insight")
            target_batch_id = str(candidate.get("batch_id"))

        enqueue_result = InsightService.enqueue_job_if_threshold_reached(
            user_id=user_id,
            batch_id=target_batch_id,
            trigger="manual",
            force=force,
        )

        if not enqueue_result.get("queued") and not force:
            reason = enqueue_result.get("reason", "nao_enfileirado")
            raise ValueError(f"Nao foi possivel enfileirar insight: {reason}")

        await InsightService.process_queued_jobs(limit=1)

        generated = db.insights.find_one(
            {
                "user_id": user_id,
                "batch_id": target_batch_id,
            },
            sort=[("created_at", -1)],
        )
        if not generated:
            raise RuntimeError("Insight nao foi gerado")
        return SearchService.serialize(generated)

    @staticmethod
    async def regenerate_insight(user_id: str, insight_id: str) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        current = db.insights.find_one({"_id": insight_id, "user_id": user_id})
        if not current:
            raise ValueError("Insight nao encontrado")

        batch_id = str(current.get("batch_id"))
        enqueue_result = InsightService.enqueue_job_if_threshold_reached(
            user_id=user_id,
            batch_id=batch_id,
            trigger="regenerate",
            force=True,
        )
        if not enqueue_result.get("queued"):
            raise RuntimeError("Nao foi possivel enfileirar regeneracao")

        await InsightService.process_queued_jobs(limit=1)

        regenerated = db.insights.find_one(
            {"user_id": user_id, "batch_id": batch_id},
            sort=[("created_at", -1)],
        )
        if not regenerated:
            raise RuntimeError("Insight nao foi regenerado")
        return SearchService.serialize(regenerated)

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
