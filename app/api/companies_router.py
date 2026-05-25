from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth_utils import get_current_user
from app.services.company_service import CompanyService

router = APIRouter()


@router.get("/companies")
async def list_companies(current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return CompanyService.list_companies_for_user(user_id=user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
