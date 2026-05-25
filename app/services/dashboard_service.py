from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import re
from typing import Any

from app.database import get_db
from app.services.company_utils import normalize_company_filter, slugify_company
from app.services.demo_service import DemoService
from app.services.enrichment_service import EnrichmentService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


class DashboardService:
    @staticmethod
    def _normalize_negative_sentiment(value: Any) -> bool:
        raw = str(value or "").strip().lower()
        return raw in {"negativo", "negative"}

    @staticmethod
    def _company_clause(company_slug: str | None) -> dict[str, Any] | None:
        if not company_slug:
            return None

        escaped = re.escape(company_slug).replace("\\-", "[-_\\s]*")
        regex_value = f"^{escaped}$"
        return {
            "$or": [
                {"company_slug": company_slug},
                {"query": {"$regex": regex_value, "$options": "i"}},
                {"brand": {"$regex": regex_value, "$options": "i"}},
                {"entity": {"$regex": regex_value, "$options": "i"}},
                {"company_name": {"$regex": regex_value, "$options": "i"}},
            ]
        }

    @staticmethod
    def _build_live_conditions(
        *,
        user_id: str,
        batch_id: str | None,
        company_slug: str | None,
        period_days: int | None,
        period_from: datetime | None,
        period_to: datetime | None,
    ) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = [{"user_id": user_id}]

        if batch_id:
            conditions.append({"$or": [{"batch_id": batch_id}, {"search_id": batch_id}]})
        else:
            conditions.append(
                {
                    "$or": [
                        {"batch_id": {"$exists": True}, "status": "processed"},
                        {"search_id": {"$exists": True}},
                    ]
                }
            )

        company_clause = DashboardService._company_clause(company_slug)
        if company_clause:
            conditions.append(company_clause)

        if period_from or period_to:
            created_range: dict[str, Any] = {}
            if period_from:
                created_range["$gte"] = period_from
            if period_to:
                created_range["$lte"] = period_to
            conditions.append({"created_at": created_range})
        elif period_days and period_days > 0:
            cutoff = utcnow() - timedelta(days=period_days)
            conditions.append({"created_at": {"$gte": cutoff}})

        return conditions

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
            {
                "aspect": aspect,
                "count": int(count),
                "label": aspect,
                "mentions": int(count),
            }
            for aspect, count in counter.most_common(10)
        ]

    @staticmethod
    def _compute_most_cited_aspects(metrics: dict[str, Any]) -> list[dict[str, Any]]:
        top_aspects = metrics.get("top_aspects") if isinstance(metrics.get("top_aspects"), dict) else {}
        return [
            {"label": str(label), "mentions": int(count)}
            for label, count in sorted(top_aspects.items(), key=lambda pair: pair[1], reverse=True)[:10]
        ]

    @staticmethod
    def _add_sentiment_ratios(metrics: dict[str, Any]) -> None:
        distribution = (
            metrics.get("sentiment_distribution")
            if isinstance(metrics.get("sentiment_distribution"), dict)
            else {}
        )
        total = max(0, int(metrics.get("total_mentions", 0) or 0))

        if total <= 0:
            metrics["positive_ratio"] = 0.0
            metrics["neutral_ratio"] = 0.0
            metrics["negative_ratio"] = 0.0
            return

        positive = int(distribution.get("positivo", distribution.get("positive", 0)) or 0)
        neutral = int(distribution.get("neutro", distribution.get("neutral", 0)) or 0)
        negative = int(distribution.get("negativo", distribution.get("negative", 0)) or 0)

        metrics["positive_ratio"] = round(positive / total, 6)
        metrics["neutral_ratio"] = round(neutral / total, 6)
        metrics["negative_ratio"] = round(negative / total, 6)

    @staticmethod
    def _extract_company_name(mention: dict[str, Any]) -> str | None:
        return (
            str(mention.get("company_name") or "").strip()
            or str(mention.get("query") or "").strip()
            or str(mention.get("brand") or "").strip()
            or str(mention.get("entity") or "").strip()
            or None
        )

    @staticmethod
    def _extract_period_label(mentions: list[dict[str, Any]], batch_id: str | None = None) -> str | None:
        if batch_id:
            return batch_id

        values: list[datetime] = []
        for mention in mentions:
            candidate = mention.get("published_at") or mention.get("created_at")
            if isinstance(candidate, datetime):
                values.append(candidate)

        if not values:
            return None

        start = min(values)
        end = max(values)
        return f"{start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"

    @staticmethod
    def _extract_period_iso(mentions: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        values: list[datetime] = []
        for mention in mentions:
            candidate = mention.get("published_at") or mention.get("created_at")
            if isinstance(candidate, datetime):
                values.append(candidate)

        if not values:
            return None, None

        return min(values).isoformat(), max(values).isoformat()

    @staticmethod
    def _empty_dashboard(*, batch_id: str | None, company_slug: str | None, mode: str) -> dict[str, Any]:
        return {
            "search_id": batch_id,
            "batch_id": batch_id,
            "mode": mode,
            "metrics": {
                "total_mentions": 0,
                "total_comments": 0,
                "sentiment_distribution": {},
                "source_distribution": {},
                "sources_distribution": {},
                "top_aspects": {},
                "top_themes": {},
                "urgency_trend": [],
                "urgency_evolution": [],
                "top_negative_aspects": [],
                "most_cited_aspects": [],
                "critical_mentions": 0,
                "average_urgency": 0,
                "positive_ratio": 0.0,
                "neutral_ratio": 0.0,
                "negative_ratio": 0.0,
                "reputation_score": 0,
                "trend": "indefinido",
            },
            "mentions": [],
            "latest_insight": None,
            "alerts": [],
            "errors": [],
            "llm_analysis": None,
            "current_company_name": None,
            "current_company_slug": company_slug,
            "period_label": None,
            "period_from": None,
            "period_to": None,
        }

    @staticmethod
    def get_dashboard(
        user_id: str,
        batch_id: str | None = None,
        period_days: int | None = None,
        limit_mentions: int = 200,
        mode: str = "live",
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "live").strip().lower()
        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)

        if normalized_mode == "demo":
            dashboard_payload = DemoService.build_demo_dashboard(
                user_id=user_id,
                company_slug=normalized_company_slug,
                period_from=period_from,
                period_to=period_to,
            )
            sync_result = DemoService.sync_demo_snapshots_to_primary(
                user_id=user_id,
                company_slug=normalized_company_slug,
                period_from=period_from,
                period_to=period_to,
            )
            dashboard_payload["synced_contexts"] = int(sync_result.get("synced", 0) or 0)
            dashboard_payload["synced_context_ids"] = [
                str(item.get("context_id"))
                for item in (sync_result.get("contexts") or [])
                if str(item.get("context_id") or "").strip()
            ]
            return dashboard_payload

        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        query = {
            "$and": DashboardService._build_live_conditions(
                user_id=user_id,
                batch_id=batch_id,
                company_slug=normalized_company_slug,
                period_days=period_days,
                period_from=period_from,
                period_to=period_to,
            )
        }

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("created_at", -1)
            .limit(max(1, min(limit_mentions, 1000)))
        )

        if not mentions:
            return DashboardService._empty_dashboard(batch_id=batch_id, company_slug=normalized_company_slug, mode="live")

        metrics = EnrichmentService.aggregate(mentions)
        metrics["total_comments"] = metrics.get("total_mentions", 0)
        metrics.setdefault("sources_distribution", metrics.get("source_distribution", {}))
        metrics.setdefault("top_themes", metrics.get("top_aspects", {}))
        metrics["urgency_trend"] = DashboardService._compute_urgency_trend(db=db, query=query)
        metrics["urgency_evolution"] = [
            {"date": item.get("date"), "avg_urgency": item.get("avg_urgency")}
            for item in metrics["urgency_trend"]
        ]
        metrics["top_negative_aspects"] = DashboardService._compute_top_negative_aspects(db=db, query=query)
        metrics["most_cited_aspects"] = DashboardService._compute_most_cited_aspects(metrics)
        DashboardService._add_sentiment_ratios(metrics)

        selected_context_id = batch_id or str(mentions[0].get("batch_id") or mentions[0].get("search_id") or "")

        insight_query: dict[str, Any] = {"user_id": user_id, "archived": False}
        if selected_context_id:
            insight_query["$or"] = [
                {"batch_id": selected_context_id},
                {"search_id": selected_context_id},
                {"context_id": selected_context_id},
            ]
        elif normalized_company_slug:
            insight_query["$or"] = [
                {"company_slug": normalized_company_slug},
                {"snapshot.company_slug": normalized_company_slug},
            ]
        latest_insight = db.insights.find_one(insight_query, sort=[("created_at", -1)])

        llm_analysis = None
        search_job = None
        if selected_context_id:
            search_job = db.search_jobs.find_one(
                {"user_id": user_id, "search_id": selected_context_id, "status": "completed"},
                {"llm_analysis": 1, "errors": 1},
            )
            if search_job and search_job.get("llm_analysis"):
                llm_analysis = search_job["llm_analysis"]

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

        errors = search_job.get("errors") if isinstance(search_job, dict) and isinstance(search_job.get("errors"), list) else []

        current_company_name = DashboardService._extract_company_name(mentions[0])
        current_company_slug = normalize_company_filter(
            company_slug=mentions[0].get("company_slug") if isinstance(mentions[0], dict) else None,
            company_id=current_company_name,
        )
        if not current_company_slug and current_company_name:
            current_company_slug = slugify_company(current_company_name)

        period_label = DashboardService._extract_period_label(mentions=mentions, batch_id=batch_id)
        period_from_iso, period_to_iso = DashboardService._extract_period_iso(mentions=mentions)

        return {
            "search_id": selected_context_id or None,
            "batch_id": selected_context_id or None,
            "mode": "live",
            "metrics": metrics,
            "mentions": SearchService.serialize_many(mentions),
            "latest_insight": SearchService.serialize(latest_insight)
            if latest_insight
            else synthetic_latest_insight,
            "alerts": SearchService.serialize_many(alerts),
            "errors": errors,
            "llm_analysis": llm_analysis,
            "current_company_name": current_company_name,
            "current_company_slug": current_company_slug,
            "period_label": period_label,
            "period_from": period_from_iso,
            "period_to": period_to_iso,
        }

    @staticmethod
    def list_mentions(
        user_id: str,
        batch_id: str | None = None,
        status: str | None = None,
        sentiment: str | None = None,
        limit: int = 100,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)
        conditions = DashboardService._build_live_conditions(
            user_id=user_id,
            batch_id=batch_id,
            company_slug=normalized_company_slug,
            period_days=None,
            period_from=period_from,
            period_to=period_to,
        )

        if status:
            if status == "processed":
                conditions.append({"$or": [{"status": "processed"}, {"status": {"$exists": False}}]})
            else:
                conditions.append({"status": status})

        if sentiment:
            conditions.append({"sentiment": sentiment})

        query = {"$and": conditions}
        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("created_at", -1)
            .limit(max(1, min(limit, 500)))
        )
        return SearchService.serialize_many(mentions)

    @staticmethod
    def aggregate_metrics(
        user_id: str,
        *,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        limit_mentions: int = 2000,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)
        query = {
            "$and": DashboardService._build_live_conditions(
                user_id=user_id,
                batch_id=None,
                company_slug=normalized_company_slug,
                period_days=None,
                period_from=period_from,
                period_to=period_to,
            )
        }

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("created_at", -1)
            .limit(max(1, min(limit_mentions, 5000)))
        )

        if not mentions:
            return {
                "company_id": normalized_company_slug,
                "company_name": None,
                "period_from": period_from.isoformat() if isinstance(period_from, datetime) else None,
                "period_to": period_to.isoformat() if isinstance(period_to, datetime) else None,
                "total_mentions": 0,
                "sentiment_distribution": {"positive": 0.0, "neutral": 0.0, "negative": 0.0},
                "urgency_evolution": [],
                "top_negative_aspects": [],
                "most_cited_aspects": [],
            }

        metrics = EnrichmentService.aggregate(mentions)
        metrics["urgency_trend"] = DashboardService._compute_urgency_trend(db=db, query=query)
        metrics["urgency_evolution"] = [
            {"date": item.get("date"), "avg_urgency": item.get("avg_urgency")}
            for item in metrics["urgency_trend"]
        ]
        metrics["top_negative_aspects"] = DashboardService._compute_top_negative_aspects(db=db, query=query)
        metrics["most_cited_aspects"] = DashboardService._compute_most_cited_aspects(metrics)
        DashboardService._add_sentiment_ratios(metrics)

        company_name = DashboardService._extract_company_name(mentions[0])
        resolved_company_slug = normalize_company_filter(
            company_slug=mentions[0].get("company_slug") if isinstance(mentions[0], dict) else None,
            company_id=company_name,
        ) or normalized_company_slug
        if not resolved_company_slug and company_name:
            resolved_company_slug = slugify_company(company_name)

        period_from_iso, period_to_iso = DashboardService._extract_period_iso(mentions=mentions)

        return {
            "company_id": resolved_company_slug,
            "company_name": company_name,
            "period_from": period_from_iso,
            "period_to": period_to_iso,
            "total_mentions": int(metrics.get("total_mentions", 0) or 0),
            "average_urgency": float(metrics.get("average_urgency", 0) or 0),
            "sentiment_distribution": {
                "positive": float(metrics.get("positive_ratio", 0.0) or 0.0),
                "neutral": float(metrics.get("neutral_ratio", 0.0) or 0.0),
                "negative": float(metrics.get("negative_ratio", 0.0) or 0.0),
            },
            "urgency_evolution": metrics.get("urgency_evolution") or [],
            "top_negative_aspects": [
                {
                    "label": str(item.get("label") or item.get("aspect") or ""),
                    "mentions": int(item.get("mentions", item.get("count", 0)) or 0),
                }
                for item in (metrics.get("top_negative_aspects") or [])
            ],
            "most_cited_aspects": metrics.get("most_cited_aspects") or [],
        }
