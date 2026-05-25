from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth_utils import get_current_user
from app.schemas import DemoSeedPayload
from app.services.demo_service import DemoService

router = APIRouter()


@router.post("/seed")
async def seed_demo_data(
    payload: DemoSeedPayload,
    sync_to_primary: bool = Query(True),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        result = DemoService.seed_demo_data(user_id=user_id, payload=payload)
        sync_result = {"synced": 0, "total": 0, "contexts": []}
        if sync_to_primary:
            sync_result = DemoService.sync_demo_snapshots_to_primary(user_id=user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return {
        "status": "ok",
        "inserted": int(result.get("inserted", 0)),
        "updated": int(result.get("updated", 0)),
        "total": int(result.get("total", 0)),
        "synced_contexts": int(sync_result.get("synced", 0)),
        "synced_context_ids": [
            str(item.get("context_id"))
            for item in (sync_result.get("contexts") or [])
            if str(item.get("context_id") or "").strip()
        ],
        "sync_enabled": bool(sync_to_primary),
    }
