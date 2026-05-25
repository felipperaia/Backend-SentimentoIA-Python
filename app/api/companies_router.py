# app/api/companies_router.py
from typing import Any
from fastapi import APIRouter, Depends
from app.auth_utils import get_current_user
from app.database import get_secondary_db

router = APIRouter()

@router.get("/companies")
async def list_companies(current_user: dict[str, Any] = Depends(get_current_user)):
    """Lista empresas unicas cadastradas no Secondary MongoDB para este usuario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))

    try:
        secondary_db = get_secondary_db()
    except Exception:
        return {"companies": []}

    collection = secondary_db["demo_dashboard_snapshots"]
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {
            "_id": "$company_slug",
            "company_name": {"$first": "$company_name"},
            "company_slug": {"$first": "$company_slug"},
        }},
        {"$project": {
            "_id": 0,
            "company_id": "$_id",
            "name": "$company_name",
            "slug": "$company_slug",
        }},
        {"$sort": {"name": 1}},
    ]
    companies = list(collection.aggregate(pipeline))
    return {"companies": companies}
