from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from pymongo import UpdateOne

from app.database import get_secondary_db
from app.schemas.ingestion import (
    IngestionBatchResponse,
    IngestionBatchSummary,
    IngestionComment,
    IngestionRejectedItem,
)
from app.services.company_utils import normalize_company_filter
from app.services.normalization_service import normalize_mention, utcnow
from app.services.search_service import SearchService


class IngestionValidationError(ValueError):
    def __init__(self, items: list[IngestionRejectedItem]):
        super().__init__("Payload de ingestao invalido")
        self.items = items


class IngestionService:
    STAGING_COLLECTION = "ingestion_staging_mentions"
    BATCH_COLLECTION = "ingestion_staging_batches"

    @staticmethod
    def _batch_id() -> str:
        timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
        return f"batch_{timestamp}_{uuid4().hex[:8]}"

    @staticmethod
    def _serialize_batch(batch: dict[str, Any]) -> dict[str, Any]:
        item = SearchService.serialize(batch)
        return IngestionBatchSummary(**item).model_dump(mode="json")

    @staticmethod
    def _validate_items(payload: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], IngestionComment]], list[IngestionRejectedItem]]:
        valid_items: list[tuple[dict[str, Any], IngestionComment]] = []
        rejected_items: list[IngestionRejectedItem] = []

        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                rejected_items.append(
                    IngestionRejectedItem(
                        index=index,
                        errors=[
                            {
                                "type": "type_error",
                                "loc": ["item"],
                                "msg": "cada item do lote deve ser um objeto JSON",
                            }
                        ],
                    )
                )
                continue

            try:
                validated = IngestionComment.model_validate(item)
                valid_items.append((item, validated))
            except ValidationError as exc:
                rejected_items.append(
                    IngestionRejectedItem(
                        index=index,
                        errors=[dict(error) for error in exc.errors()],
                    )
                )

        return valid_items, rejected_items

    @staticmethod
    def _resolve_company(comment: IngestionComment) -> tuple[str, str]:
        company_name = (
            str(comment.company_name or "").strip()
            or str(comment.query or "").strip()
            or str(comment.entity or "").strip()
            or str(comment.company_slug or "").strip()
        )

        company_slug = normalize_company_filter(
            company_slug=comment.company_slug,
            company_id=company_name,
        )
        if not company_slug:
            raise ValueError("nao foi possivel normalizar company_slug")

        return company_name, company_slug

    @staticmethod
    def _build_staging_doc(
        *,
        user_id: str,
        batch_id: str,
        now,
        raw_item: dict[str, Any],
        validated: IngestionComment,
    ) -> dict[str, Any]:
        company_name, company_slug = IngestionService._resolve_company(validated)

        payload_raw = validated.raw if isinstance(validated.raw, dict) else {}
        merged_raw = {
            **payload_raw,
            "ingestion_input": raw_item,
            "ingestion_audit": {
                "user_id": user_id,
                "batch_id": batch_id,
                "company_name": company_name,
                "company_slug": company_slug,
            },
        }

        normalized = normalize_mention(
            query=company_name,
            source=validated.source,
            text=validated.text,
            author=validated.author,
            published_at=validated.published_at,
            url=validated.canonical_url or validated.url,
            rating=validated.rating,
            raw=merged_raw,
        )
        if not normalized:
            raise ValueError("item invalido: texto nao pode estar vazio")

        if validated.external_id:
            normalized["external_id"] = str(validated.external_id)
        if validated.source_item_id:
            normalized["source_item_id"] = str(validated.source_item_id)

        source = str(normalized.get("source") or validated.source).strip().lower()
        published_at = normalized.get("published_at") or now

        content_hash = str(normalized.get("content_hash") or "").strip()
        fingerprint = str(normalized.get("text_fingerprint") or "").strip()
        external_id = str(normalized.get("external_id") or "").strip()
        dedupe_seed = content_hash or fingerprint or external_id
        if not dedupe_seed:
            dedupe_seed = hashlib.sha256(f"{source}|{validated.text}".encode("utf-8")).hexdigest()

        staging_hash = hashlib.sha256(f"{user_id}|{company_slug}|{source}|{dedupe_seed}".encode("utf-8")).hexdigest()

        normalized.update(
            {
                "user_id": user_id,
                "batch_id": batch_id,
                "company_name": company_name,
                "company_slug": company_slug,
                "source": source,
                "published_at": published_at,
                "staging_hash": staging_hash,
                "raw_payload": raw_item,
                "ingested_at": now,
                "updated_at": now,
            }
        )
        return normalized

    @staticmethod
    def ingest_comments(user_id: str, payload: list[dict[str, Any]]) -> IngestionBatchResponse:
        secondary_db = get_secondary_db()

        if not isinstance(payload, list) or not payload:
            raise IngestionValidationError(
                items=[
                    IngestionRejectedItem(
                        index=0,
                        errors=[
                            {
                                "type": "value_error",
                                "loc": ["body"],
                                "msg": "o body deve ser um array JSON com pelo menos um item",
                            }
                        ],
                    )
                ]
            )

        valid_items, rejected_items = IngestionService._validate_items(payload)
        if rejected_items:
            raise IngestionValidationError(items=rejected_items)

        now = utcnow()
        batch_id = IngestionService._batch_id()

        staging_docs: list[dict[str, Any]] = []
        sources: set[str] = set()
        company_slugs: set[str] = set()

        for raw_item, validated in valid_items:
            doc = IngestionService._build_staging_doc(
                user_id=user_id,
                batch_id=batch_id,
                now=now,
                raw_item=raw_item,
                validated=validated,
            )
            sources.add(str(doc.get("source") or "").strip().lower())
            company_slugs.add(str(doc.get("company_slug") or "").strip())
            staging_docs.append(doc)

        operations = [
            UpdateOne(
                {
                    "user_id": user_id,
                    "staging_hash": str(item.get("staging_hash") or ""),
                },
                {
                    "$setOnInsert": item,
                },
                upsert=True,
            )
            for item in staging_docs
        ]

        inserted_count = 0
        duplicate_count = 0
        if operations:
            result = secondary_db[IngestionService.STAGING_COLLECTION].bulk_write(operations, ordered=False)
            inserted_count = int(result.upserted_count)
            duplicate_count = int(result.matched_count)

        secondary_db[IngestionService.BATCH_COLLECTION].insert_one(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "status": "completed",
                "received_count": len(payload),
                "inserted_count": inserted_count,
                "duplicate_count": duplicate_count,
                "company_slugs": sorted(slug for slug in company_slugs if slug),
                "sources": sorted(source for source in sources if source),
                "created_at": now,
                "updated_at": now,
            }
        )

        return IngestionBatchResponse(
            batch_id=batch_id,
            received=len(payload),
            inserted=inserted_count,
            duplicates=duplicate_count,
            status="completed",
        )

    @staticmethod
    def list_batches(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        secondary_db = get_secondary_db()
        batches = list(
            secondary_db[IngestionService.BATCH_COLLECTION]
            .find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [IngestionService._serialize_batch(item) for item in batches]

    @staticmethod
    def get_batch(user_id: str, batch_id: str) -> dict[str, Any] | None:
        secondary_db = get_secondary_db()
        batch = secondary_db[IngestionService.BATCH_COLLECTION].find_one(
            {"user_id": user_id, "batch_id": batch_id}
        )
        if not batch:
            return None

        recent_mentions = list(
            secondary_db[IngestionService.STAGING_COLLECTION]
            .find(
                {"user_id": user_id, "batch_id": batch_id},
                {"raw": 0},
            )
            .sort("published_at", -1)
            .limit(20)
        )

        output = IngestionService._serialize_batch(batch)
        output["recent_mentions"] = SearchService.serialize_many(recent_mentions)
        return output
