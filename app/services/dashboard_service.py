from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

from app.database import get_db
from app.services.company_utils import normalize_company_filter
from app.services.enrichment_service import EnrichmentService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


class DashboardService:
    METRICS_CACHE_COLLECTION = "metrics_cache"

    @staticmethod
    def _to_json_compatible(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): DashboardService._to_json_compatible(item) for key, item in value.items()}
        if isinstance(value, list):
            return [DashboardService._to_json_compatible(item) for item in value]
        return value

    @staticmethod
    def _filters_hash(filters: dict[str, Any] | None = None) -> str:
        payload = DashboardService._to_json_compatible(filters or {})
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _metrics_cache_key(
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
            return now - timedelta(days=int(period_days)), now

        return None, None

    @staticmethod
    def _empty_metrics_payload() -> dict[str, Any]:
        return {
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
        }

    @staticmethod
    def _normalize_negative_sentiment(value: Any) -> bool:
        raw = str(value or "").strip().lower()
        return raw in {"negativo", "negative"}

    @staticmethod
    def _build_live_conditions(
        *,
        user_id: str,
        batch_id: str | None,
        company_slug: str,
        period_from: datetime | None,
        period_to: datetime | None,
    ) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = [{"user_id": user_id}, {"company_slug": company_slug}]

        if batch_id:
            conditions.append({"$or": [{"batch_id": batch_id}, {"search_id": batch_id}]})

        if period_from or period_to:
            published_range: dict[str, Any] = {}
            if period_from:
                published_range["$gte"] = period_from
            if period_to:
                published_range["$lte"] = period_to
            conditions.append({"published_at": published_range})

        return conditions

    @staticmethod
    def _load_cached_metrics(
        *,
        db: Any,
        cache_key: str,
    ) -> dict[str, Any] | None:
        cached = db[DashboardService.METRICS_CACHE_COLLECTION].find_one(
            {"cache_key": cache_key},
            {"_id": 0, "payload": 1},
        )
        payload = cached.get("payload") if isinstance(cached, dict) else None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _save_cached_metrics(
        *,
        db: Any,
        cache_key: str,
        user_id: str,
        company_slug: str,
        period_from: datetime | None,
        period_to: datetime | None,
        filters_hash: str,
        payload: dict[str, Any],
    ) -> None:
        now = utcnow()
        db[DashboardService.METRICS_CACHE_COLLECTION].update_one(
            {"cache_key": cache_key},
            {
                "$set": {
                    "cache_key": cache_key,
                    "user_id": user_id,
                    "company_slug": company_slug,
                    "period_from": period_from,
                    "period_to": period_to,
                    "filters_hash": filters_hash,
                    "payload": payload,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )

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
    def _empty_dashboard(
        *,
        batch_id: str | None,
        company_slug: str | None,
        mode: str,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "search_id": batch_id,
            "batch_id": batch_id,
            "mode": mode,
            "metrics": DashboardService._empty_metrics_payload(),
            "mentions": [],
            "latest_insight": None,
            "alerts": [],
            "errors": [],
            "llm_analysis": None,
            "current_company_name": None,
            "current_company_slug": company_slug,
            "period_label": None,
            "period_from": period_from.isoformat() if isinstance(period_from, datetime) else None,
            "period_to": period_to.isoformat() if isinstance(period_to, datetime) else None,
        }

    @staticmethod
    def _compute_metrics_payload(
        *,
        db: Any,
        query: dict[str, Any],
        mentions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not mentions:
            return DashboardService._empty_metrics_payload()

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

        period_from_iso, period_to_iso = DashboardService._extract_period_iso(mentions=mentions)
        metrics["period_from"] = period_from_iso
        metrics["period_to"] = period_to_iso
        metrics["company_name"] = DashboardService._extract_company_name(mentions[0])
        return metrics

    @staticmethod
    def _get_or_compute_metrics_payload(
        *,
        db: Any,
        user_id: str,
        company_slug: str,
        period_from: datetime | None,
        period_to: datetime | None,
        batch_id: str | None,
        limit_mentions: int,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_filters = {"batch_id": batch_id, **(filters or {})}
        filters_hash = DashboardService._filters_hash(effective_filters)
        cache_key = DashboardService._metrics_cache_key(
            user_id=user_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
            filters_hash=filters_hash,
        )

        cached = DashboardService._load_cached_metrics(db=db, cache_key=cache_key)
        if isinstance(cached, dict):
            return cached

        query = {
            "$and": DashboardService._build_live_conditions(
                user_id=user_id,
                batch_id=batch_id,
                company_slug=company_slug,
                period_from=period_from,
                period_to=period_to,
            )
        }
        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("published_at", -1)
            .limit(max(1, min(limit_mentions, 5000)))
        )

        payload = DashboardService._compute_metrics_payload(db=db, query=query, mentions=mentions)
        if not mentions:
            payload["period_from"] = period_from.isoformat() if isinstance(period_from, datetime) else None
            payload["period_to"] = period_to.isoformat() if isinstance(period_to, datetime) else None
            payload["company_name"] = None

        DashboardService._save_cached_metrics(
            db=db,
            cache_key=cache_key,
            user_id=user_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
            filters_hash=filters_hash,
            payload=payload,
        )
        return payload

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
        del mode
        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)
        effective_period_from, effective_period_to = DashboardService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )

        if not normalized_company_slug:
            return DashboardService._empty_dashboard(
                batch_id=batch_id,
                company_slug=None,
                mode="live",
                period_from=effective_period_from,
                period_to=effective_period_to,
            )

        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            return DashboardService._empty_dashboard(
                batch_id=batch_id,
                company_slug=normalized_company_slug,
                mode="live",
                period_from=effective_period_from,
                period_to=effective_period_to,
            )

        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        query = {
            "$and": DashboardService._build_live_conditions(
                user_id=user_id,
                batch_id=batch_id,
                company_slug=normalized_company_slug,
                period_from=effective_period_from,
                period_to=effective_period_to,
            )
        }

        mentions = list(
            db.mentions.find(query, {"raw": 0})
            .sort("published_at", -1)
            .limit(max(1, min(limit_mentions, 1000)))
        )

        if not mentions:
            return DashboardService._empty_dashboard(
                batch_id=batch_id,
                company_slug=normalized_company_slug,
                mode="live",
                period_from=effective_period_from,
                period_to=effective_period_to,
            )

        metrics = DashboardService._get_or_compute_metrics_payload(
            db=db,
            user_id=user_id,
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
            batch_id=batch_id,
            limit_mentions=5000,
            filters={"scope": "dashboard"},
        )

        selected_context_id = batch_id or str(mentions[0].get("batch_id") or mentions[0].get("search_id") or "")

        insight_query: dict[str, Any] = {
            "user_id": user_id,
            "company_slug": normalized_company_slug,
            "archived": False,
        }
        if isinstance(effective_period_from, datetime):
            insight_query["period_to"] = {"$gte": effective_period_from}
        if isinstance(effective_period_to, datetime):
            insight_query["period_from"] = {"$lte": effective_period_to}
        latest_insight = db.insights.find_one(insight_query, sort=[("created_at", -1)])

        llm_analysis = None
        if isinstance(latest_insight, dict):
            llm_payload = latest_insight.get("llm_payload")
            if isinstance(llm_payload, dict):
                llm_analysis = llm_payload

        alerts: list[dict[str, Any]] = []
        alert_query: dict[str, Any] = {"user_id": user_id, "company_slug": normalized_company_slug}
        if effective_period_from or effective_period_to:
            created_range: dict[str, Any] = {}
            if effective_period_from:
                created_range["$gte"] = effective_period_from
            if effective_period_to:
                created_range["$lte"] = effective_period_to
            alert_query["created_at"] = created_range
        alerts = list(db.alerts.find(alert_query).sort("created_at", -1).limit(100))

        current_company_name = DashboardService._extract_company_name(mentions[0])
        current_company_slug = normalized_company_slug

        period_label = DashboardService._extract_period_label(mentions=mentions, batch_id=None)
        if effective_period_from or effective_period_to:
            start_label = effective_period_from.strftime("%d/%m/%Y") if isinstance(effective_period_from, datetime) else "-"
            end_label = effective_period_to.strftime("%d/%m/%Y") if isinstance(effective_period_to, datetime) else "-"
            period_label = f"{start_label} - {end_label}"

        period_from_iso = effective_period_from.isoformat() if isinstance(effective_period_from, datetime) else metrics.get("period_from")
        period_to_iso = effective_period_to.isoformat() if isinstance(effective_period_to, datetime) else metrics.get("period_to")

        return {
            "search_id": selected_context_id or None,
            "batch_id": selected_context_id or None,
            "mode": "live",
            "metrics": metrics,
            "mentions": SearchService.serialize_many(mentions),
            "latest_insight": SearchService.serialize(latest_insight) if latest_insight else None,
            "alerts": SearchService.serialize_many(alerts),
            "errors": [],
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
        if not normalized_company_slug:
            return []

        if (
            isinstance(period_from, datetime)
            and isinstance(period_to, datetime)
            and period_from > period_to
        ):
            return []

        conditions = DashboardService._build_live_conditions(
            user_id=user_id,
            batch_id=batch_id,
            company_slug=normalized_company_slug,
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
            .sort("published_at", -1)
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
        period_days: int | None = None,
        batch_id: str | None = None,
        filters: dict[str, Any] | None = None,
        limit_mentions: int = 2000,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)
        effective_period_from, effective_period_to = DashboardService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )

        if not normalized_company_slug:
            return {
                "company_id": None,
                "company_name": None,
                "period_from": effective_period_from.isoformat() if isinstance(effective_period_from, datetime) else None,
                "period_to": effective_period_to.isoformat() if isinstance(effective_period_to, datetime) else None,
                "total_mentions": 0,
                "sentiment_distribution": {"positive": 0.0, "neutral": 0.0, "negative": 0.0},
                "urgency_evolution": [],
                "top_negative_aspects": [],
                "most_cited_aspects": [],
            }

        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            return {
                "company_id": normalized_company_slug,
                "company_name": None,
                "period_from": effective_period_from.isoformat(),
                "period_to": effective_period_to.isoformat(),
                "total_mentions": 0,
                "sentiment_distribution": {"positive": 0.0, "neutral": 0.0, "negative": 0.0},
                "urgency_evolution": [],
                "top_negative_aspects": [],
                "most_cited_aspects": [],
            }

        metrics = DashboardService._get_or_compute_metrics_payload(
            db=db,
            user_id=user_id,
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
            batch_id=batch_id,
            limit_mentions=limit_mentions,
            filters={"scope": "metrics", **(filters or {})},
        )

        if include_raw:
            return metrics

        period_from_iso = effective_period_from.isoformat() if isinstance(effective_period_from, datetime) else metrics.get("period_from")
        period_to_iso = effective_period_to.isoformat() if isinstance(effective_period_to, datetime) else metrics.get("period_to")

        return {
            "company_id": normalized_company_slug,
            "company_name": metrics.get("company_name"),
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
