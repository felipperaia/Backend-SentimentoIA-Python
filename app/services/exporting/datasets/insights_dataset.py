from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import get_db
from app.services.company_utils import normalize_company_filter
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


def _normalize_priority(priority: str | None) -> str | None:
    candidate = str(priority or "").strip().lower().replace(" ", "_")
    if candidate in {"alta", "high", "critica", "critical"}:
        return "high"
    if candidate in {"media", "medium", "moderada", "moderate"}:
        return "medium"
    if candidate in {"baixa", "low", "ok"}:
        return "low"
    return None


def _normalize_resolution(resolution: str | None) -> str | None:
    candidate = str(resolution or "").strip().lower().replace(" ", "_")
    if candidate in {"resolved", "resolvido", "done", "concluido", "concluído"}:
        return "resolved"
    if candidate in {"in_progress", "em_andamento", "processing", "working"}:
        return "in_progress"
    if candidate in {"pending", "pendente", "open", "novo", "new"}:
        return "pending"
    return None


def build_insights_dataset(context: ExportContext) -> dict[str, Any]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")

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

    limit = max(1, min(int(context.limit or 300), 500))
    priority_filter = _normalize_priority(str(context.extra.get("priority") or ""))
    resolution_filter = _normalize_resolution(str(context.extra.get("resolution") or ""))

    conditions: list[dict[str, Any]] = [
        {"user_id": context.user_id},
        {"archived": {"$ne": True}},
        {"company_slug": normalized_company_slug},
    ]
    if priority_filter:
        conditions.append({"priority": priority_filter})
    if resolution_filter:
        conditions.append({"resolution": resolution_filter})
    if isinstance(effective_period_from, datetime):
        conditions.append({"period_to": {"$gte": effective_period_from}})
    if isinstance(effective_period_to, datetime):
        conditions.append({"period_from": {"$lte": effective_period_to}})

    insights = list(
        db.insights.find({"$and": conditions}).sort("created_at", -1).limit(limit)
    )

    risk_count = sum(1 for item in insights if str(item.get("priority") or "").lower() in {"high", "alta"})
    open_count = sum(1 for item in insights if str(item.get("resolution") or "pending").lower() in {"pending", "in_progress"})

    return {
        "company_slug": normalized_company_slug,
        "period_from": effective_period_from,
        "period_to": effective_period_to,
        "insights": insights,
        "total_insights": len(insights),
        "risk_count": risk_count,
        "open_count": open_count,
    }

