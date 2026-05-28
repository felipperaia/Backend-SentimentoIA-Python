import logging
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth_utils import get_current_user
from app.database import get_db
from app.schemas import UserRole
from app.services import AuthService

logger = logging.getLogger(__name__)

router = APIRouter()


def verify_admin(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    role = str(current_user.get("role") or "").strip().lower()
    if role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso negado. Privilegios de administrador necessarios",
        )
    return current_user


@router.get("/users")
async def list_users(
    limit: int = Query(10, ge=1, le=100),
    skip: int = Query(0, ge=0),
    current_admin: dict[str, Any] = Depends(verify_admin),
):
    del current_admin
    try:
        db = get_db()
        users = list(
            db.users.find({}, {"password_hash": 0, "mfa_secret": 0})
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        total = db.users.count_documents({})
        return {
            "total": total,
            "limit": limit,
            "skip": skip,
            "users": [
                {
                    "id": str(u["_id"]),
                    "email": u["email"],
                    "name": u["name"],
                    "phone": u.get("phone"),
                    "role": u.get("role", "user"),
                    "mfa_enabled": u.get("mfa_enabled", False),
                    "is_active": u.get("is_active", True),
                    "created_at": u.get("created_at"),
                    "last_signed_in": u.get("last_signed_in"),
                }
                for u in users
            ],
        }
    except Exception as exc:
        logger.error("Erro ao listar usuarios: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Erro ao listar usuarios") from exc


@router.get("/audit-logs")
async def get_audit_logs(
    action: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    skip: int = Query(0, ge=0),
    current_admin: dict[str, Any] = Depends(verify_admin),
):
    del current_admin
    try:
        db = get_db()
        filter_query: dict[str, Any] = {}
        if action:
            filter_query["action"] = action

        logs = list(
            db.audit_logs.find(filter_query)
            .sort("timestamp", -1)
            .skip(skip)
            .limit(limit)
        )
        total = db.audit_logs.count_documents(filter_query)
        return {
            "total": total,
            "limit": limit,
            "skip": skip,
            "logs": [
                {
                    "id": str(l["_id"]),
                    "user_id": l.get("user_id"),
                    "action": l.get("action"),
                    "details": l.get("details"),
                    "ip_address": l.get("ip_address"),
                    "timestamp": l.get("timestamp"),
                }
                for l in logs
            ],
        }
    except Exception as exc:
        logger.error("Erro ao obter logs de auditoria: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao obter logs de auditoria",
        ) from exc


@router.get("/system-stats")
async def get_system_stats(current_admin: dict[str, Any] = Depends(verify_admin)):
    del current_admin
    try:
        db = get_db()

        total_users = db.users.count_documents({})
        total_brands = db.brands.count_documents({})
        total_mentions = db.mentions.count_documents({})
        total_analyses = db.sentiment_analysis.count_documents({})
        total_reports = db.reports.count_documents({})

        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        recent_mentions = db.mentions.count_documents({"created_at": {"$gte": seven_days_ago}})

        total_reports_completed = db.reports.count_documents({"status": "completed"})
        report_success_rate = (total_reports_completed / total_reports * 100) if total_reports > 0 else 0

        return {
            "total_users": total_users,
            "total_brands": total_brands,
            "total_mentions": total_mentions,
            "total_analyses": total_analyses,
            "total_reports": total_reports,
            "recent_mentions_7_days": recent_mentions,
            "report_success_rate": round(report_success_rate, 2),
            "timestamp": datetime.utcnow(),
        }
    except Exception as exc:
        logger.error("Erro ao obter estatisticas do sistema: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao obter estatisticas do sistema",
        ) from exc


@router.post("/users/{target_user_id}/toggle-active")
async def toggle_user_active(
    target_user_id: str,
    current_admin: dict[str, Any] = Depends(verify_admin),
):
    try:
        db = get_db()
        user = db.users.find_one({"_id": ObjectId(target_user_id)})

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario nao encontrado")

        new_status = not user.get("is_active", True)
        db.users.update_one(
            {"_id": ObjectId(target_user_id)},
            {"$set": {"is_active": new_status}},
        )

        admin_user_id = str(current_admin.get("_id") or current_admin.get("id") or "")
        AuthService.log_audit(
            user_id=admin_user_id,
            action="user_status_changed",
            details={"target_user_id": target_user_id, "new_status": new_status},
        )

        return {"status": "success", "user_id": target_user_id, "is_active": new_status}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Erro ao alterar status do usuario: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao alterar status do usuario",
        ) from exc


@router.get("/alerts")
async def get_system_alerts(
    severity: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_admin: dict[str, Any] = Depends(verify_admin),
):
    del current_admin
    try:
        db = get_db()
        filter_query: dict[str, Any] = {}
        if severity:
            filter_query["severity"] = severity

        alerts = list(
            db.alerts.find(filter_query)
            .sort("created_at", -1)
            .limit(limit)
        )

        return {
            "total": len(alerts),
            "alerts": [
                {
                    "id": str(a["_id"]),
                    "brand_id": a.get("brand_id"),
                    "severity": a.get("severity"),
                    "message": a.get("message"),
                    "created_at": a.get("created_at"),
                    "resolved": a.get("resolved", False),
                }
                for a in alerts
            ],
        }
    except Exception as exc:
        logger.error("Erro ao obter alertas: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Erro ao obter alertas") from exc
