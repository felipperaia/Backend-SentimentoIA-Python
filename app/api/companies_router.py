from typing import Any

from fastapi import APIRouter, Depends

from app.auth_utils import get_current_user
from app.database import get_db

router = APIRouter()


@router.get("/companies")
async def list_companies(current_user: dict[str, Any] = Depends(get_current_user)):
    """Lista empresas (brand_names) vinculadas ao user_id a partir de search_jobs e demo_dashboard_snapshots."""
    db = get_db()
    user_id = str(current_user.get("_id") or current_user.get("id"))

    pipeline = [
        {"$match": {"user_id": user_id, "status": "completed"}},
        {
            "$group": {
                "_id": "$query",
                "company_name": {"$first": "$query"},
                "last_seen": {"$max": "$created_at"},
            }
        },
        {"$sort": {"last_seen": -1}},
        {"$limit": 100},
    ]
    raw_companies = list(db.search_jobs.aggregate(pipeline))

    companies: list[dict[str, str]] = []
    seen_slugs: set[str] = set()
    for item in raw_companies:
        name = str(item.get("company_name") or item.get("_id") or "").strip()
        if not name:
            continue
        slug = name.lower().replace(" ", "-")
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        companies.append(
            {
                "company_id": slug,
                "name": name,
                "slug": slug,
                "source": "live",
            }
        )

    try:
        from app.database import get_secondary_db

        secondary_db = get_secondary_db()
        demo_pipeline = [
            {"$match": {"user_id": user_id}},
            {
                "$group": {
                    "_id": "$company_slug",
                    "company_name": {"$first": "$company_name"},
                    "last_seen": {"$max": "$updated_at"},
                }
            },
            {"$sort": {"last_seen": -1}},
            {"$limit": 100},
        ]
        demo_companies = list(secondary_db.demo_dashboard_snapshots.aggregate(demo_pipeline))
        for item in demo_companies:
            slug = str(item.get("_id") or "").strip()
            name = str(item.get("company_name") or slug).strip()
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            companies.append(
                {
                    "company_id": slug,
                    "name": name,
                    "slug": slug,
                    "source": "demo",
                }
            )
    except Exception:
        pass

    return {"companies": companies, "total": len(companies)}
