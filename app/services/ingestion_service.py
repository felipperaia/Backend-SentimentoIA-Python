from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import ValidationError
from pymongo import UpdateOne

from app.database import get_db, get_secondary_db
from app.schemas.ingestion import (
    IngestionBatchResponse,
    IngestionBatchSummary,
    IngestionComment,
    IngestionCommitResponse,
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
        if validated.location:
            normalized["location"] = str(validated.location)

        source = str(normalized.get("source") or validated.source).strip().lower()
        published_at = normalized.get("published_at") or now

        content_hash = str(normalized.get("content_hash") or "").strip()
        fingerprint = str(normalized.get("text_fingerprint") or "").strip()
        external_id = str(normalized.get("external_id") or "").strip()
        dedupe_seed = content_hash or fingerprint or external_id
        if not dedupe_seed:
            dedupe_seed = hashlib.sha256(f"{source}|{validated.text}".encode("utf-8")).hexdigest()

        staging_hash = hashlib.sha256(f"{company_slug}|{source}|{dedupe_seed}".encode("utf-8")).hexdigest()

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
    def _build_staging_query(
        *,
        user_id: str,
        batch_id: str | None = None,
        company_slug: str | None = None,
        source: str | None = None,
        staging_ids: list[str] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        query: dict[str, Any] = {}

        normalized_batch_id = str(batch_id or "").strip()
        if normalized_batch_id:
            query["batch_id"] = normalized_batch_id

        normalized_company_slug = normalize_company_filter(
            company_slug=company_slug,
            company_id=company_slug,
        )
        if normalized_company_slug:
            query["company_slug"] = normalized_company_slug

        normalized_source = str(source or "").strip().lower()
        if normalized_source:
            query["source"] = normalized_source

        normalized_staging_ids = [str(item or "").strip() for item in (staging_ids or [])]
        normalized_staging_ids = [item for item in normalized_staging_ids if item]
        if normalized_staging_ids:
            object_ids: list[ObjectId] = []
            invalid_ids: list[str] = []
            for item in normalized_staging_ids:
                try:
                    object_ids.append(ObjectId(item))
                except (InvalidId, TypeError):
                    invalid_ids.append(item)

            if invalid_ids:
                raise ValueError("staging_ids contem valores invalidos")

            query["_id"] = {"$in": object_ids}

        return query, normalized_company_slug

    @staticmethod
    def list_staging_comments(
        *,
        user_id: str,
        batch_id: str | None = None,
        company_slug: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        secondary_db = get_secondary_db()
        staging_collection = secondary_db.get_collection(IngestionService.STAGING_COLLECTION)

        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))

        query, _ = IngestionService._build_staging_query(
            user_id=user_id,
            batch_id=batch_id,
            company_slug=company_slug,
            source=source,
        )

        total = int(staging_collection.count_documents(query))
        items = list(
            staging_collection.find(query)
            .sort("published_at", -1)
            .skip(safe_offset)
            .limit(safe_limit)
        )

        return {
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "items": SearchService.serialize_many(items),
        }

    @staticmethod
    def _build_commit_metadata(
        *,
        now,
        batch_id: str | None,
        source: str | None,
        staging_ids: list[str] | None,
        safe_limit: int,
        inserted_count: int,
        sources: list[str],
        company_slug: str | None,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "partial_success": False,
            "message": f"Commit bruto concluido com {inserted_count} mencao(oes).",
            "sources_requested": len(sources),
            "sources_with_data": len(sources),
            "sources_failed": 0,
            "timeout_sources": [],
            "source_status": [],
            "unmapped_error_count": 0,
            "staging_filters": {
                "batch_id": str(batch_id or "").strip() or None,
                "company_slug": company_slug,
                "source": str(source or "").strip().lower() or None,
                "staging_ids": len(staging_ids or []),
                "limit": safe_limit,
                "committed_at": now,
            },
        }

    @staticmethod
    def _prepare_primary_docs(*, selected_items: list[dict[str, Any]], user_id: str, commit_id: str, now) -> list[dict[str, Any]]:
        primary_docs: list[dict[str, Any]] = []
        for item in selected_items:
            doc = dict(item)
            doc.pop("_id", None)
            doc["user_id"] = user_id
            doc["committed_by_user_id"] = user_id
            doc["committed_at"] = now
            doc["commit_id"] = commit_id
            doc["ingestion_mode"] = "raw_commit"
            doc.setdefault("created_at", now)
            doc["updated_at"] = now
            primary_docs.append(doc)
        return primary_docs

    @staticmethod
    def _commit_scope_from_docs(
        *,
        primary_docs: list[dict[str, Any]],
        normalized_company_slug: str | None,
    ) -> tuple[str, str | None, list[str], Any, Any]:
        company_name = str(primary_docs[0].get("company_name") or primary_docs[0].get("query") or "").strip()
        effective_company_slug = str(primary_docs[0].get("company_slug") or "").strip() or normalized_company_slug
        sources = sorted(
            {
                str(item.get("source") or "").strip().lower()
                for item in primary_docs
                if str(item.get("source") or "").strip()
            }
        )

        published_values = [item.get("published_at") for item in primary_docs if item.get("published_at")]
        period_from = min(published_values) if published_values else None
        period_to = max(published_values) if published_values else None
        return company_name, effective_company_slug, sources, period_from, period_to

    @staticmethod
    def _upsert_commit_search_job(
        *,
        primary_db,
        user_id: str,
        commit_id: str,
        company_name: str,
        company_slug: str | None,
        inserted_count: int,
        sources: list[str],
        period_from,
        period_to,
        now,
        status_summary: dict[str, Any],
        staging_filters: dict[str, Any],
    ) -> None:
        primary_db.search_jobs.update_one(
            {"user_id": user_id, "search_id": commit_id},
            {
                "$set": {
                    "search_id": commit_id,
                    "user_id": user_id,
                    "query": company_name,
                    "company_name": company_name,
                    "company_slug": company_slug,
                    "status": "completed",
                    "total": inserted_count,
                    "duplicate_count": 0,
                    "sources": sources,
                    "period_days": 0,
                    "period_from": period_from,
                    "period_to": period_to,
                    "metrics": {},
                    "errors": [],
                    "status_summary": status_summary,
                    "staging_filters": staging_filters,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )

    @staticmethod
    def commit_staging_to_primary(
        *,
        user_id: str,
        batch_id: str | None = None,
        staging_ids: list[str] | None = None,
        company_slug: str | None = None,
        source: str | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        primary_db = get_db()
        if primary_db is None:
            raise RuntimeError("Banco de dados primario indisponivel")

        secondary_db = get_secondary_db()

        safe_limit = max(1, min(int(limit), 20000))
        query, normalized_company_slug = IngestionService._build_staging_query(
            user_id=user_id,
            batch_id=batch_id,
            company_slug=company_slug,
            source=source,
            staging_ids=staging_ids,
        )

        staging_collection = secondary_db.get_collection(IngestionService.STAGING_COLLECTION)

        selected_items = list(
            staging_collection.find(query)
            .sort("published_at", -1)
            .limit(safe_limit)
        )

        now = utcnow()
        commit_id = f"commit_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        selected_count = len(selected_items)

        if selected_count == 0:
            return IngestionCommitResponse(
                commit_id=commit_id,
                status="empty",
                user_id=user_id,
                selected=0,
                inserted=0,
                committed_at=now,
                batch_id=str(batch_id or "").strip() or None,
                company_slug=normalized_company_slug,
                source=str(source or "").strip().lower() or None,
            ).model_dump(mode="json")

        primary_docs = IngestionService._prepare_primary_docs(
            selected_items=selected_items,
            user_id=user_id,
            commit_id=commit_id,
            now=now,
        )

        if primary_docs:
            primary_db.mentions.insert_many(primary_docs, ordered=False)

        inserted_count = len(primary_docs)

        company_name, effective_company_slug, sources, period_from, period_to = IngestionService._commit_scope_from_docs(
            primary_docs=primary_docs,
            normalized_company_slug=normalized_company_slug,
        )

        commit_metadata = IngestionService._build_commit_metadata(
            now=now,
            batch_id=batch_id,
            source=source,
            staging_ids=staging_ids,
            safe_limit=safe_limit,
            inserted_count=inserted_count,
            sources=sources,
            company_slug=effective_company_slug,
        )

        IngestionService._upsert_commit_search_job(
            primary_db=primary_db,
            user_id=user_id,
            commit_id=commit_id,
            company_name=company_name,
            company_slug=effective_company_slug,
            inserted_count=inserted_count,
            sources=sources,
            period_from=period_from,
            period_to=period_to,
            now=now,
            status_summary={
                key: value
                for key, value in commit_metadata.items()
                if key != "staging_filters"
            },
            staging_filters=commit_metadata["staging_filters"],
        )

        return IngestionCommitResponse(
            commit_id=commit_id,
            status="completed",
            user_id=user_id,
            selected=selected_count,
            inserted=inserted_count,
            committed_at=now,
            batch_id=str(batch_id or "").strip() or None,
            company_slug=effective_company_slug or None,
            source=str(source or "").strip().lower() or None,
        ).model_dump(mode="json")

    @staticmethod
    def ingest_comments(user_id: str, payload: list[dict[str, Any]]) -> IngestionBatchResponse:
        secondary_db = get_secondary_db()
        staging_collection = secondary_db.get_collection(IngestionService.STAGING_COLLECTION)
        batch_collection = secondary_db.get_collection(IngestionService.BATCH_COLLECTION)

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
            result = staging_collection.bulk_write(operations, ordered=False)
            inserted_count = int(result.upserted_count)
            duplicate_count = int(result.matched_count)

        batch_collection.insert_one(
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
        batch_collection = secondary_db.get_collection(IngestionService.BATCH_COLLECTION)
        batches = list(
            batch_collection.find({})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [IngestionService._serialize_batch(item) for item in batches]

    @staticmethod
    def get_batch(user_id: str, batch_id: str) -> dict[str, Any] | None:
        secondary_db = get_secondary_db()
        batch_collection = secondary_db.get_collection(IngestionService.BATCH_COLLECTION)
        staging_collection = secondary_db.get_collection(IngestionService.STAGING_COLLECTION)

        batch = batch_collection.find_one({"batch_id": batch_id})
        if not batch:
            return None

        recent_mentions = list(
            staging_collection.find(
                {"batch_id": batch_id},
                {"raw": 0},
            )
            .sort("published_at", -1)
            .limit(20)
        )

        output = IngestionService._serialize_batch(batch)
        output["recent_mentions"] = SearchService.serialize_many(recent_mentions)
        return output
