from app.database import get_db
from app.services.enrichment_service import EnrichmentService
from app.services.insight_service import InsightService
from app.services.llm_service import LLMService
from app.services.normalization_service import utcnow


class ProcessingService:
    @staticmethod
    def _sync_batch_status(batch_id: str, user_id: str) -> None:
        db = get_db()
        if db is None:
            return

        grouped = db.mentions.aggregate(
            [
                {"$match": {"user_id": user_id, "batch_id": batch_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        counts = {item.get("_id") or "unknown": int(item.get("count", 0)) for item in grouped}

        pending = counts.get("pending", 0)
        processing = counts.get("processing", 0)
        processed = counts.get("processed", 0)
        errors = counts.get("error", 0)

        if pending > 0 or processing > 0:
            batch_status = "processing"
        elif processed > 0 and errors > 0:
            batch_status = "processed_with_errors"
        elif processed > 0:
            batch_status = "processed"
        elif errors > 0:
            batch_status = "error"
        else:
            batch_status = "queued"

        db.comment_batches.update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {
                "$set": {
                    "status": batch_status,
                    "pending_count": pending,
                    "processing_count": processing,
                    "processed_count": processed,
                    "error_count": errors,
                    "updated_at": utcnow(),
                }
            },
        )

    @staticmethod
    async def process_pending_mentions(limit: int = 50) -> dict[str, int]:
        db = get_db()
        if db is None:
            return {"found": 0, "processed": 0, "errors": 0}

        pending_mentions = list(
            db.mentions.find(
                {
                    "status": "pending",
                    "batch_id": {"$exists": True},
                }
            )
            .sort("created_at", 1)
            .limit(limit)
        )

        found = len(pending_mentions)
        processed_count = 0
        error_count = 0
        touched_batches: set[tuple[str, str]] = set()

        for mention in pending_mentions:
            user_id = str(mention.get("user_id", ""))
            batch_id = str(mention.get("batch_id", ""))
            if user_id and batch_id:
                touched_batches.add((batch_id, user_id))

            claimed = db.mentions.update_one(
                {
                    "_id": mention["_id"],
                    "status": "pending",
                },
                {
                    "$set": {
                        "status": "processing",
                        "updated_at": utcnow(),
                    }
                },
            )
            if claimed.modified_count == 0:
                continue

            try:
                text = str(mention.get("text", "")).strip()
                if not text:
                    raise ValueError("comentario sem texto valido")

                enrichment = EnrichmentService.analyze_mention(text, mention.get("rating"))
                llm_analysis = await LLMService.analyze_single_mention(text)

                merged_aspects = list(enrichment.get("aspects") or [])
                for aspect in (llm_analysis.get("aspect_sentiment") or {}).keys():
                    if aspect not in merged_aspects:
                        merged_aspects.append(aspect)

                confidence_score = float(llm_analysis.get("confidence_score", enrichment.get("confidence", 0.55)) or 0.55)
                urgency_score = float(llm_analysis.get("urgency_score", enrichment.get("urgency_score", 0.0)) or 0.0)
                critical_terms = list(enrichment.get("critical_terms") or [])
                for factor in llm_analysis.get("urgency_factors") or []:
                    if factor not in critical_terms:
                        critical_terms.append(factor)

                db.mentions.update_one(
                    {"_id": mention["_id"]},
                    {
                        "$set": {
                            **enrichment,
                            "sentiment": llm_analysis.get("sentiment", enrichment.get("sentiment", "neutro")),
                            "confidence": round(confidence_score, 3),
                            "confidence_score": round(confidence_score, 3),
                            "urgency_score": round(urgency_score, 4),
                            "critical_terms": critical_terms,
                            "criticality": llm_analysis.get("criticality", enrichment.get("criticality", "baixa")),
                            "aspects": merged_aspects,
                            "aspect_sentiment": llm_analysis.get("aspect_sentiment") or {},
                            "urgency_factors": llm_analysis.get("urgency_factors") or [],
                            "summary": llm_analysis.get("summary") or "",
                            "status": "processed",
                            "llm_eligible": True,
                            "processed_at": utcnow(),
                            "updated_at": utcnow(),
                        },
                        "$unset": {"error_message": ""},
                    },
                )
                processed_count += 1
            except Exception as exc:
                db.mentions.update_one(
                    {"_id": mention["_id"]},
                    {
                        "$set": {
                            "status": "error",
                            "error_message": str(exc)[:400],
                            "updated_at": utcnow(),
                        }
                    },
                )
                error_count += 1

        for batch_id, user_id in touched_batches:
            ProcessingService._sync_batch_status(batch_id=batch_id, user_id=user_id)
            # Enfileira insight automatico quando o batch processado atingir o limiar configurado.
            InsightService.enqueue_job_if_threshold_reached(
                user_id=user_id,
                context_id=batch_id,
                trigger="auto",
                force=False,
                context_type="batch",
            )

        return {
            "found": found,
            "processed": processed_count,
            "errors": error_count,
        }
