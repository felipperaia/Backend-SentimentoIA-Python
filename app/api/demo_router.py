from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth_utils import get_current_user
from app.database import get_db

router = APIRouter()


class SeedInsight(BaseModel):
    title: str
    summary: str
    priority: str = "media"
    urgency: float = 0.5
    period_from: datetime | None = None
    period_to: datetime | None = None


class SeedDashboardMetrics(BaseModel):
    total_mentions: int = 0
    positive_ratio: float = 0.0
    neutral_ratio: float = 0.0
    negative_ratio: float = 0.0
    average_urgency: float = 0.0
    urgency_evolution: list[dict[str, Any]] = Field(default_factory=list)
    top_negative_aspects: list[dict[str, Any]] = Field(default_factory=list)
    most_cited_aspects: list[dict[str, Any]] = Field(default_factory=list)


class SeedBatch(BaseModel):
    batch_key: str
    period_from: datetime
    period_to: datetime
    dashboard_metrics: SeedDashboardMetrics = Field(default_factory=SeedDashboardMetrics)
    insights: list[SeedInsight] = Field(default_factory=list)


class SeedCompany(BaseModel):
    slug: str
    name: str
    segment: str | None = None
    batches: list[SeedBatch]


class DemoSeedPayload(BaseModel):
    companies: list[SeedCompany]


@router.post("/seed")
async def seed_demo_data(
    payload: DemoSeedPayload,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Recebe JSON de seed e popula o banco (principal ou secundário) com snapshots demo."""
    user_id = str(current_user.get("_id") or current_user.get("id"))

    try:
        from app.database import get_secondary_db

        target_db = get_secondary_db()
        collection = target_db.demo_dashboard_snapshots
    except Exception:
        db = get_db()
        if db is None:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível")
        collection = db.demo_dashboard_snapshots

    now = datetime.now(timezone.utc)
    inserted = 0

    for company in payload.companies:
        for batch in company.batches:
            metrics = batch.dashboard_metrics.model_dump()
            insights_raw = [i.model_dump() for i in batch.insights]

            doc = {
                "user_id": user_id,
                "company_slug": company.slug,
                "company_name": company.name,
                "segment": company.segment,
                "batch_key": batch.batch_key,
                "period_from": batch.period_from,
                "period_to": batch.period_to,
                "metrics": metrics,
                "insights": insights_raw,
                "created_at": now,
                "updated_at": now,
            }

            collection.update_one(
                {
                    "user_id": user_id,
                    "company_slug": company.slug,
                    "batch_key": batch.batch_key,
                },
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            inserted += 1

    return {"status": "ok", "inserted": inserted}
