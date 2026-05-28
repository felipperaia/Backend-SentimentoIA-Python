from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.auth_utils import get_current_user
from app.schemas.ingestion import IngestionBatchResponse
from app.services.ingestion_service import IngestionService, IngestionValidationError

router = APIRouter()


@router.post("/comments", response_model=IngestionBatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_comments(
    payload: list[dict[str, Any]] = Body(...),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return IngestionService.ingest_comments(user_id=user_id, payload=payload)
    except IngestionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "payload de ingestao invalido",
                "items": [item.model_dump(mode="json") for item in exc.items],
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/batches")
async def list_ingestion_batches(
    limit: int = Query(20, ge=1, le=100),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return {
            "items": IngestionService.list_batches(user_id=user_id, limit=limit),
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/batches/{batch_id}")
async def get_ingestion_batch(
    batch_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        result = IngestionService.get_batch(user_id=user_id, batch_id=batch_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch nao encontrado")
    return result
