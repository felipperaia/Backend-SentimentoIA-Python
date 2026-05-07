from datetime import timedelta
from typing import Any

from app.database import get_db
from app.services.enrichment_service import EnrichmentService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


class DashboardService:
    @staticmethod
    def get_dashboard(
        user_id: str,
        batch_id: str | None = None,
        period_days: int | None = None,
        limit_mentions: int = 200,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        query: dict[str, Any] = {
            "user_id": user_id,
            "status": "processed",
            "batch_id": {"$exists": True},
        }
        if batch_id:
            query["batch_id"] = batch_id

        if period_days and period_days > 0:
            cutoff = utcnow() - timedelta(days=period_days)
            query["created_at"] = {"$gte": cutoff}

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("created_at", -1)
            .limit(max(1, min(limit_mentions, 1000)))
        )

        if not mentions:
            return {
                "search_id": batch_id,
                "batch_id": batch_id,
                "metrics": {
                    "total_mentions": 0,
                    "total_comments": 0,
                    "sentiment_distribution": {},
                    "source_distribution": {},
                    "top_aspects": {},
                    "critical_mentions": 0,
                    "average_urgency": 0,
                    "reputation_score": 0,
                    "trend": "indefinido",
                },
                "mentions": [],
                "latest_insight": None,
            }

        metrics = EnrichmentService.aggregate(mentions)
        metrics["total_comments"] = metrics.get("total_mentions", 0)

        selected_batch_id = batch_id or str(mentions[0].get("batch_id") or "")
        latest_insight_query: dict[str, Any] = {
            "user_id": user_id,
            "archived": False,
        }
        if selected_batch_id:
            latest_insight_query["batch_id"] = selected_batch_id

        latest_insight = db.insights.find_one(latest_insight_query, sort=[("created_at", -1)])

        return {
            "search_id": selected_batch_id or None,
            "batch_id": selected_batch_id or None,
            "metrics": metrics,
            "mentions": SearchService.serialize_many(mentions),
            "latest_insight": SearchService.serialize(latest_insight) if latest_insight else None,
        }

    @staticmethod
    def list_mentions(
        user_id: str,
        batch_id: str | None = None,
        status: str = "processed",
        sentiment: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        query: dict[str, Any] = {
            "user_id": user_id,
            "batch_id": {"$exists": True},
            "status": status,
        }
        if batch_id:
            query["batch_id"] = batch_id
        if sentiment:
            query["sentiment"] = sentiment

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("created_at", -1)
            .limit(max(1, min(limit, 500)))
        )
        return SearchService.serialize_many(mentions)
