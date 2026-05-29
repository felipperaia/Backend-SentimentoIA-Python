from datetime import datetime
from typing import Any

from app.services.exporting.contracts import ExportContext


def _serialize_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ")


def build_csv_raw_template(view_model: dict[str, Any], context: ExportContext) -> dict[str, Any]:
    mentions = view_model.get("mentions") or []
    filename = str(context.extra.get("filename") or "mentions.csv")

    columns = [
        {"key": "user_id", "label": "user_id"},
        {"key": "company_slug", "label": "company_slug"},
        {"key": "company_name", "label": "company_name"},
        {"key": "search_id", "label": "search_id"},
        {"key": "batch_id", "label": "batch_id"},
        {"key": "source", "label": "source"},
        {"key": "author", "label": "author"},
        {"key": "published_at", "label": "published_at"},
        {"key": "created_at", "label": "created_at"},
        {"key": "sentiment", "label": "sentiment"},
        {"key": "criticality", "label": "criticality"},
        {"key": "urgency_score", "label": "urgency_score"},
        {"key": "confidence", "label": "confidence"},
        {"key": "confidence_score", "label": "confidence_score"},
        {"key": "rating", "label": "rating"},
        {"key": "url", "label": "url"},
        {"key": "text", "label": "text"},
        {"key": "external_id", "label": "external_id"},
        {"key": "canonical_url", "label": "canonical_url"},
    ]

    rows: list[dict[str, str]] = []
    for mention in mentions:
        rows.append(
            {
                "user_id": str(mention.get("user_id") or context.user_id),
                "company_slug": str(mention.get("company_slug") or view_model.get("company_slug") or ""),
                "company_name": str(mention.get("company_name") or view_model.get("company_name") or ""),
                "search_id": str(mention.get("search_id") or ""),
                "batch_id": str(mention.get("batch_id") or ""),
                "source": str(mention.get("source") or ""),
                "author": str(mention.get("author") or ""),
                "published_at": _serialize_datetime(mention.get("published_at")),
                "created_at": _serialize_datetime(mention.get("created_at")),
                "sentiment": str(mention.get("sentiment") or ""),
                "criticality": str(mention.get("criticality") or ""),
                "urgency_score": str(mention.get("urgency_score") or ""),
                "confidence": str(mention.get("confidence") or mention.get("confidence_score") or ""),
                "confidence_score": str(mention.get("confidence_score") or mention.get("confidence") or ""),
                "rating": str(mention.get("rating") or ""),
                "url": str(mention.get("url") or ""),
                "text": _clean_text(mention.get("text")),
                "external_id": str(mention.get("external_id") or ""),
                "canonical_url": str(mention.get("canonical_url") or ""),
            }
        )

    return {"columns": columns, "rows": rows, "filename": filename}

