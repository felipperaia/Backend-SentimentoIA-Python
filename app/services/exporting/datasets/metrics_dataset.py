from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.company_utils import normalize_company_filter
from app.services.dashboard_service import DashboardService
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


def build_metrics_dataset(context: ExportContext) -> dict[str, Any]:
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

    metrics = DashboardService.aggregate_metrics(
        user_id=context.user_id,
        company_slug=normalized_company_slug,
        period_from=effective_period_from,
        period_to=effective_period_to,
        period_days=context.period_days,
        include_raw=False,
    )

    sentiment_distribution = metrics.get("sentiment_distribution") or {}
    positive_ratio = float(sentiment_distribution.get("positive", 0.0) or 0.0)
    neutral_ratio = float(sentiment_distribution.get("neutral", 0.0) or 0.0)
    negative_ratio = float(sentiment_distribution.get("negative", 0.0) or 0.0)

    return {
        "company_slug": normalized_company_slug,
        "company_name": str(metrics.get("company_name") or normalized_company_slug),
        "period_from": effective_period_from,
        "period_to": effective_period_to,
        "total_mentions": int(metrics.get("total_mentions", 0) or 0),
        "average_urgency": float(metrics.get("average_urgency", 0.0) or 0.0),
        "sentiment_distribution": {
            "positive": positive_ratio,
            "neutral": neutral_ratio,
            "negative": negative_ratio,
        },
        "urgency_evolution": metrics.get("urgency_evolution") or [],
        "top_negative_aspects": metrics.get("top_negative_aspects") or [],
        "most_cited_aspects": metrics.get("most_cited_aspects") or [],
    }

