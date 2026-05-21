from typing import Any

from app.database import get_db
from app.services.normalization_service import utcnow


def _as_serializable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _db_required() -> Any:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")
    return db


def _find_one(
    db: Any,
    collection_names: list[str],
    query: dict[str, Any],
    projection: dict[str, int],
    sort_field: str = "created_at",
) -> dict[str, Any] | None:
    for name in collection_names:
        doc = db[name].find_one(query, projection, sort=[(sort_field, -1)])
        if doc:
            return doc
    return None


def _find_many(
    db: Any,
    collection_names: list[str],
    query: dict[str, Any],
    projection: dict[str, int],
    limit: int,
    sort_field: str = "created_at",
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 1), 200))
    for name in collection_names:
        items = list(db[name].find(query, projection).sort(sort_field, -1).limit(safe_limit))
        if items:
            return items
    return []


def get_user_dashboard_summary(user_id: str) -> dict[str, Any]:
    """Resumo agregado minimo para chat/insights sem campos sensiveis."""
    db = _db_required()

    projection = {
        "query": 1,
        "search_id": 1,
        "metrics": 1,
        "total": 1,
        "created_at": 1,
        "updated_at": 1,
    }
    doc = _find_one(
        db,
        ["search_jobs", "searchjobs"],
        {"user_id": user_id, "status": "completed"},
        projection,
        sort_field="updated_at",
    )

    if not doc:
        return {
            "last_query": None,
            "search_id": None,
            "total_comments": 0,
            "sentiment_score": 0.0,
            "criticality": "low",
            "sentiment_distribution": {"positive": 0, "negative": 0, "neutral": 0},
            "updated_at": None,
        }

    metrics = doc.get("metrics") if isinstance(doc.get("metrics"), dict) else {}
    distribution = metrics.get("sentiment_distribution") if isinstance(metrics.get("sentiment_distribution"), dict) else {}

    positive = int(distribution.get("positive", distribution.get("positivo", 0)) or 0)
    negative = int(distribution.get("negative", distribution.get("negativo", 0)) or 0)
    neutral = int(distribution.get("neutral", distribution.get("neutro", 0)) or 0)

    total_comments = int(metrics.get("total_comments", metrics.get("total_mentions", doc.get("total", 0))) or 0)
    criticality = str(metrics.get("criticality") or "").strip().lower()
    if criticality not in {"high", "medium", "low"}:
        critical_mentions = int(metrics.get("critical_mentions", 0) or 0)
        if critical_mentions >= 10:
            criticality = "high"
        elif critical_mentions > 0:
            criticality = "medium"
        else:
            criticality = "low"

    updated_at = doc.get("updated_at") or doc.get("created_at")
    return {
        "last_query": doc.get("query"),
        "search_id": doc.get("search_id"),
        "total_comments": total_comments,
        "sentiment_score": float(metrics.get("sentiment_score", metrics.get("reputation_score", 0)) or 0),
        "criticality": criticality,
        "sentiment_distribution": {
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
        },
        "updated_at": _as_serializable(updated_at),
    }


def get_user_recent_mentions(user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Retorna apenas campos necessarios ao entendimento sem metadados sensiveis."""
    db = _db_required()

    projection = {
        "text": 1,
        "source": 1,
        "sentiment": 1,
        "criticality": 1,
        "urgency_score": 1,
        "published_at": 1,
        "created_at": 1,
    }
    items = _find_many(db, ["mentions"], {"user_id": user_id}, projection, limit, sort_field="published_at")

    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "text": str(item.get("text") or "")[:500],
                "source": item.get("source") or "unknown",
                "sentiment": str(item.get("sentiment") or "neutral").lower(),
                "criticality": str(item.get("criticality") or "low").lower(),
                "urgency_score": float(item.get("urgency_score") or 0),
                "published_at": _as_serializable(item.get("published_at") or item.get("created_at")),
            }
        )
    return output


def get_user_open_insights(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    db = _db_required()

    projection = {
        "insight_id": 1,
        "priority": 1,
        "resolution": 1,
        "executive_summary": 1,
        "recommended_actions": 1,
        "created_at": 1,
        "updated_at": 1,
    }
    query = {
        "user_id": user_id,
        "archived": {"$ne": True},
        "$or": [
            {"resolution": {"$in": ["pending", "in_progress"]}},
            {"status": {"$in": ["open", "in_progress"]}},
        ],
    }
    items = _find_many(db, ["insights"], query, projection, limit, sort_field="updated_at")

    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "insight_id": item.get("insight_id") or str(item.get("_id") or ""),
                "priority": str(item.get("priority") or "medium").lower(),
                "resolution": str(item.get("resolution") or "pending").lower(),
                "executive_summary": str(item.get("executive_summary") or "")[:500],
                "recommended_actions": item.get("recommended_actions") if isinstance(item.get("recommended_actions"), list) else [],
                "updated_at": _as_serializable(item.get("updated_at") or item.get("created_at")),
            }
        )
    return output


def get_user_settings_safe(user_id: str) -> dict[str, Any]:
    db = _db_required()

    projection = {
        "theme": 1,
        "locale": 1,
        "insight_threshold": 1,
        "llm_trigger_min_comments": 1,
        "updated_at": 1,
    }
    settings_doc = _find_one(db, ["dashboard_settings", "dashboardsettings"], {"user_id": user_id}, projection)

    if not settings_doc:
        return {
            "theme": "light",
            "locale": "pt-BR",
            "insight_threshold": 20,
            "updated_at": None,
        }

    threshold = settings_doc.get("insight_threshold")
    if threshold is None:
        threshold = settings_doc.get("llm_trigger_min_comments", 20)

    return {
        "theme": settings_doc.get("theme") or "light",
        "locale": settings_doc.get("locale") or "pt-BR",
        "insight_threshold": int(threshold or 20),
        "updated_at": _as_serializable(settings_doc.get("updated_at")),
    }


def get_user_alerts(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    db = _db_required()

    projection = {
        "message": 1,
        "level": 1,
        "severity": 1,
        "created_at": 1,
    }
    items = _find_many(db, ["alerts"], {"user_id": user_id}, projection, limit)

    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "message": str(item.get("message") or "")[:300],
                "level": str(item.get("level") or item.get("severity") or "info").lower(),
                "created_at": _as_serializable(item.get("created_at")),
            }
        )
    return output


def build_authorized_context(user_id: str) -> dict[str, Any]:
    """Contexto estritamente autorizado por usuario para chat e insights."""
    context = {
        "access_policy": "user-scoped-authorized-only",
        "scope": "chat_and_insights",
        "generated_at": utcnow().isoformat(),
        "capabilities": [
            "get_user_dashboard_summary",
            "get_user_recent_mentions",
            "get_user_open_insights",
            "get_user_settings_safe",
            "get_user_alerts",
        ],
        "dashboard": {
            "last_query": None,
            "search_id": None,
            "total_comments": 0,
            "sentiment_score": 0.0,
            "criticality": "low",
            "sentiment_distribution": {"positive": 0, "negative": 0, "neutral": 0},
            "updated_at": None,
        },
        "recent_mentions": [],
        "open_insights": [],
        "settings": {
            "theme": "light",
            "locale": "pt-BR",
            "insight_threshold": 20,
            "updated_at": None,
        },
        "alerts": [],
    }

    try:
        context["dashboard"] = get_user_dashboard_summary(user_id)
    except Exception:
        pass

    try:
        context["recent_mentions"] = get_user_recent_mentions(user_id, limit=10)
    except Exception:
        pass

    try:
        context["open_insights"] = get_user_open_insights(user_id, limit=5)
    except Exception:
        pass

    try:
        context["settings"] = get_user_settings_safe(user_id)
    except Exception:
        pass

    try:
        context["alerts"] = get_user_alerts(user_id, limit=5)
    except Exception:
        pass

    return context


class ControlledContextService:
    """Wrapper de compatibilidade para chamadas estaticas antigas."""

    @staticmethod
    def get_user_dashboard_summary(user_id: str) -> dict[str, Any]:
        return get_user_dashboard_summary(user_id)

    @staticmethod
    def get_user_recent_mentions(user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return get_user_recent_mentions(user_id, limit=limit)

    @staticmethod
    def get_user_open_insights(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return get_user_open_insights(user_id, limit=limit)

    @staticmethod
    def get_user_settings_safe(user_id: str) -> dict[str, Any]:
        return get_user_settings_safe(user_id)

    @staticmethod
    def get_user_alerts(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return get_user_alerts(user_id, limit=limit)

    @staticmethod
    def build_authorized_context(user_id: str) -> dict[str, Any]:
        return build_authorized_context(user_id)


__all__ = [
    "ControlledContextService",
    "build_authorized_context",
    "get_user_dashboard_summary",
    "get_user_recent_mentions",
    "get_user_open_insights",
    "get_user_settings_safe",
    "get_user_alerts",
]
