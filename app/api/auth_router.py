import logging
from datetime import timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.auth_utils import create_access_token, create_refresh_token, decode_refresh_token, get_current_user
from app.config import settings
from app.schemas import (
    ChangePasswordRequest,
    MFADisable,
    MFALoginChallenge,
    MFAStatusResponse,
    MFAVerify,
    PasswordReset,
    PasswordResetConfirm,
    PasswordResetResponse,
    RefreshTokenRequest,
    TokenResponse,
    TokenRefreshResponse,
    UserCreate,
    UserLogin,
    UserProfileUpdate,
    UserResponse,
)
from app.services import AuthService
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)
router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]

MFA_INVALID_CODE_DETAIL = "Código MFA inválido"


def _access_token_expires_in_seconds() -> int:
    return int(settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60


def resolve_login_mfa(user: dict, credentials: UserLogin, request: Request) -> JSONResponse | None:
    user_id = str(user["_id"])
    mfa_active = bool(user.get("mfa_enabled") and user.get("mfa_secret"))

    if user.get("mfa_enabled") and not user.get("mfa_secret"):
        logger.warning("Usuario %s com MFA habilitado sem secret. Ignorando desafio MFA.", user_id)

    if not mfa_active:
        return None

    if not credentials.mfa_code:
        AuthService.log_audit(
            user_id=user_id,
            action="user_login_mfa_challenge",
            ip_address=request.client.host if request.client else None,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "mfa_required": True,
                "message": "Codigo MFA necessario para concluir o login.",
            },
        )

    if not AuthService.validate_mfa_code(user_id, credentials.mfa_code):
        AuthService.log_audit(
            user_id=user_id,
            action="user_login_mfa_failed",
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=MFA_INVALID_CODE_DETAIL)

    return None


def serialize_user(user: dict) -> UserResponse:
    return UserResponse(
        id=str(user["_id"]),
        email=user["email"],
        name=user["name"],
        username=user.get("username"),
        phone=user.get("phone"),
        role=user.get("role", "user"),
        mfa_enabled=user.get("mfa_enabled", False),
        mfa_verified=user.get("mfa_verified", False),
        data_rights_url="/api/privacy/policy",
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
        refresh_token = create_refresh_token(
            subject=str(user["_id"]),
            role=user.get("role", "user"),
            expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
        return TokenResponse(
            access_token=token,
            refresh_token=refresh_token,
            expires_in=_access_token_expires_in_seconds(),
            user=serialize_user(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Erro ao registrar usuário")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Erro ao registrar usuário") from exc


@router.post("/login", response_model=TokenResponse | MFALoginChallenge)
async def login(credentials: UserLogin, request: Request):
    """Autentica email/senha e devolve JWT."""
    user = AuthService.authenticate_user(credentials.email, credentials.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email ou senha inválidos")

    user_id = str(user["_id"])
    mfa_challenge = resolve_login_mfa(user=user, credentials=credentials, request=request)
    if mfa_challenge is not None:
        return mfa_challenge

    AuthService.log_audit(
        user_id=user_id,
        action="user_login",
        ip_address=request.client.host if request.client else None,
    )

    token = create_access_token(
        subject=user_id,
        role=user.get("role", "user"),
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user_id,
        role=user.get("role", "user"),
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    return TokenResponse(
        access_token=token,
        refresh_token=refresh_token,
        expires_in=_access_token_expires_in_seconds(),
        user=serialize_user(user),
    )


@router.post("/refresh", response_model=TokenRefreshResponse)
async def refresh_token(payload: RefreshTokenRequest):
    decoded = decode_refresh_token(payload.refresh_token)
    user_id = str(decoded["sub"])

    user = AuthService.get_user_by_id(user_id)
    if not user or not user.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inativo ou inexistente")

    access_token = create_access_token(
        subject=user_id,
        role=user.get("role", "user"),
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    new_refresh_token = create_refresh_token(
        subject=user_id,
        role=user.get("role", "user"),
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )

    return TokenRefreshResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=_access_token_expires_in_seconds(),
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser):
    """Retorna o usuário autenticado pelo Bearer token."""
    return serialize_user(current_user)


@router.patch("/me", response_model=UserResponse)
async def update_profile(payload: UserProfileUpdate, current_user: CurrentUser, request: Request):
    """Atualiza dados do perfil do usuário autenticado."""
    try:
        updated = AuthService.update_profile(
            str(current_user["_id"]),
            name=payload.name,
            username=payload.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    AuthService.log_audit(
        user_id=str(current_user["_id"]),
        action="user_profile_updated",
        details={
            "name_updated": payload.name is not None,
            "username_updated": payload.username is not None,
        },
        ip_address=request.client.host if request.client else None,
    )

    return serialize_user(updated)


@router.delete("/me")
async def delete_me(
    current_user: CurrentUser,
    hard_delete: bool = False,
    delete_related_data: bool = True,
):
    """Exclui a conta do usuário autenticado e dados relacionados (quando habilitado)."""
    user_id = str(current_user.get("_id") or current_user.get("id"))

    try:
        result = AuthService.delete_user_account(
            user_id=user_id,
            hard_delete=bool(hard_delete),
            delete_related_data=bool(delete_related_data),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "status": "deleted",
        "mode": result.get("status"),
        "related_data_deleted": bool(result.get("related_data_deleted", False)),
    }


@router.post("/change-password", response_model=PasswordResetResponse)
async def change_password(payload: ChangePasswordRequest, current_user: CurrentUser, request: Request):
    try:
        changed = AuthService.change_password(
            str(current_user["_id"]),
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not changed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Senha atual inválida")

    AuthService.log_audit(
        user_id=str(current_user["_id"]),
        action="user_password_changed",
        ip_address=request.client.host if request.client else None,
    )

    return PasswordResetResponse(status="success", message="Senha alterada com sucesso.")


@router.post("/logout")
async def logout(current_user: CurrentUser):
    """JWT é stateless; frontend remove token e preferencias locais apos logout."""
    del current_user
    return {
        "status": "logged_out",
        "clear_preferences": True,
    }


@router.get("/mfa/status", response_model=MFAStatusResponse)
async def mfa_status(current_user: CurrentUser):
    enabled = bool(current_user.get("mfa_enabled") and current_user.get("mfa_secret"))
    return MFAStatusResponse(
        mfa_enabled=enabled,
        mfa_verified=bool(current_user.get("mfa_verified") and enabled),
    )


@router.post("/mfa/setup")
async def setup_mfa(current_user: CurrentUser):
    try:
        secret, qr_code = AuthService.setup_mfa(str(current_user["_id"]))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "status": "success",
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_code}",
        "message": "Escaneie o código QR com seu aplicativo autenticador",
    }


@router.post("/mfa/enable")
async def enable_mfa(mfa_data: MFAVerify, current_user: CurrentUser):
    if not AuthService.enable_mfa(str(current_user["_id"]), mfa_data.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=MFA_INVALID_CODE_DETAIL)
    return {"status": "success", "message": "MFA habilitado com sucesso"}


@router.post("/mfa/verify")
async def verify_mfa(mfa_data: MFAVerify, current_user: CurrentUser):
    if not AuthService.validate_mfa_code(str(current_user["_id"]), mfa_data.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=MFA_INVALID_CODE_DETAIL)
    return {"status": "success", "message": "MFA verificado com sucesso"}


@router.post("/mfa/disable")
async def disable_mfa(payload: MFADisable, current_user: CurrentUser):
    if not AuthService.disable_mfa(str(current_user["_id"]), payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Senha inválida")
    return {"status": "success", "message": "MFA desabilitado com sucesso"}


@router.post("/password/forgot", response_model=PasswordResetResponse)
async def forgot_password(payload: PasswordReset, request: Request):
    token, user = AuthService.create_password_reset_token(payload.email)

    if token and user:
        frontend_base_url = (settings.FRONTEND_URL or "").strip().rstrip("/") or str(request.base_url).rstrip("/")
        reset_url = f"{frontend_base_url}/redefinir-senha?token={quote(token)}"

        email_sent = await EmailService.send_password_reset_email(
            to_email=payload.email,
            to_name=str(user.get("name") or "").strip() or None,
            reset_url=reset_url,
        )

        if not email_sent:
            logger.warning("Link de reset gerado para %s, mas SMTP nao enviou e-mail", payload.email)

        AuthService.log_audit(
            user_id=str(user["_id"]),
            action="password_reset_requested",
            ip_address=request.client.host if request.client else None,
        )

    return PasswordResetResponse(
        status="success",
        message="Se o endereco estiver cadastrado, voce recebera um link de recuperacao.",
    )


@router.post("/password/reset", response_model=PasswordResetResponse)
async def reset_password(payload: PasswordResetConfirm):
    if not AuthService.reset_password_with_token(payload.token, payload.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token de recuperação inválido ou expirado",
        )

    return PasswordResetResponse(
        status="success",
        message="Senha redefinida com sucesso.",
    )
