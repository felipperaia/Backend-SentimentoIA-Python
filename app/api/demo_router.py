from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth_utils import get_current_user
from app.database import get_secondary_db
from app.schemas.demo import DemoSeedPayload

router = APIRouter()


@router.post("/seed")
async def seed_demo_data(
    payload: DemoSeedPayload,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Recebe JSON de seed e popula snapshots demo no MongoDB secundario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))

    try:
        target_db = get_secondary_db()
        collection = target_db.demo_dashboard_snapshots
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Secondary MongoDB not configured",
        ) from exc

    now = datetime.now(timezone.utc)
    inserted = 0

    for company in payload.companies:
        company_name_normalized = str(company.name or "").strip()
        company_slug_normalized = str(company.slug or "").strip().lower()
        if not company_slug_normalized:
            continue

        for batch in company.batches:
            metrics = batch.dashboard_metrics.model_dump()
            insights_raw = [i.model_dump() for i in batch.insights]
            mentions_raw = [dict(item) for item in (batch.mentions or [])]

            doc = {
                "user_id": user_id,
                "company_slug": company_slug_normalized,
                "company_name": company_name_normalized,
                "segment": company.segment,
                "batch_key": batch.batch_key,
                "period_from": batch.period_from,
                "period_to": batch.period_to,
                "mentions": mentions_raw,
                "metrics": metrics,
                "insights": insights_raw,
                "created_at": now,
                "updated_at": now,
            }

            collection.update_one(
                {
                    "user_id": user_id,
                    "company_slug": company_slug_normalized,
                    "batch_key": batch.batch_key,
                },
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            inserted += 1

    return {"status": "ok", "inserted": inserted}
