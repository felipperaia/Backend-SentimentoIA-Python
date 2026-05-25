from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth_utils import get_current_user
from app.database import get_secondary_db

router = APIRouter()


@router.get("/companies")
async def list_companies(current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))

    try:
        secondary_db = get_secondary_db()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Secondary MongoDB not configured",
        ) from exc

    collection = secondary_db.demo_dashboard_snapshots
    pipeline = [
        {"$match": {"user_id": user_id}},
        {
            "$group": {
                "_id": {"$toLower": "$company_slug"},
                "company_name": {"$first": "$company_name"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "company_id": "$_id",
                "name": "$company_name",
                "slug": "$_id",
            }
        },
        {"$sort": {"name": 1}},
    ]

    companies = list(collection.aggregate(pipeline))
    return {"companies": companies}
