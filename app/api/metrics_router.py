from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth_utils import get_current_user
from app.database import get_db
from app.services.normalization_service import utcnow

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
async def get_metrics(
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    period_days: int = Query(30, alias="periodDays", ge=1, le=365),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Retorna métricas agregadas por empresa e período."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Banco de dados indisponivel")

    user_id = str(current_user.get("_id") or current_user.get("id"))

    slug = company_slug or company_id
    now = _utcnow()
    date_from = period_from or (now - timedelta(days=period_days))
    date_to = period_to or now

    match_query: dict[str, Any] = {
        "user_id": user_id,
        "status": "processed",
    }

    if slug:
        slug_text = str(slug).replace("-", " ")
        match_query["$or"] = [
            {"brand_name": {"$regex": slug_text, "$options": "i"}},
            {"query": {"$regex": slug_text, "$options": "i"}},
        ]

    if date_from or date_to:
        date_filter: dict[str, Any] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        match_query["published_at"] = date_filter

    pipeline = [
        {"$match": match_query},
        {
            "$group": {
                "_id": "$sentiment",
                "count": {"$sum": 1},
                "avg_urgency": {"$avg": "$urgency_score"},
            }
        },
    ]

    sentiment_agg = list(db.mentions.aggregate(pipeline))
    total = sum(int(item.get("count", 0) or 0) for item in sentiment_agg)
    sentiment_dist = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
    avg_urgency = 0.0

    for item in sentiment_agg:
        key = str(item.get("_id") or "").lower()
        ratio = round((int(item.get("count", 0) or 0) / total), 4) if total else 0.0
        if key in ("positivo", "positive"):
            sentiment_dist["positive"] = ratio
        elif key in ("negativo", "negative"):
            sentiment_dist["negative"] = ratio
        else:
            sentiment_dist["neutral"] = ratio
        avg_urgency += float(item.get("avg_urgency") or 0.0)

    if sentiment_agg:
        avg_urgency = round(avg_urgency / len(sentiment_agg), 4)

    aspect_pipeline = [
        {"$match": match_query},
        {"$unwind": "$aspects"},
        {"$group": {"_id": "$aspects", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    aspects_raw = list(db.mentions.aggregate(aspect_pipeline))
    most_cited = [{"label": a.get("_id"), "mentions": a.get("count")} for a in aspects_raw if a.get("_id")]

    urgency_pipeline = [
        {"$match": {**match_query, "urgency_score": {"$exists": True, "$ne": None}}},
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$published_at"}},
                "avg_urgency": {"$avg": "$urgency_score"},
            }
        },
        {"$sort": {"_id": 1}},
        {"$limit": 60},
    ]
    urgency_evolution = [
        {"date": u.get("_id"), "avg_urgency": round(float(u.get("avg_urgency") or 0), 4)}
        for u in db.mentions.aggregate(urgency_pipeline)
        if u.get("_id")
    ]

    neg_pipeline = [
        {"$match": {**match_query, "sentiment": {"$in": ["negativo", "negative"]}}},
        {"$unwind": "$aspects"},
        {"$group": {"_id": "$aspects", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    top_negative = [
        {"label": a.get("_id"), "mentions": a.get("count")}
        for a in db.mentions.aggregate(neg_pipeline)
        if a.get("_id")
    ]

    company_name = str(slug).replace("-", " ").title() if slug else "Todas as empresas"

    return {
        "ok": True,
        "company_id": slug,
        "company_name": company_name,
        "period_from": date_from.isoformat() if date_from else None,
        "period_to": date_to.isoformat() if date_to else None,
        "total_mentions": total,
        "average_urgency": avg_urgency,
        "sentiment_distribution": sentiment_dist,
        "urgency_evolution": urgency_evolution,
        "most_cited_aspects": most_cited,
        "top_negative_aspects": top_negative,
    }


def _normalize_sentiment(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "positive": "positivo",
        "positivo": "positivo",
        "negative": "negativo",
        "negativo": "negativo",
        "neutral": "neutro",
        "neutro": "neutro",
    }
    return mapping.get(raw, "neutro")


def _normalize_criticality(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "low": "baixa",
        "baixa": "baixa",
        "medium": "média",
        "media": "média",
        "média": "média",
        "high": "alta",
        "alta": "alta",
        "critical": "crítica",
        "critica": "crítica",
        "crítica": "crítica",
    }
    return mapping.get(raw, "baixa")


def _normalize_source(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "web": "websearch",
        "web_search": "websearch",
        "google": "websearch",
        "x": "twitter",
    }
    return aliases.get(raw, raw or "unknown")


@router.get("/classification")
async def classification_metrics(
    period_days: int = Query(30, ge=1, le=365),
    batch_id: str | None = Query(None),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    db = get_db()
    if db is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Banco de dados indisponivel")

    user_id = str(current_user.get("_id") or current_user.get("id"))

    cutoff = utcnow() - timedelta(days=int(period_days))
    query: dict[str, Any] = {
        "user_id": user_id,
        "created_at": {"$gte": cutoff},
    }

    if batch_id:
        query["$or"] = [{"batch_id": batch_id}, {"search_id": batch_id}]

    projection = {
        "sentiment": 1,
        "criticality": 1,
        "confidence_score": 1,
        "confidence": 1,
        "urgency_score": 1,
        "urgency_factors": 1,
        "aspect_sentiment": 1,
        "source": 1,
    }
    mentions = list(db.mentions.find(query, projection))

    by_sentiment = Counter()
    by_criticality = Counter()
    factor_counter = Counter()
    negative_aspect_counter = Counter()
    source_counter = Counter()

    confidence_values: list[float] = []
    urgency_values: list[float] = []

    for mention in mentions:
        by_sentiment[_normalize_sentiment(mention.get("sentiment"))] += 1
        by_criticality[_normalize_criticality(mention.get("criticality"))] += 1

        source_counter[_normalize_source(mention.get("source"))] += 1

        try:
            confidence_raw = mention.get("confidence_score", mention.get("confidence"))
            if confidence_raw is not None:
                confidence_values.append(max(0.0, min(float(confidence_raw), 1.0)))
        except (TypeError, ValueError):
            pass

        try:
            urgency_raw = mention.get("urgency_score")
            if urgency_raw is not None:
                urgency_values.append(max(0.0, min(float(urgency_raw), 1.0)))
        except (TypeError, ValueError):
            pass

        for factor in mention.get("urgency_factors") or []:
            value = str(factor or "").strip().lower()
            if value:
                factor_counter[value] += 1

        aspect_sentiment = mention.get("aspect_sentiment")
        if isinstance(aspect_sentiment, dict):
            for aspect, sentiment in aspect_sentiment.items():
                if _normalize_sentiment(sentiment) == "negativo":
                    normalized_aspect = str(aspect or "").strip().lower()
                    if normalized_aspect:
                        negative_aspect_counter[normalized_aspect] += 1

    known_sources = [
        "reclameaqui",
        "reddit",
        "youtube",
        "appstore",
        "playstore",
        "glassdoor",
        "trustpilot",
        "mastodon",
        "websearch",
        "twitter",
        "instagram",
        "facebook",
        "yelp",
        "tripadvisor",
        "unknown",
    ]
    sources_coverage = {source: int(source_counter.get(source, 0)) for source in known_sources}

    total = len(mentions)
    avg_confidence = round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else 0.0
    avg_urgency = round(sum(urgency_values) / len(urgency_values), 4) if urgency_values else 0.0

    return {
        "period_days": int(period_days),
        "batch_id": batch_id,
        "total_analyzed": total,
        "by_sentiment": {
            "positivo": int(by_sentiment.get("positivo", 0)),
            "neutro": int(by_sentiment.get("neutro", 0)),
            "negativo": int(by_sentiment.get("negativo", 0)),
        },
        "by_criticality": {
            "baixa": int(by_criticality.get("baixa", 0)),
            "média": int(by_criticality.get("média", 0)),
            "alta": int(by_criticality.get("alta", 0)),
            "crítica": int(by_criticality.get("crítica", 0)),
        },
        "avg_confidence": avg_confidence,
        "avg_urgency": avg_urgency,
        "top_urgency_factors": [
            {"factor": factor, "count": int(count)}
            for factor, count in factor_counter.most_common(10)
        ],
        "top_aspects_negative": [
            {"aspect": aspect, "count": int(count)}
            for aspect, count in negative_aspect_counter.most_common(10)
        ],
        "sources_coverage": sources_coverage,
    }
