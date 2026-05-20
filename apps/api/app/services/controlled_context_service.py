import re
from typing import Any

from app.database import get_db

SENSITIVE_KEY_PATTERN = re.compile(
    r"password|senha|hash|token|api_key|secret|mfa|phone|cpf|cnpj|email",
    re.IGNORECASE,
)


def _as_serializable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _strip_sensitive(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return None

    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, nested in value.items():
            if SENSITIVE_KEY_PATTERN.search(str(key or "")):
                continue
            sanitized = _strip_sensitive(nested, depth + 1)
            if sanitized is not None:
                clean[str(key)] = sanitized
        return clean

    if isinstance(value, list):
        items: list[Any] = []
        for item in value:
            sanitized = _strip_sensitive(item, depth + 1)
            if sanitized is not None:
                items.append(sanitized)
        return items

    return _as_serializable(value)


def _find_one(db: Any, collection_names: list[str], query: dict[str, Any], projection: dict[str, int]) -> dict[str, Any] | None:
    for name in collection_names:
        doc = db[name].find_one(query, projection, sort=[("created_at", -1)])
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
    for name in collection_names:
        items = list(db[name].find(query, projection).sort(sort_field, -1).limit(limit))
        if items:
            return items
    return []


def get_user_dashboard_summary(user_id: str) -> dict[str, Any]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")

    projection = {
        "query": 1,
        "search_id": 1,
        "metrics": 1,
        "total": 1,
        "created_at": 1,
    }
    doc = _find_one(
        db,
        ["search_jobs", "searchjobs"],
        {"user_id": user_id, "status": "completed"},
        projection,
    )

    if not doc:
        return {
            "last_query": None,
            "total_comments": 0,
            "sentiment_score": 0,
            "criticality": "low",
            "positive": 0,
            "negative": 0,
            "neutral": 0,
            "search_id": None,
            "created_at": None,
        }

    metrics = doc.get("metrics") if isinstance(doc.get("metrics"), dict) else {}
    sentiment_distribution = metrics.get("sentiment_distribution") if isinstance(metrics.get("sentiment_distribution"), dict) else {}

    positive = int(sentiment_distribution.get("positivo", sentiment_distribution.get("positive", 0)) or 0)
    negative = int(sentiment_distribution.get("negativo", sentiment_distribution.get("negative", 0)) or 0)
    neutral = int(sentiment_distribution.get("neutro", sentiment_distribution.get("neutral", 0)) or 0)
    critical_mentions = int(metrics.get("critical_mentions", 0) or 0)

    criticality = str(metrics.get("criticality") or "").lower()
    if criticality not in {"high", "medium", "low"}:
        if critical_mentions > 10:
            criticality = "high"
        elif critical_mentions > 0:
            criticality = "medium"
        else:
            criticality = "low"

    total_comments = int(metrics.get("total_comments", metrics.get("total_mentions", doc.get("total", 0))) or 0)

    return {
        "last_query": doc.get("query"),
        "total_comments": total_comments,
        "sentiment_score": float(metrics.get("sentiment_score", metrics.get("reputation_score", 0)) or 0),
        "criticality": criticality,
        "positive": positive,
        "negative": negative,
        "neutral": neutral,
        "search_id": doc.get("search_id"),
        "created_at": _as_serializable(doc.get("created_at")),
    }


def get_user_recent_mentions(user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")

    safe_limit = max(1, min(int(limit), 50))
    projection = {
        "text": 1,
        "sentiment": 1,
        "source": 1,
        "criticality": 1,
        "aspects": 1,
        "published_at": 1,
        "publishedat": 1,
        "source_tier": 1,
        "created_at": 1,
    }
    items = _find_many(db, ["mentions"], {"user_id": user_id}, projection, safe_limit)

    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "text": str(item.get("text") or "")[:500],
                "sentiment": item.get("sentiment"),
                "source": item.get("source"),
                "criticality": item.get("criticality"),
                "aspects": item.get("aspects") if isinstance(item.get("aspects"), dict) else {},
                "publishedat": _as_serializable(item.get("published_at") or item.get("publishedat") or item.get("created_at")),
                "source_tier": item.get("source_tier") or "standard",
            }
        )

    return output


def get_user_open_insights(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")

    safe_limit = max(1, min(int(limit), 20))
    query = {
        "user_id": user_id,
        "archived": {"$ne": True},
        "$or": [
            {"resolution": {"$in": ["pending", "in_progress"]}},
            {"status": {"$in": ["open", "in_progress"]}},
        ],
    }
    projection = {
        "insight_id": 1,
        "insightid": 1,
        "priority": 1,
        "resolution": 1,
        "executive_summary": 1,
        "recommended_actions": 1,
        "risks": 1,
        "opportunities": 1,
        "created_at": 1,
        "createdat": 1,
    }

    items = _find_many(db, ["insights"], query, projection, safe_limit)
    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "insightid": item.get("insightid") or item.get("insight_id") or str(item.get("_id") or ""),
                "priority": item.get("priority") or "medium",
                "resolution": item.get("resolution") or "pending",
                "executive_summary": str(item.get("executive_summary") or "")[:500],
                "recommended_actions": (
                    item.get("recommended_actions") if isinstance(item.get("recommended_actions"), list) else []
                ),
                "risks": item.get("risks") if isinstance(item.get("risks"), list) else [],
                "opportunities": item.get("opportunities") if isinstance(item.get("opportunities"), list) else [],
                "createdat": _as_serializable(item.get("created_at") or item.get("createdat")),
            }
        )

    return output


def get_user_settings_safe(user_id: str) -> dict[str, Any]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")

    projection = {
        "theme": 1,
        "locale": 1,
        "insight_threshold": 1,
        "llm_trigger_min_comments": 1,
        "updated_at": 1,
    }
    settings_doc = _find_one(
        db,
        ["dashboard_settings", "dashboardsettings"],
        {"user_id": user_id},
        projection,
    )

    if not settings_doc:
        return {
            "theme": "light",
            "locale": "pt-BR",
            "insight_threshold": 20,
        }

    threshold = settings_doc.get("insight_threshold")
    if threshold is None:
        threshold = settings_doc.get("llm_trigger_min_comments", 20)

    return {
        "theme": settings_doc.get("theme") or "light",
        "locale": settings_doc.get("locale") or "pt-BR",
        "insight_threshold": int(threshold or 20),
    }


def get_user_alerts(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    db = get_db()
    if db is None:
        raise RuntimeError("Banco de dados indisponivel")

    safe_limit = max(1, min(int(limit), 20))
    projection = {
        "message": 1,
        "level": 1,
        "severity": 1,
        "created_at": 1,
        "createdat": 1,
    }
    items = _find_many(db, ["alerts"], {"user_id": user_id}, projection, safe_limit)

    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "message": item.get("message") or "",
                "level": item.get("level") or item.get("severity") or "info",
                "createdat": _as_serializable(item.get("created_at") or item.get("createdat")),
            }
        )

    return output


def build_authorized_context(user_id: str) -> dict[str, Any]:
    context = {
        "access_policy": "internal-controlled-only",
        "capabilities": [
            "get_user_dashboard_summary",
            "get_user_recent_mentions",
            "get_user_open_insights",
            "get_user_settings_safe",
            "get_user_alerts",
        ],
        "dashboard": get_user_dashboard_summary(user_id),
        "recent_mentions": get_user_recent_mentions(user_id),
        "open_insights": get_user_open_insights(user_id),
        "settings": get_user_settings_safe(user_id),
        "alerts": get_user_alerts(user_id),
    }
    return _strip_sensitive(context, depth=0)


class ControlledContextService:
    """Wrapper de compatibilidade para chamadas antigas."""

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
