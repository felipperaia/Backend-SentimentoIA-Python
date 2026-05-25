from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
from typing import Any

from app.database import get_db, get_secondary_db
from app.schemas import DemoSeedPayload
from app.services.company_utils import normalize_company_filter, slugify_company
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService


class DemoService:
    COLLECTION_NAME = "demo_dashboard_snapshots"

    @staticmethod
    def _collection():
        db = get_secondary_db()
        return db[DemoService.COLLECTION_NAME]

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if raw.endswith("Z"):
                raw = f"{raw[:-1]}+00:00"
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _normalize_ratio(value: Any) -> float:
        numeric = DemoService._coerce_float(value)
        return max(0.0, min(1.0, round(numeric, 6)))

    @staticmethod
    def _normalize_top_aspects(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("aspect") or "").strip()
            if not label:
                continue
            mentions = DemoService._coerce_int(item.get("mentions", item.get("count", 0)))
            normalized.append({"label": label, "mentions": max(0, mentions)})
        return normalized

    @staticmethod
    def _normalize_urgency_evolution(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            point_date = DemoService._coerce_datetime(item.get("date"))
            avg_urgency = DemoService._coerce_float(item.get("avg_urgency", 0.0), default=0.0)
            if point_date is None:
                continue
            normalized.append({"date": point_date, "avg_urgency": round(avg_urgency, 6)})
        return normalized

    @staticmethod
    def _build_period_label(snapshot: dict[str, Any]) -> str | None:
        batch_key = str(snapshot.get("batch_key") or "").strip()
        if batch_key:
            return batch_key

        period_from = DemoService._coerce_datetime(snapshot.get("period_from"))
        period_to = DemoService._coerce_datetime(snapshot.get("period_to"))
        if period_from and period_to:
            return f"{period_from.strftime('%d/%m/%Y')} - {period_to.strftime('%d/%m/%Y')}"
        return None

    @staticmethod
    def _to_datetime_iso(value: Any) -> str | None:
        dt = DemoService._coerce_datetime(value)
        return dt.isoformat() if dt else None

    @staticmethod
    def _build_context_id(user_id: str, snapshot: dict[str, Any]) -> str:
        company_slug = str(snapshot.get("company_slug") or "").strip().lower()
        batch_key = str(snapshot.get("batch_key") or "").strip().lower()
        period_from = DemoService._to_datetime_iso(snapshot.get("period_from")) or ""
        period_to = DemoService._to_datetime_iso(snapshot.get("period_to")) or ""

        raw = "::".join(
            [
                str(user_id or "").strip().lower(),
                company_slug,
                batch_key,
                period_from,
                period_to,
            ]
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"demo_{digest}"

    @staticmethod
    def _safe_metrics(value: Any) -> dict[str, Any]:
        metrics = value if isinstance(value, dict) else {}
        return {
            "total_mentions": max(0, DemoService._coerce_int(metrics.get("total_mentions", 0))),
            "positive_ratio": DemoService._normalize_ratio(metrics.get("positive_ratio", 0.0)),
            "neutral_ratio": DemoService._normalize_ratio(metrics.get("neutral_ratio", 0.0)),
            "negative_ratio": DemoService._normalize_ratio(metrics.get("negative_ratio", 0.0)),
            "average_urgency": max(0.0, min(1.0, DemoService._coerce_float(metrics.get("average_urgency", 0.0)))),
            "urgency_evolution": DemoService._normalize_urgency_evolution(metrics.get("urgency_evolution") or []),
            "top_negative_aspects": DemoService._normalize_top_aspects(metrics.get("top_negative_aspects") or []),
            "most_cited_aspects": DemoService._normalize_top_aspects(metrics.get("most_cited_aspects") or []),
        }

    @staticmethod
    def _extract_latest_insight(snapshot: dict[str, Any]) -> dict[str, Any] | None:
        insights = snapshot.get("insights")
        if not isinstance(insights, list):
            return None

        ranked: list[tuple[float, datetime, dict[str, Any]]] = []
        for item in insights:
            if not isinstance(item, dict):
                continue
            urgency_value = DemoService._coerce_float(item.get("urgency", 0.0), default=0.0)
            timestamp = (
                DemoService._coerce_datetime(item.get("period_to"))
                or DemoService._coerce_datetime(item.get("period_from"))
                or datetime.min
            )
            ranked.append((urgency_value, timestamp, item))

        if not ranked:
            return None

        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return ranked[0][2]

    @staticmethod
    def _build_llm_analysis_from_insight(latest_insight: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(latest_insight, dict):
            return {}

        summary = str(latest_insight.get("summary") or latest_insight.get("title") or "").strip()
        if not summary:
            summary = "Resumo demo sincronizado do banco secundario."

        actions_raw = latest_insight.get("actions") or latest_insight.get("recommended_actions") or []
        actions = [str(item).strip() for item in actions_raw if str(item).strip()][:5]

        return {
            "executive_summary": summary,
            "sentiment_overview": summary,
            "risks": [],
            "opportunities": [],
            "recommended_actions": actions,
            "decision_guidance": summary,
            "trend": "stable",
            "priority": str(latest_insight.get("priority") or "medium").strip().lower() or "medium",
            "resolution": "pending",
        }

    @staticmethod
    def sync_demo_snapshots_to_primary(
        user_id: str,
        *,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        snapshots = DemoService.list_demo_snapshots(
            user_id=user_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
            limit=limit,
        )
        if not snapshots:
            return {
                "synced": 0,
                "total": 0,
                "contexts": [],
            }

        now = utcnow()
        contexts: list[dict[str, Any]] = []

        for snapshot in snapshots:
            company_name = str(snapshot.get("company_name") or "").strip()
            if not company_name:
                company_name = str(snapshot.get("company_slug") or "").strip() or "Empresa"

            resolved_company_slug = normalize_company_filter(
                company_slug=str(snapshot.get("company_slug") or "").strip() or None,
                company_id=company_name,
            )
            if not resolved_company_slug:
                resolved_company_slug = slugify_company(company_name)

            context_id = DemoService._build_context_id(user_id=user_id, snapshot=snapshot)
            metrics = DemoService._safe_metrics(snapshot.get("metrics"))
            latest_insight = DemoService._extract_latest_insight(snapshot)
            llm_analysis = DemoService._build_llm_analysis_from_insight(latest_insight)

            period_from_value = DemoService._coerce_datetime(snapshot.get("period_from"))
            period_to_value = DemoService._coerce_datetime(snapshot.get("period_to"))
            created_at = DemoService._coerce_datetime(snapshot.get("created_at")) or now
            batch_key = str(snapshot.get("batch_key") or "").strip() or context_id

            db.demo_context_links.update_one(
                {
                    "user_id": user_id,
                    "context_id": context_id,
                },
                {
                    "$set": {
                        "user_id": user_id,
                        "context_id": context_id,
                        "company_name": company_name,
                        "company_slug": resolved_company_slug,
                        "batch_key": batch_key,
                        "period_from": period_from_value,
                        "period_to": period_to_value,
                        "secondary_snapshot_id": str(snapshot.get("_id") or ""),
                        "metrics": metrics,
                        "latest_insight": latest_insight or None,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": created_at,
                    },
                },
                upsert=True,
            )

            db.search_jobs.update_one(
                {
                    "user_id": user_id,
                    "search_id": context_id,
                },
                {
                    "$set": {
                        "user_id": user_id,
                        "search_id": context_id,
                        "query": company_name,
                        "company_name": company_name,
                        "company_slug": resolved_company_slug,
                        "status": "completed",
                        "sources": ["demo"],
                        "source_mode": "secondary_seed",
                        "is_demo_seed": True,
                        "batch_key": batch_key,
                        "period_from": period_from_value,
                        "period_to": period_to_value,
                        "results_count": int(metrics.get("total_mentions", 0) or 0),
                        "metrics": metrics,
                        "llm_analysis": llm_analysis,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": created_at,
                    },
                },
                upsert=True,
            )

            if latest_insight:
                insight_id = f"demo_insight_{context_id}"
                summary_text = str(latest_insight.get("summary") or latest_insight.get("title") or "").strip()
                if not summary_text:
                    summary_text = "Resumo demo sincronizado do banco secundario."

                db.insights.update_one(
                    {
                        "user_id": user_id,
                        "insight_id": insight_id,
                    },
                    {
                        "$set": {
                            "user_id": user_id,
                            "insight_id": insight_id,
                            "context_id": context_id,
                            "context_type": "search",
                            "search_id": context_id,
                            "batch_id": context_id,
                            "company_name": company_name,
                            "company_slug": resolved_company_slug,
                            "period_from": period_from_value,
                            "period_to": period_to_value,
                            "priority": str(latest_insight.get("priority") or "medium").strip().lower() or "medium",
                            "urgency": str(latest_insight.get("urgency") or "medium"),
                            "executive_summary": summary_text,
                            "recommended_actions": llm_analysis.get("recommended_actions") or [],
                            "trend": str(llm_analysis.get("trend") or "stable"),
                            "status": "open",
                            "resolution": "pending",
                            "archived": False,
                            "snapshot": {
                                "company_name": company_name,
                                "company_slug": resolved_company_slug,
                                "total_mentions": int(metrics.get("total_mentions", 0) or 0),
                                "period_from": period_from_value,
                                "period_to": period_to_value,
                            },
                            "updated_at": now,
                        },
                        "$setOnInsert": {
                            "created_at": created_at,
                        },
                    },
                    upsert=True,
                )

            contexts.append(
                {
                    "context_id": context_id,
                    "company_slug": resolved_company_slug,
                    "batch_key": batch_key,
                }
            )

        return {
            "synced": len(contexts),
            "total": len(snapshots),
            "contexts": contexts,
        }

    @staticmethod
    def seed_demo_data(user_id: str, payload: DemoSeedPayload) -> dict[str, int]:
        collection = DemoService._collection()
        now = utcnow()

        inserted = 0
        updated = 0

        for company in payload.companies:
            company_slug = normalize_company_filter(company_slug=company.slug) or slugify_company(company.name)
            company_name = str(company.name or "").strip() or company_slug
            segment = str(company.segment).strip() if company.segment is not None else None

            for batch in company.batches:
                metrics = batch.dashboard_metrics
                normalized_doc = {
                    "user_id": user_id,
                    "company_slug": company_slug,
                    "company_name": company_name,
                    "segment": segment,
                    "batch_key": str(batch.batch_key),
                    "period_from": batch.period_from,
                    "period_to": batch.period_to,
                    "metrics": {
                        "total_mentions": max(0, int(metrics.total_mentions)),
                        "positive_ratio": DemoService._normalize_ratio(metrics.positive_ratio),
                        "neutral_ratio": DemoService._normalize_ratio(metrics.neutral_ratio),
                        "negative_ratio": DemoService._normalize_ratio(metrics.negative_ratio),
                        "average_urgency": max(0.0, min(1.0, DemoService._coerce_float(metrics.average_urgency))),
                        "urgency_evolution": DemoService._normalize_urgency_evolution(metrics.urgency_evolution),
                        "top_negative_aspects": DemoService._normalize_top_aspects(metrics.top_negative_aspects),
                        "most_cited_aspects": DemoService._normalize_top_aspects(metrics.most_cited_aspects),
                    },
                    "insights": [
                        {
                            "title": str(insight.title),
                            "summary": str(insight.summary),
                            "priority": str(insight.priority),
                            "urgency": DemoService._coerce_float(insight.urgency),
                            "period_from": insight.period_from,
                            "period_to": insight.period_to,
                        }
                        for insight in batch.insights
                    ],
                    "updated_at": now,
                }

                result = collection.update_one(
                    {
                        "user_id": user_id,
                        "company_slug": company_slug,
                        "batch_key": str(batch.batch_key),
                    },
                    {
                        "$set": normalized_doc,
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )

                if result.upserted_id:
                    inserted += 1
                elif result.modified_count > 0 or result.matched_count > 0:
                    updated += 1

        return {
            "inserted": int(inserted),
            "updated": int(updated),
            "total": int(inserted + updated),
        }

    @staticmethod
    def list_demo_snapshots(
        user_id: str,
        *,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        collection = DemoService._collection()

        query: dict[str, Any] = {"user_id": user_id}
        if company_slug:
            query["company_slug"] = company_slug

        overlap_conditions: list[dict[str, Any]] = []
        if period_from:
            overlap_conditions.append({"period_to": {"$gte": period_from}})
        if period_to:
            overlap_conditions.append({"period_from": {"$lte": period_to}})
        if overlap_conditions:
            query["$and"] = overlap_conditions

        snapshots = list(
            collection.find(query)
            .sort("updated_at", -1)
            .limit(max(1, min(limit, 1000)))
        )
        return snapshots

    @staticmethod
    def _aggregate_ratios(snapshots: list[dict[str, Any]], key: str) -> float:
        weighted_sum = 0.0
        weight_total = 0

        for snapshot in snapshots:
            metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
            weight = max(1, DemoService._coerce_int(metrics.get("total_mentions", 0), default=0))
            value = DemoService._normalize_ratio(metrics.get(key, 0.0))
            weighted_sum += value * weight
            weight_total += weight

        if weight_total <= 0:
            return 0.0
        return round(weighted_sum / weight_total, 6)

    @staticmethod
    def _aggregate_average_urgency(snapshots: list[dict[str, Any]]) -> float:
        weighted_sum = 0.0
        weight_total = 0

        for snapshot in snapshots:
            metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
            weight = max(1, DemoService._coerce_int(metrics.get("total_mentions", 0), default=0))
            urgency = max(0.0, min(1.0, DemoService._coerce_float(metrics.get("average_urgency", 0.0))))
            weighted_sum += urgency * weight
            weight_total += weight

        if weight_total <= 0:
            return 0.0
        return round(weighted_sum / weight_total, 6)

    @staticmethod
    def _aggregate_urgency_evolution(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[float]] = defaultdict(list)

        for snapshot in snapshots:
            metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
            for item in metrics.get("urgency_evolution") or []:
                if not isinstance(item, dict):
                    continue
                dt = DemoService._coerce_datetime(item.get("date"))
                if not dt:
                    continue
                bucket_key = dt.date().isoformat()
                buckets[bucket_key].append(DemoService._coerce_float(item.get("avg_urgency", 0.0)))

        output: list[dict[str, Any]] = []
        for bucket_key in sorted(buckets.keys()):
            values = buckets[bucket_key]
            avg_value = sum(values) / len(values) if values else 0.0
            output.append(
                {
                    "date": bucket_key,
                    "avg_urgency": round(avg_value, 6),
                }
            )
        return output

    @staticmethod
    def _aggregate_top_items(snapshots: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        counter: dict[str, int] = defaultdict(int)

        for snapshot in snapshots:
            metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
            entries = metrics.get(key)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or item.get("aspect") or "").strip()
                if not label:
                    continue
                counter[label] += max(0, DemoService._coerce_int(item.get("mentions", item.get("count", 0))))

        ranked = sorted(counter.items(), key=lambda pair: pair[1], reverse=True)
        return [{"label": label, "mentions": int(count)} for label, count in ranked[:10]]

    @staticmethod
    def build_demo_dashboard(
        user_id: str,
        *,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> dict[str, Any]:
        snapshots = DemoService.list_demo_snapshots(
            user_id=user_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
        )

        if not snapshots:
            return {
                "search_id": None,
                "batch_id": None,
                "mode": "demo",
                "metrics": {
                    "total_mentions": 0,
                    "positive_ratio": 0.0,
                    "neutral_ratio": 0.0,
                    "negative_ratio": 0.0,
                    "average_urgency": 0.0,
                    "urgency_evolution": [],
                    "top_negative_aspects": [],
                    "most_cited_aspects": [],
                },
                "mentions": [],
                "latest_insight": None,
                "alerts": [],
                "errors": [],
                "llm_analysis": None,
                "current_company_name": None,
                "current_company_slug": company_slug,
                "period_label": None,
            }

        snapshots_sorted = sorted(
            snapshots,
            key=lambda item: item.get("updated_at") or item.get("created_at") or datetime.min,
            reverse=True,
        )
        latest = snapshots_sorted[0]

        total_mentions = sum(
            max(0, DemoService._coerce_int((snapshot.get("metrics") or {}).get("total_mentions", 0)))
            for snapshot in snapshots_sorted
        )

        all_insights: list[dict[str, Any]] = []
        for snapshot in snapshots_sorted:
            insights = snapshot.get("insights")
            if isinstance(insights, list):
                all_insights.extend([item for item in insights if isinstance(item, dict)])

        latest_insight = all_insights[0] if all_insights else None

        period_label = DemoService._build_period_label(latest)
        if not period_label and snapshots_sorted:
            min_from = min(
                [DemoService._coerce_datetime(item.get("period_from")) for item in snapshots_sorted if DemoService._coerce_datetime(item.get("period_from"))],
                default=None,
            )
            max_to = max(
                [DemoService._coerce_datetime(item.get("period_to")) for item in snapshots_sorted if DemoService._coerce_datetime(item.get("period_to"))],
                default=None,
            )
            if min_from and max_to:
                period_label = f"{min_from.strftime('%d/%m/%Y')} - {max_to.strftime('%d/%m/%Y')}"

        response = {
            "search_id": None,
            "batch_id": str(latest.get("batch_key") or "") or None,
            "mode": "demo",
            "metrics": {
                "total_mentions": int(total_mentions),
                "positive_ratio": DemoService._aggregate_ratios(snapshots_sorted, "positive_ratio"),
                "neutral_ratio": DemoService._aggregate_ratios(snapshots_sorted, "neutral_ratio"),
                "negative_ratio": DemoService._aggregate_ratios(snapshots_sorted, "negative_ratio"),
                "average_urgency": DemoService._aggregate_average_urgency(snapshots_sorted),
                "urgency_evolution": DemoService._aggregate_urgency_evolution(snapshots_sorted),
                "top_negative_aspects": DemoService._aggregate_top_items(snapshots_sorted, "top_negative_aspects"),
                "most_cited_aspects": DemoService._aggregate_top_items(snapshots_sorted, "most_cited_aspects"),
            },
            "mentions": [],
            "latest_insight": SearchService.serialize(latest_insight) if latest_insight else None,
            "alerts": [],
            "errors": [],
            "llm_analysis": None,
            "current_company_name": str(latest.get("company_name") or "") or None,
            "current_company_slug": str(latest.get("company_slug") or "") or company_slug,
            "period_label": period_label,
            "period_from": DemoService._to_datetime_iso(latest.get("period_from")),
            "period_to": DemoService._to_datetime_iso(latest.get("period_to")),
        }
        return response

    @staticmethod
    def list_companies_from_demo(user_id: str) -> list[dict[str, str]]:
        snapshots = DemoService.list_demo_snapshots(user_id=user_id, limit=1000)
        seen: dict[str, dict[str, str]] = {}

        for snapshot in snapshots:
            slug = str(snapshot.get("company_slug") or "").strip()
            if not slug:
                continue
            name = str(snapshot.get("company_name") or slug).strip() or slug
            if slug not in seen:
                seen[slug] = {
                    "company_id": slug,
                    "name": name,
                    "slug": slug,
                }

        return sorted(seen.values(), key=lambda item: item["name"].lower())
