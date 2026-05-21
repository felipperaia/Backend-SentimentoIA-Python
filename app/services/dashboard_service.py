from datetime import timedelta
from collections import Counter
from typing import Any

from app.database import get_db
from app.services.enrichment_service import EnrichmentService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


class DashboardService:
    @staticmethod
    def _normalize_negative_sentiment(value: Any) -> bool:
        raw = str(value or "").strip().lower()
        return raw in {"negativo", "negative"}

    @staticmethod
    def _compute_urgency_trend(db: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
        trend_query: dict[str, Any] = {"$and": [query, {"created_at": {"$type": "date"}}]}
        pipeline = [
            {"$match": trend_query},
            {
                "$project": {
                    "day": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$created_at",
                        }
                    },
                    "urgency_score": {"$ifNull": ["$urgency_score", 0]},
                    "criticality_normalized": {
                        "$toLower": {"$ifNull": ["$criticality", ""]}
                    },
                }
            },
            {
                "$group": {
                    "_id": "$day",
                    "avg_urgency": {"$avg": "$urgency_score"},
                    "critical_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$criticality_normalized",
                                        ["crítica", "critica", "critical"],
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]

        items = list(db.mentions.aggregate(pipeline))
        return [
            {
                "date": str(item.get("_id") or ""),
                "avg_urgency": round(float(item.get("avg_urgency") or 0.0), 4),
                "critical_count": int(item.get("critical_count") or 0),
            }
            for item in items
            if str(item.get("_id") or "").strip()
        ]

    @staticmethod
    def _compute_top_negative_aspects(db: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
        counter = Counter()
        cursor = db.mentions.find(query, {"aspect_sentiment": 1})

        for mention in cursor:
            aspect_sentiment = mention.get("aspect_sentiment")
            if not isinstance(aspect_sentiment, dict):
                continue
            for aspect, sentiment in aspect_sentiment.items():
                aspect_name = str(aspect or "").strip().lower()
                if not aspect_name:
                    continue
                if DashboardService._normalize_negative_sentiment(sentiment):
                    counter[aspect_name] += 1

        return [
            {"aspect": aspect, "count": int(count)}
            for aspect, count in counter.most_common(10)
        ]

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

        # Busca por batch_id OU search_id (ambos os fluxos).
        query: dict[str, Any] = {"user_id": user_id}
        if batch_id:
            query["$or"] = [{"batch_id": batch_id}, {"search_id": batch_id}]
        else:
            query["$or"] = [
                {"batch_id": {"$exists": True}, "status": "processed"},
                {"search_id": {"$exists": True}},
            ]

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
                    "sources_distribution": {},
                    "top_aspects": {},
                    "top_themes": {},
                    "urgency_trend": [],
                    "top_negative_aspects": [],
                    "critical_mentions": 0,
                    "average_urgency": 0,
                    "reputation_score": 0,
                    "trend": "indefinido",
                },
                "mentions": [],
                "latest_insight": None,
                "alerts": [],
                "errors": [],
                "llm_analysis": None,
            }

        metrics = EnrichmentService.aggregate(mentions)
        metrics["total_comments"] = metrics.get("total_mentions", 0)
        metrics.setdefault("sources_distribution", metrics.get("source_distribution", {}))
        metrics.setdefault("top_themes", metrics.get("top_aspects", {}))
        metrics["urgency_trend"] = DashboardService._compute_urgency_trend(db=db, query=query)
        metrics["top_negative_aspects"] = DashboardService._compute_top_negative_aspects(db=db, query=query)

        # Identifica batch/search ID do contexto.
        selected_context_id = batch_id
        if not selected_context_id:
            selected_context_id = str(
                mentions[0].get("batch_id") or mentions[0].get("search_id") or ""
            )

        # Busca insight mais recente no mesmo contexto (batch/search/context_id).
        insight_query: dict[str, Any] = {"user_id": user_id, "archived": False}
        if selected_context_id:
            insight_query["$or"] = [
                {"batch_id": selected_context_id},
                {"search_id": selected_context_id},
                {"context_id": selected_context_id},
            ]
        latest_insight = db.insights.find_one(insight_query, sort=[("created_at", -1)])

        # Se nao encontrou insight, tenta buscar llm_analysis do search_job.
        llm_analysis = None
        search_job = None
        if selected_context_id:
            search_job = db.search_jobs.find_one(
                {"user_id": user_id, "search_id": selected_context_id, "status": "completed"},
                {"llm_analysis": 1, "errors": 1},
            )
            if search_job and search_job.get("llm_analysis"):
                llm_analysis = search_job["llm_analysis"]

        # Fallback de latest_insight para manter contrato visual no frontend.
        synthetic_latest_insight: dict[str, Any] | None = None
        if not latest_insight and isinstance(llm_analysis, dict):
            synthetic_latest_insight = {
                "id": f"llm-fallback-{selected_context_id or 'latest'}",
                "insight_id": f"llm-fallback-{selected_context_id or 'latest'}",
                "context_id": selected_context_id,
                "context_type": "search",
                "priority": str(llm_analysis.get("priority") or "medium"),
                "resolution": str(llm_analysis.get("resolution") or "pending"),
                "trend": str(llm_analysis.get("trend") or metrics.get("trend") or "stable"),
                "executive_summary": str(
                    llm_analysis.get("executive_summary")
                    or llm_analysis.get("sentiment_overview")
                    or "Resumo indisponivel no momento"
                ),
                "recommended_actions": llm_analysis.get("recommended_actions")
                if isinstance(llm_analysis.get("recommended_actions"), list)
                else [],
            }

        alerts: list[dict[str, Any]] = []
        if selected_context_id:
            alerts = list(
                db.alerts.find({"user_id": user_id, "search_id": selected_context_id}).sort("created_at", -1).limit(100)
            )

        errors = []
        if isinstance(search_job, dict):
            errors = search_job.get("errors") if isinstance(search_job.get("errors"), list) else []

        return {
            "search_id": selected_context_id or None,
            "batch_id": selected_context_id or None,
            "metrics": metrics,
            "mentions": SearchService.serialize_many(mentions),
            "latest_insight": SearchService.serialize(latest_insight)
            if latest_insight
            else synthetic_latest_insight,
            "alerts": SearchService.serialize_many(alerts),
            "errors": errors,
            "llm_analysis": llm_analysis,
        }

    @staticmethod
    def list_mentions(
        user_id: str,
        batch_id: str | None = None,
        status: str | None = None,
        sentiment: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        conditions: list[dict[str, Any]] = [{"user_id": user_id}]
        if batch_id:
            conditions.append({"$or": [{"batch_id": batch_id}, {"search_id": batch_id}]})
        else:
            conditions.append(
                {
                    "$or": [
                        {"batch_id": {"$exists": True}},
                        {"search_id": {"$exists": True}},
                    ]
                }
            )

        if status:
            # Compatibilidade: mencoes de busca podem nao ter campo status.
            if status == "processed":
                conditions.append({"$or": [{"status": "processed"}, {"status": {"$exists": False}}]})
            else:
                conditions.append({"status": status})

        if sentiment:
            conditions.append({"sentiment": sentiment})

        query: dict[str, Any] = {"$and": conditions}

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("created_at", -1)
            .limit(max(1, min(limit, 500)))
        )
        return SearchService.serialize_many(mentions)
