import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth_utils import create_access_token, get_current_user
from app.config import settings
from app.schemas import MFAVerify, TokenResponse, UserCreate, UserLogin, UserResponse
from app.services import AuthService

logger = logging.getLogger(__name__)
router = APIRouter()


def serialize_user(user: dict) -> UserResponse:
    return UserResponse(
        id=str(user["_id"]),
        email=user["email"],
        name=user["name"],
        phone=user.get("phone"),
        role=user.get("role", "user"),
        mfa_enabled=user.get("mfa_enabled", False),
        mfa_verified=user.get("mfa_verified", False),
        created_at=user.get("created_at"),
        updated_at=user.get("updated_at"),
        last_signed_in=user.get("last_signed_in"),
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate, request: Request):
    """Registra usuário, salva no MongoDB e devolve JWT."""
    try:
        user = AuthService.create_user(user_data)
        AuthService.log_audit(
            user_id=str(user["_id"]),
            action="user_registered",
            details={"email": user_data.email},
            ip_address=request.client.host if request.client else None,
        )
        token = create_access_token(
            subject=str(user["_id"]),
            role=user.get("role", "user"),
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        return TokenResponse(access_token=token, user=serialize_user(user))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Erro ao registrar usuário")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Erro ao registrar usuário") from exc


@router.post("/login", response_model=TokenResponse)
async def login(credentials: UserLogin, request: Request):
    """Autentica email/senha e devolve JWT."""
    user = AuthService.authenticate_user(credentials.email, credentials.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email ou senha inválidos")

    AuthService.log_audit(
        user_id=str(user["_id"]),
        action="user_login",
        ip_address=request.client.host if request.client else None,
    )

    token = create_access_token(
        subject=str(user["_id"]),
        role=user.get("role", "user"),
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return TokenResponse(access_token=token, user=serialize_user(user))


@router.get("/me", response_model=UserResponse)
async def me(current_user: dict = Depends(get_current_user)):
    """Retorna o usuário autenticado pelo Bearer token."""
    return serialize_user(current_user)


@router.post("/logout")
async def logout():
    """JWT é stateless; logout é feito no frontend removendo o token."""
    return {"status": "success"}


@router.post("/mfa/setup")
async def setup_mfa(current_user: dict = Depends(get_current_user)):
    secret, qr_code = AuthService.setup_mfa(str(current_user["_id"]))
    return {
        "status": "success",
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_code}",
        "message": "Escaneie o código QR com seu aplicativo autenticador",
    }


@router.post("/mfa/verify")
async def verify_mfa(mfa_data: MFAVerify, current_user: dict = Depends(get_current_user)):
    if not AuthService.verify_mfa_code(str(current_user["_id"]), mfa_data.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Código MFA inválido")
    return {"status": "success", "message": "MFA verificado com sucesso"}
