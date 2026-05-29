from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.auth_utils import get_current_user
from app.schemas.ingestion import (
    IngestionBatchResponse,
    IngestionCommitRequest,
    IngestionCommitResponse,
    IngestionStagingListResponse,
)
from app.services.ingestion_service import IngestionService, IngestionValidationError

router = APIRouter()
INVALID_PAYLOAD_MESSAGE = "payload de ingestao invalido"


@router.post("/comments", response_model=IngestionBatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_comments(
    payload: Any = Body(...),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    normalized_payload: list[dict[str, Any]]
    if isinstance(payload, list):
        normalized_payload = payload
    elif isinstance(payload, dict):
        mentions = payload.get("mentions")
        comments = payload.get("comments")
        if isinstance(mentions, list):
            normalized_payload = mentions
        elif isinstance(comments, list):
            normalized_payload = comments
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": INVALID_PAYLOAD_MESSAGE,
                    "items": [
                        {
                            "index": 0,
                            "errors": [
                                {
                                    "type": "value_error",
                                    "loc": ["body"],
                                    "msg": "envie um array JSON ou objeto com campo mentions/comments em array",
                                }
                            ],
                        }
                    ],
                },
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": INVALID_PAYLOAD_MESSAGE,
                "items": [
                    {
                        "index": 0,
                        "errors": [
                            {
                                "type": "type_error",
                                "loc": ["body"],
                                "msg": "body deve ser array JSON ou objeto",
                            }
                        ],
                    }
                ],
            },
        )

    try:
        return IngestionService.ingest_comments(user_id=user_id, payload=normalized_payload)
    except IngestionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": INVALID_PAYLOAD_MESSAGE,
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


@router.get("/staging/comments", response_model=IngestionStagingListResponse)
async def list_staging_comments(
    batch_id: str | None = Query(None),
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    source: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return IngestionService.list_staging_comments(
            user_id=user_id,
            batch_id=batch_id,
            company_slug=company_slug or company_id,
            source=source,
            limit=limit,
            offset=offset,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/commit", response_model=IngestionCommitResponse)
async def commit_staging_to_primary(
    payload: IngestionCommitRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return IngestionService.commit_staging_to_primary(
            user_id=user_id,
            batch_id=payload.batch_id,
            staging_ids=payload.staging_ids,
            company_slug=payload.company_slug,
            source=payload.source,
            limit=payload.limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
