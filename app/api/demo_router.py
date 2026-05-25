# app/api/demo_router.py
from datetime import datetime
from typing import Any, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth_utils import get_current_user
from app.database import get_secondary_db

router = APIRouter()


# --- Schemas internos --------------------------------------------------------

class SeedMention(BaseModel):
    source: str
    sentiment: str
    urgency: str
    text: str
    aspect: Optional[str] = None
    date: Optional[str] = None


class SeedInsight(BaseModel):
    title: str
    summary: str
    priority: str = "media"
    urgency: float = 0.5
    period_from: Optional[datetime] = None
    period_to: Optional[datetime] = None


class SeedDashboardMetrics(BaseModel):
    total_mentions: int = 0
    positive_ratio: float = 0.0
    neutral_ratio: float = 0.0
    negative_ratio: float = 0.0
    average_urgency: float = 0.0
    urgency_evolution: List[dict] = Field(default_factory=list)
    top_negative_aspects: List[dict] = Field(default_factory=list)
    most_cited_aspects: List[dict] = Field(default_factory=list)


class SeedBatch(BaseModel):
    batch_key: str = "default"
    period_from: Optional[datetime] = None
    period_to: Optional[datetime] = None
    dashboard_metrics: Optional[SeedDashboardMetrics] = None
    insights: List[SeedInsight] = Field(default_factory=list)
    mentions: List[SeedMention] = Field(default_factory=list)


class SeedCompany(BaseModel):
    slug: str
    name: str
    segment: Optional[str] = None
    batches: List[SeedBatch] = Field(default_factory=list)


class DemoSeedPayload(BaseModel):
    # Formato estruturado (multiplas empresas/batches)
    companies: Optional[List[SeedCompany]] = None
    # Formato simples (seed direto com mentions flat)
    company_name: Optional[str] = None
    company_slug: Optional[str] = None
    period_label: Optional[str] = None
    mentions: Optional[List[SeedMention]] = None


# --- Helpers ----------------------------------------------------------------

def _normalize_slug(raw: str) -> str:
    """Garante sempre lowercase, sem espacos extras - case insensitive."""
    return raw.strip().lower()


def _normalize_name(raw: str) -> str:
    """Preserva capitalizacao original para exibicao (ex: Samsung, nao SAMSUNG)."""
    stripped = raw.strip()
    if stripped == stripped.upper() or stripped == stripped.lower():
        return stripped.title()
    return stripped


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _build_metrics_from_mentions(mentions: List[dict]) -> dict:
    """Deriva metricas basicas a partir de uma lista de mencoes flat."""
    total = len(mentions)
    if total == 0:
        return {}
    pos = sum(1 for m in mentions if str(m.get("sentiment", "")).lower() in ("positivo", "positive", "pos"))
    neg = sum(1 for m in mentions if str(m.get("sentiment", "")).lower() in ("negativo", "negative", "neg"))
    neu = total - pos - neg
    aspects_neg: dict[str, int] = {}
    aspects_all: dict[str, int] = {}
    for m in mentions:
        asp = str(m.get("aspect") or "").strip()
        if asp:
            aspects_all[asp] = aspects_all.get(asp, 0) + 1
            if str(m.get("sentiment", "")).lower() in ("negativo", "negative", "neg"):
                aspects_neg[asp] = aspects_neg.get(asp, 0) + 1
    return {
        "total_mentions": total,
        "positive_ratio": round(pos / total, 4),
        "neutral_ratio": round(neu / total, 4),
        "negative_ratio": round(neg / total, 4),
        "average_urgency": 0.5,
        "urgency_evolution": [],
        "top_negative_aspects": [
            {"label": k, "mentions": v}
            for k, v in sorted(aspects_neg.items(), key=lambda x: -x[1])[:10]
        ],
        "most_cited_aspects": [
            {"label": k, "mentions": v}
            for k, v in sorted(aspects_all.items(), key=lambda x: -x[1])[:10]
        ],
    }


# --- Endpoint ----------------------------------------------------------------

@router.post("/seed")
async def seed_demo_data(
    payload: DemoSeedPayload,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    Insere dados de seed no Secondary MongoDB Atlas.
    Aceita dois formatos:
      1. { companies: [...] } - estruturado com batches
      2. { company_name, company_slug, period_label, mentions: [...] } - flat
    """
    try:
        secondary_db = get_secondary_db()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Secondary MongoDB nao configurado. Verifique SECONDARY_MONGODB_URI no .env.",
        ) from exc

    collection = secondary_db["demo_dashboard_snapshots"]
    user_id = str(current_user.get("_id") or current_user.get("id"))
    now = datetime.utcnow()
    upserted_count = 0

    # -- Formato flat (seed simples) ------------------------------------------
    if payload.company_name and not payload.companies:
        company_slug = _normalize_slug(payload.company_slug or payload.company_name)
        company_name = _normalize_name(payload.company_name)
        mentions_raw = [_model_dump(m) for m in (payload.mentions or [])]
        metrics = _build_metrics_from_mentions(mentions_raw)
        doc = {
            "user_id": user_id,
            "company_slug": company_slug,
            "company_name": company_name,
            "segment": None,
            "batch_key": "default",
            "period_label": payload.period_label or "Ultimos 30 dias",
            "period_from": now,
            "period_to": now,
            "metrics": metrics,
            "mentions": mentions_raw,
            "insights": [],
            "created_at": now,
            "updated_at": now,
        }
        collection.update_one(
            {"user_id": user_id, "company_slug": company_slug, "batch_key": "default"},
            {"$set": doc},
            upsert=True,
        )
        upserted_count += 1

    # -- Formato estruturado (multiplas empresas + batches) -------------------
    for company in (payload.companies or []):
        company_slug = _normalize_slug(company.slug)
        company_name = _normalize_name(company.name)

        for batch in company.batches:
            mentions_raw = [_model_dump(m) for m in batch.mentions]
            metrics = (
                _model_dump(batch.dashboard_metrics)
                if batch.dashboard_metrics
                else _build_metrics_from_mentions(mentions_raw)
            )
            doc = {
                "user_id": user_id,
                "company_slug": company_slug,
                "company_name": company_name,
                "segment": company.segment,
                "batch_key": batch.batch_key,
                "period_from": batch.period_from or now,
                "period_to": batch.period_to or now,
                "metrics": metrics,
                "mentions": mentions_raw,
                "insights": [_model_dump(i) for i in batch.insights],
                "created_at": now,
                "updated_at": now,
            }
            collection.update_one(
                {"user_id": user_id, "company_slug": company_slug, "batch_key": batch.batch_key},
                {"$set": doc},
                upsert=True,
            )
            upserted_count += 1

    if upserted_count == 0:
        raise HTTPException(status_code=400, detail="Nenhuma empresa ou mencao encontrada no payload.")

    return {"status": "ok", "inserted": upserted_count}
