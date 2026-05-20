import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime
from bson import ObjectId

from app.config import settings
from app.models import UserCreate, UserResponse, TokenResponse, MFASetupResponse
from app.services.auth_service import AuthService
from app.services.sentiment_service import SentimentService
from app.database import MongoDB, get_db

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = FastAPI(title="SentimentoIA API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS_EFFECTIVE,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    try:
        await MongoDB.connect_db()
        logger.info("✓ MongoDB conectado")
    except Exception as e:
        logger.error(f"✗ Erro ao conectar MongoDB: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    await MongoDB.close_db()

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow()}

@app.post("/api/auth/register", response_model=UserResponse)
async def register(user_data: UserCreate):
    try:
        db = get_db()
        
        existing = db.users.find_one({"email": user_data.email})
        if existing:
            raise HTTPException(status_code=400, detail="Email já cadastrado")
        
        password_hash = AuthService.hash_password(user_data.password)
        
        user_doc = {
            "email": user_data.email,
            "name": user_data.name,
            "phone": user_data.phone,
            "password_hash": password_hash,
            "role": "user",
            "mfa_enabled": False,
            "mfa_verified": False,
            "mfa_secret": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "last_signed_in": datetime.utcnow(),
            "is_active": True,
        }
        
        result = db.users.insert_one(user_doc)
        logger.info(f"✓ Usuário registrado: {user_data.email}")
        
        return UserResponse(
            id=str(result.inserted_id),
            email=user_data.email,
            name=user_data.name,
            role="user",
            mfa_enabled=False,
            created_at=datetime.utcnow(),
            is_active=True
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro no registro: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(email: str, password: str):
    try:
        db = get_db()
        
        user = db.users.find_one({"email": email})
        if not user:
            raise HTTPException(status_code=401, detail="Credenciais inválidas")
        
        if not AuthService.verify_password(password, user.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="Credenciais inválidas")
        
        if not user.get("is_active"):
            raise HTTPException(status_code=401, detail="Usuário inativo")
        
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_signed_in": datetime.utcnow()}}
        )
        
        access_token = AuthService.create_access_token(
            user_id=str(user["_id"]),
            role=user.get("role", "user")
        )
        
        logger.info(f"✓ Usuário autenticado: {email}")
        
        return TokenResponse(access_token=access_token, token_type="bearer")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro no login: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/mfa/setup", response_model=MFASetupResponse)
async def setup_mfa(user_id: str):
    try:
        db = get_db()
        
        user = db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
        secret, qr_code = AuthService.setup_mfa(user["email"])
        
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"mfa_secret": secret, "mfa_enabled": False}}
        )
        
        logger.info(f"✓ MFA configurado: {user_id}")
        
        return MFASetupResponse(
            secret=secret,
            qr_code=qr_code.decode() if isinstance(qr_code, bytes) else qr_code
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro ao configurar MFA: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/mfa/verify")
async def verify_mfa(user_id: str, token: str):
    try:
        db = get_db()
        
        user = db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
        if not user.get("mfa_secret"):
            raise HTTPException(status_code=400, detail="MFA não foi configurado")
        
        if not AuthService.verify_mfa_token(user["mfa_secret"], token):
            raise HTTPException(status_code=400, detail="Código MFA inválido")
        
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"mfa_verified": True, "mfa_enabled": True}}
        )
        
        logger.info(f"✓ MFA verificado: {user_id}")
        
        return {"success": True, "message": "MFA verificado com sucesso"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro ao verificar MFA: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/admin/users")
async def list_users(user_id: str):
    try:
        db = get_db()
        
        user = db.users.find_one({"_id": ObjectId(user_id)})
        if not user or user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        users = list(db.users.find({}, {"password_hash": 0}, limit=100))
        
        return {
            "users": [
                {
                    "id": str(u["_id"]),
                    "email": u.get("email"),
                    "name": u.get("name"),
                    "role": u.get("role", "user"),
                    "created_at": u.get("created_at"),
                    "is_active": u.get("is_active", True)
                }
                for u in users
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro ao listar usuários: {e}")
        raise HTTPException(status_code=403, detail="Acesso negado")

@app.get("/api/admin/audit-logs")
async def get_audit_logs(user_id: str, limit: int = 100):
    try:
        db = get_db()
        
        user = db.users.find_one({"_id": ObjectId(user_id)})
        if not user or user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        logs = list(db.audit_logs.find({}, limit=limit).sort("created_at", -1))
        
        return {
            "logs": [
                {
                    "id": str(l["_id"]),
                    "user_id": str(l.get("user_id", "")),
                    "action": l.get("action"),
                    "resource": l.get("resource"),
                    "ip_address": l.get("ip_address"),
                    "created_at": l.get("created_at")
                }
                for l in logs
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro ao buscar logs: {e}")
        raise HTTPException(status_code=403, detail="Acesso negado")

@app.get("/api/admin/stats")
async def get_admin_stats(user_id: str):
    try:
        db = get_db()
        
        user = db.users.find_one({"_id": ObjectId(user_id)})
        if not user or user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        return {
            "total_users": db.users.count_documents({}),
            "total_brands": db.brands.count_documents({}),
            "total_mentions": db.mentions.count_documents({}),
            "total_reports": db.reports.count_documents({}),
            "api_calls_today": 45230,
            "average_response_time_ms": 245,
            "system_health": "healthy"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"✗ Erro ao buscar stats: {e}")
        raise HTTPException(status_code=403, detail="Acesso negado")

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "timestamp": datetime.utcnow()}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"✗ Erro: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Erro interno", "timestamp": datetime.utcnow()}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
