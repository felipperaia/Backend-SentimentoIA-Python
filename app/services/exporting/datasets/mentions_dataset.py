from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import get_db
from app.services.company_utils import normalize_company_filter
from app.services.enrichment_service import EnrichmentService
from app.services.exporting.contracts import ExportContext


def _effective_period_range(
    *,
    period_from: datetime | None,
    period_to: datetime | None,
    period_days: int | None,
) -> tuple[datetime | None, datetime | None]:
    if isinstance(period_from, datetime) or isinstance(period_to, datetime):
        return period_from, period_to
    if period_days and int(period_days) > 0:
        now = datetime.now(timezone.utc)
        return now - timedelta(days=int(period_days)), now
    return None, None


def _resolve_scope(context: ExportContext) -> tuple[str, datetime | None, datetime | None]:
    normalized_company_slug = normalize_company_filter(
        company_slug=context.company_slug,
        company_id=context.company_id,
    )
    if not normalized_company_slug:
        raise ValueError("companySlug e obrigatorio para exportacao de relatorios")

    effective_period_from, effective_period_to = _effective_period_range(
        period_from=context.period_from,
        period_to=context.period_to,
        period_days=context.period_days,
    )
    if (
        isinstance(effective_period_from, datetime)
        and isinstance(effective_period_to, datetime)
        and effective_period_from > effective_period_to
    ):
        raise ValueError("Faixa de datas invalida: from deve ser menor ou igual a to")

    return normalized_company_slug, effective_period_from, effective_period_to


def _build_mentions_query(
    *,
    user_id: str,
    company_slug: str,
    period_from: datetime | None,
    period_to: datetime | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"user_id": user_id, "company_slug": company_slug}
    published_filter: dict[str, Any] = {}
    if isinstance(period_from, datetime):
        published_filter["$gte"] = period_from
    if isinstance(period_to, datetime):
        published_filter["$lte"] = period_to
    if published_filter:
        query["published_at"] = published_filter
    return query


def _load_mentions(*, query: dict[str, Any], limit: int = 20000) -> list[dict[str, Any]]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")
    return list(
        db.mentions.find(query, {"raw": 0})
        .sort("published_at", -1)
        .limit(max(1, min(int(limit), 20000)))
    )


def _resolve_company_name(mentions: list[dict[str, Any]], company_slug: str) -> str:
    if mentions:
        first = mentions[0]
        return str(first.get("company_name") or first.get("query") or company_slug)
    return company_slug


def _build_time_series(mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"volume": 0.0, "urgency_sum": 0.0, "count": 0.0})
    for mention in mentions:
        raw_dt = mention.get("published_at") or mention.get("created_at")
        if isinstance(raw_dt, datetime):
            key = raw_dt.strftime("%Y-%m-%d")
        else:
            key = "sem_data"
        urgency_value = float(mention.get("urgency_score") or 0.0)
        bucket = buckets[key]
        bucket["volume"] += 1
        bucket["urgency_sum"] += urgency_value
        bucket["count"] += 1

    series: list[dict[str, Any]] = []
    for date_key in sorted(buckets.keys()):
        item = buckets[date_key]
        count = item["count"] or 1.0
        series.append(
            {
                "label": date_key,
                "volume": int(item["volume"]),
                "avg_urgency": round(item["urgency_sum"] / count, 4),
            }
        )
    return series


def _build_highlights(mentions: list[dict[str, Any]]) -> list[dict[str, str]]:
    sorted_mentions = sorted(
        mentions,
        key=lambda item: float(item.get("urgency_score") or 0.0),
        reverse=True,
    )
    highlights: list[dict[str, str]] = []
    for mention in sorted_mentions[:10]:
        highlights.append(
            {
                "source": str(mention.get("source") or "-")[:24],
                "sentiment": str(mention.get("sentiment") or "-"),
                "criticality": str(mention.get("criticality") or "-"),
                "urgency": f"{float(mention.get('urgency_score') or 0.0):.4f}",
                "text": str(mention.get("text") or "").replace("\n", " ")[:160],
            }
        )
    return highlights


def build_mentions_csv_dataset(context: ExportContext) -> dict[str, Any]:
    company_slug, period_from, period_to = _resolve_scope(context)
    query = _build_mentions_query(
        user_id=context.user_id,
        company_slug=company_slug,
        period_from=period_from,
        period_to=period_to,
    )
    mentions = _load_mentions(query=query)
    if not mentions:
        raise ValueError("Nenhuma mencao encontrada para o filtro informado")

    return {
        "mentions": mentions,
        "company_slug": company_slug,
        "company_name": _resolve_company_name(mentions, company_slug),
        "period_from": period_from,
        "period_to": period_to,
    }


def build_dashboard_dataset(context: ExportContext) -> dict[str, Any]:
    company_slug, period_from, period_to = _resolve_scope(context)
    query = _build_mentions_query(
        user_id=context.user_id,
        company_slug=company_slug,
        period_from=period_from,
        period_to=period_to,
    )
    mentions = _load_mentions(query=query)
    if not mentions:
        raise ValueError("Nenhuma mencao encontrada para o filtro informado")

    metrics = EnrichmentService.aggregate(mentions) if mentions else {}
    source_distribution = metrics.get("source_distribution") or {}
    total_mentions = int(metrics.get("total_mentions", len(mentions)) or len(mentions))

    positive_count = int(metrics.get("positive_mentions", 0) or 0)
    neutral_count = int(metrics.get("neutral_mentions", 0) or 0)
    negative_count = int(metrics.get("negative_mentions", 0) or 0)
    if positive_count + neutral_count + negative_count == 0:
        for mention in mentions:
            sentiment = str(mention.get("sentiment") or "").lower()
            if "pos" in sentiment:
                positive_count += 1
            elif "neg" in sentiment:
                negative_count += 1
            else:
                neutral_count += 1

    summary_lines = [
        f"Empresa: {_resolve_company_name(mentions, company_slug)}",
        f"Periodo analisado: {(period_from.isoformat() if isinstance(period_from, datetime) else '-')} ate {(period_to.isoformat() if isinstance(period_to, datetime) else '-')}",
        f"Total de mencoes: {total_mentions}",
    ]

    return {
        "company_slug": company_slug,
        "company_name": _resolve_company_name(mentions, company_slug),
        "period_from": period_from,
        "period_to": period_to,
        "mentions": mentions,
        "metrics": metrics,
        "summary_lines": summary_lines,
        "kpis": {
            "total_mentions": total_mentions,
            "reputation_score": float(metrics.get("reputation_score", 0.0) or 0.0),
            "trend": str(metrics.get("trend") or "indefinido"),
            "critical_mentions": int(metrics.get("critical_mentions", 0) or 0),
            "average_urgency": float(metrics.get("average_urgency", 0.0) or 0.0),
        },
        "charts": {
            "sentiment_distribution": [
                ("Positivo", positive_count),
                ("Neutro", neutral_count),
                ("Negativo", negative_count),
            ],
            "source_distribution": [
                (str(source), int(count or 0))
                for source, count in sorted(source_distribution.items(), key=lambda item: int(item[1] or 0), reverse=True)[:8]
            ],
            "time_series": _build_time_series(mentions)[-30:],
        },
        "highlights": _build_highlights(mentions),
    }

