import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api.admin_router import router as admin_router
from app.api.auth_router import router as auth_router
from app.api.companies_router import router as companies_router
from app.api.demo_router import router as demo_router
from app.api.ingestion_router import router as ingestion_router
from app.api.metrics_router import router as metrics_router
from app.api.mentions_router import router as mentions_router
from app.api.reports_router import router as reports_router
from app.auth_utils import decode_access_token, get_current_user, get_optional_current_user
from app.config import settings
from app.database import connect_db, disconnect_db, get_db
from app.models import ScrapeRequest, SearchRequest
from app.schemas import (
    ChatMessageCreateRequest,
    ChatThreadCreateRequest,
    PrivacyConsentResponse,
    PrivacyConsentUpsertRequest,
    UserSettingsUpdateRequest,
)
from app.services.chat_service import ChatService, ChatUnavailableError
from app.services.dashboard_service import DashboardService
from app.services.enrichment_service import EnrichmentService
from app.services.insight_service import InsightGenerationError, InsightService
from app.services.llm_service import LLMService
from app.services.normalization_service import normalize_mention, utcnow
from app.services.nps_service import NpsService
from app.services.report_service import ReportService
from app.services.scraper_service import ScraperService
from app.services.search_service import SearchService
from app.services.source_registry_service import SourceRegistryService

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


def _limiter_key(request: Request) -> str:
    authorization = str(request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            try:
                payload = decode_access_token(token)
                subject = str(payload.get("sub") or "").strip()
                if subject:
                    return f"user:{subject}"
            except HTTPException:
                pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_limiter_key)


class AnalyzeRequest(BaseModel):
    text: str
    brand_name: str | None = None
    source: str = "manual"


class InsightGenerateRequest(BaseModel):
    batch_id: str | None = None
    force: bool = False


class NpsSubmitRequest(BaseModel):
    session_id: str
    module_key: str = "geral"
    score: int
    comment: str | None = None
    route: str | None = None
    context_metadata: dict | None = None


class NpsDismissRequest(BaseModel):
    session_id: str
    module_key: str = "geral"
    route: str | None = None


INTERNAL_ERROR_MARKERS = (
    "traceback",
    "stack",
    "exception",
    "gateway",
    "upstream",
    "ollama",
    "model",
    "http://",
    "https://",
)


def _default_http_error_message(status_code: int) -> str:
    if status_code == 400:
        return "Requisicao invalida. Verifique os dados informados."
    if status_code == 401:
        return "Sessao invalida ou expirada. Faca login novamente."
    if status_code == 403:
        return "Acesso negado para esta operacao."
    if status_code == 404:
        return "Recurso nao encontrado."
    if status_code in {408, 504}:
        return "Tempo limite excedido. Tente novamente."
    if status_code == 429:
        return "Muitas requisicoes em sequencia. Aguarde e tente novamente."
    if status_code >= 500:
        return "Erro interno. Tente novamente em instantes."
    return "Nao foi possivel concluir a solicitacao."


def _extract_detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail.strip()
    if isinstance(detail, dict):
        for key in ("detail", "message", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            for key in ("msg", "detail", "message"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _sanitize_http_detail(status_code: int, detail: Any) -> str:
    text = _extract_detail_text(detail)
    if not text:
        return _default_http_error_message(status_code)

    lowered = text.lower()
    if any(marker in lowered for marker in INTERNAL_ERROR_MARKERS):
        return _default_http_error_message(status_code)

    if len(text) > 240:
        return _default_http_error_message(status_code)

    return text


def _parse_query_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    raw_value = str(value or "").strip()
    if not raw_value:
        return default

    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default

    return max(minimum, min(maximum, parsed))


def _hash_ip_prefix(ip_address: str | None) -> str:
    if not ip_address:
        return hashlib.sha256(b"unknown").hexdigest()

    value = str(ip_address).strip()
    if "." in value:
        parts = value.split(".")
        prefix = ".".join(parts[:3])
    else:
        parts = value.split(":")
        prefix = ":".join(parts[:3])

    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def _hash_user_agent(user_agent: str | None) -> str:
    value = str(user_agent or "").strip()
    if not value:
        return hashlib.sha256(b"unknown-agent").hexdigest()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def rate_limit_exceeded_handler(request: Request, exc: Exception):
    response = _rate_limit_exceeded_handler(request, exc)
    retry_after_raw = response.headers.get("Retry-After")
    retry_after: int | None = None
    if retry_after_raw is not None:
        try:
            retry_after = int(float(str(retry_after_raw).strip()))
        except (TypeError, ValueError):
            retry_after = None

    payload: dict[str, Any] = {
        "ok": False,
        "error": "Limite de requisicoes atingido. Tente novamente em instantes.",
        "detail": "Limite de requisicoes atingido. Tente novamente em instantes.",
    }
    if retry_after is not None:
        payload["retry_after"] = retry_after

    return JSONResponse(status_code=429, content=payload, headers=dict(response.headers))


async def auto_refresh_loop() -> None:
    """Atualização automática opcional.

    Quando AUTO_REFRESH_ENABLED=True, o backend reexecuta buscas recentes.
    Isso mantém dashboard/histórico vivos sem depender do usuário clicar novamente.
    """
    while settings.AUTO_REFRESH_ENABLED:
        try:
            db = get_db()
            if db is not None:
                jobs = list(db.search_jobs.find({"status": "completed"}).sort("created_at", -1).limit(20))
                for job in jobs:
                    await SearchService.run_search(
                        user_id=job["user_id"],
                        query=job["query"],
                        sources=job.get("sources", SourceRegistryService.default_sources()),
                        period_days=job.get("period_days", 30),
                        locality=job.get("locality"),
                        use_cache=False,
                    )
        except Exception as exc:
            logger.error("Erro no auto refresh: %s", exc)

        await asyncio.sleep(settings.AUTO_REFRESH_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    await connect_db()
    SourceRegistryService.sync_defaults_to_db()

    try:
        await LLMService.validate_connection()
    except Exception as exc:
        logger.warning(f"Ollama não acessível. Funcionalidades de IA desabilitadas: {exc}")

    task = None
    if settings.auto_refresh_enabled:
        task = asyncio.create_task(auto_refresh_loop())

    try:
        yield
    finally:
        if task:
            task.cancel()
        await disconnect_db()


app = FastAPI(
    title="SentimentoIA API",
    description="Backend com coleta multi-fonte resiliente (Reddit, YouTube, App/Play Store e compatibilidade legada), Ollama Cloud e MongoDB.",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.include_router(auth_router, prefix="/api/auth", tags=["Autenticação"])
app.include_router(ingestion_router, prefix="/api/ingestion", tags=["Ingestao"])
app.include_router(demo_router, prefix="/api/demo", tags=["Demo"])
app.include_router(companies_router, prefix="/api", tags=["Empresas"])


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    detail = _sanitize_http_detail(exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": detail,
            "detail": detail,
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Evita que exceções inesperadas virem resposta HTML quebrada no frontend."""
    request_id = uuid4().hex
    logger.exception("Erro nao tratado request_id=%s path=%s", request_id, request.url.path)

    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "Erro interno. Tente novamente em instantes.",
            "request_id": request_id,
        },
    )


@app.get("/health")
async def health():
    """Healthcheck simples para saber se o backend está ativo."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/privacy/policy")
async def privacy_policy():
    return {
        "version": "1.0",
        "effective_date": "2024-01-01",
        "lgpd_compliant": True,
        "data_controller": "SentimentoIA",
        "data_subject_rights": ["acesso", "retificação", "exclusão", "portabilidade", "revogação"],
        "retention_policy": (
            "Dados mantidos até exclusão pelo usuário ou "
            f"{max(1, int(settings.DATA_RETENTION_YEARS))} anos de inatividade."
        ),
        "contact": settings.PRIVACY_CONTACT_EMAIL or "privacidade@sentimentoia.com",
    }


@app.get("/api/privacy/rights")
@app.get("/api/lgpd/rights")
async def privacy_rights():
    return {
        "law": "LGPD",
        "rights": [
            "acesso",
            "correcao",
            "anonimizacao",
            "portabilidade",
            "eliminacao",
            "informacao_sobre_compartilhamento",
            "revogacao_de_consentimento",
        ],
        "contact": settings.PRIVACY_CONTACT_EMAIL or "privacidade@sentimentoia.com",
        "retention_years": max(1, int(settings.DATA_RETENTION_YEARS or 2)),
    }


@app.post("/api/privacy/consent")
async def privacy_consent(
    payload: PrivacyConsentUpsertRequest,
    request: Request,
    current_user: dict[str, Any] | None = Depends(get_optional_current_user),
):
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Banco de dados indisponivel")

    user_id = str(current_user.get("_id") or current_user.get("id")) if current_user else None
    if not user_id and not payload.session_id:
        raise HTTPException(status_code=400, detail="Informe session_id quando não autenticado")

    preferences = payload.normalized_preferences()
    consent = payload.resolved_consent()
    version = str(payload.version or "1.0")
    ip_hash = _hash_ip_prefix(request.client.host if request.client else None)
    user_agent_hash = _hash_user_agent(request.headers.get("user-agent"))
    now = utcnow()

    filter_query: dict[str, Any]
    if user_id:
        filter_query = {"user_id": user_id}
    else:
        filter_query = {
            "session_id": str(payload.session_id or "").strip(),
            "ip_hash": ip_hash,
        }

    consent_doc = {
        "user_id": user_id,
        "userid": user_id,
        "session_id": str(payload.session_id or "").strip() or None,
        "consent": consent,
        "version": version,
        "preferences": preferences,
        "analytics": bool(preferences.get("cookies_analiticos", False)),
        "marketing": bool(preferences.get("cookies_personalizacao", False)),
        "ip_hash": ip_hash,
        "updated_at": now,
        "user_agent_hash": user_agent_hash,
    }

    db.privacyconsents.update_one(
        filter_query,
        {
            "$set": consent_doc,
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    return PrivacyConsentResponse(
        consent=consent,
        preferences=preferences,
        version=version,
        session_id=consent_doc.get("session_id"),
        user_id=user_id,
        created_at=now,
        updated_at=now,
    ).model_dump()


@app.get("/api/privacy/consent")
async def get_privacy_consent(
    session_id: str | None = Query(None),
    current_user: dict[str, Any] | None = Depends(get_optional_current_user),
):
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Banco de dados indisponivel")

    user_id = str(current_user.get("_id") or current_user.get("id")) if current_user else None

    query: dict[str, Any] = {}
    if user_id:
        query = {"user_id": user_id}
    elif session_id:
        query = {"session_id": session_id}
    else:
        raise HTTPException(status_code=400, detail="Informe session_id quando não autenticado")

    doc = db.privacyconsents.find_one(query, sort=[("updated_at", -1), ("created_at", -1)])
    if not doc:
        return PrivacyConsentResponse(
            consent=False,
            preferences={
                "cookies_analiticos": False,
                "cookies_personalizacao": False,
                "cookies_treinamento_ia": False,
            },
            version="1.0",
            session_id=session_id,
            user_id=user_id,
        ).model_dump()

    preferences = doc.get("preferences") if isinstance(doc.get("preferences"), dict) else {}
    if not preferences:
        preferences = {
            "cookies_analiticos": bool(doc.get("analytics", False)),
            "cookies_personalizacao": bool(doc.get("marketing", False)),
            "cookies_treinamento_ia": False,
        }

    return PrivacyConsentResponse(
        consent=bool(doc.get("consent", any(bool(v) for v in preferences.values()))),
        preferences={
            "cookies_analiticos": bool(preferences.get("cookies_analiticos", False)),
            "cookies_personalizacao": bool(preferences.get("cookies_personalizacao", False)),
            "cookies_treinamento_ia": bool(preferences.get("cookies_treinamento_ia", False)),
        },
        version=str(doc.get("version") or "1.0"),
        session_id=doc.get("session_id"),
        user_id=doc.get("user_id"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    ).model_dump()


@app.get("/api/privacy/export-summary")
async def privacy_export_summary(current_user: dict[str, Any] = Depends(get_current_user)):
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Banco de dados indisponivel")

    user_id = str(current_user.get("_id") or current_user.get("id"))
    collections = [
        "mentions",
        "search_jobs",
        "insights",
        "chat_threads",
        "chat_messages",
        "nps_responses",
        "alerts",
        "reports",
        "privacyconsents",
        "user_consents",
    ]
    summary: dict[str, int] = {}
    total_records = 0
    for collection in collections:
        count = int(db[collection].count_documents({"user_id": user_id}))
        summary[collection] = count
        total_records += count

    return {
        "ok": True,
        "user_id": user_id,
        "total_records": total_records,
        "collections": summary,
        "generated_at": utcnow().isoformat(),
    }


@app.get("/api/status/integrations")
async def integrations_status():
    """Mostra se as integrações principais estão configuradas."""
    llm = await LLMService.healthcheck()
    active_sources = SourceRegistryService.active_sources()
    return {
        "scraping_enabled": True,
        "scraper_delay_seconds": settings.scraper_request_delay_seconds,
        "scraper_default_limit": settings.scraper_default_limit,
        "scraper_default_sources": SourceRegistryService.default_sources(),
        "scraper_active_sources": active_sources,
        "scraper_source_metadata": SourceRegistryService.source_metadata(),
        "mongodb_configured": bool(settings.mongodb_uri),
        "llm": llm,
        "external_source_apis_removed": True,
    }


@app.post("/api/search")
@limiter.limit(f"{settings.rate_limit_search_per_minute}/minute")
async def search_mentions(request: Request, payload: SearchRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """Executa busca real e salva tudo por search_id.

    Entrada esperada:
    {
      "brand_name": "Nike",
                        "sources": ["reclameaqui", "reddit", "web"],
      "period_days": 30,
      "locality": "São Paulo",
      "replace_existing": false
    }

    Observação:
    - replace_existing=True força nova busca.
    - replace_existing=False permite cache inteligente por CACHE_TTL_MINUTES.
    """
    sources = [getattr(source, "value", str(source)) for source in payload.sources]
    user_id = str(current_user.get("_id") or current_user.get("id"))

    result = await SearchService.run_search(
        user_id=user_id,
        query=payload.brand_name,
        sources=sources,
        period_days=payload.period_days,
        locality=payload.locality,
        use_cache=not payload.replace_existing,
    )
    del request
    return result


@app.post("/api/scrape")
@limiter.limit(f"{settings.rate_limit_scrape_per_minute}/minute")
async def scrape_mentions(request: Request, payload: ScrapeRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """Executa scraping simples por fonte e retorna resultados agrupados."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    sources = [getattr(source, "value", str(source)) for source in payload.sources]
    try:
        result = await ScraperService.scrape_async(
            query=payload.query,
            sources=sources,
            limit_per_source=payload.limit_per_source,
            user_id=user_id,
        )
        del request
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dashboard")
async def dashboard(
    batch_id: str | None = Query(None),
    search_id: str | None = Query(None),
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    mode: str = Query("live"),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    period_days: int | None = Query(None, ge=1, le=365),
    limit_mentions: int = Query(200, ge=1, le=1000),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Retorna dashboard do pipeline processado (mentions status=processed)."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    selected_batch_id = batch_id or search_id
    selected_company_slug = company_slug or company_id
    normalized_mode = str(mode or "live").strip().lower()
    if normalized_mode not in {"live", "demo"}:
        raise HTTPException(status_code=400, detail="Modo invalido. Use live ou demo")

    return DashboardService.get_dashboard(
        user_id=user_id,
        batch_id=selected_batch_id,
        period_days=period_days,
        limit_mentions=limit_mentions,
        mode=normalized_mode,
        company_slug=selected_company_slug,
        period_from=period_from,
        period_to=period_to,
    )


@app.get("/api/mentions")
async def mentions(
    batch_id: str | None = Query(None),
    search_id: str | None = Query(None),
    batch_id_alias: str | None = Query(None, alias="batchId"),
    search_id_alias: str | None = Query(None, alias="searchId"),
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    status: str | None = Query(None),
    sentiment: str | None = Query(None),
    limit: str | None = Query("100"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista mencoes por contexto, com filtros opcionais por status e sentimento."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    selected_batch_id = (
        str(batch_id or "").strip()
        or str(search_id or "").strip()
        or str(batch_id_alias or "").strip()
        or str(search_id_alias or "").strip()
        or None
    )
    normalized_status = str(status or "").strip().lower() or None
    normalized_sentiment = str(sentiment or "").strip().lower() or None
    normalized_limit = _parse_query_int(limit, default=100, minimum=1, maximum=1000)

    return DashboardService.list_mentions(
        user_id=user_id,
        batch_id=selected_batch_id,
        status=normalized_status,
        sentiment=normalized_sentiment,
        limit=normalized_limit,
        company_slug=company_slug or company_id,
        period_from=period_from,
        period_to=period_to,
    )


@app.get("/api/insights")
async def insights(
    batch_id: str | None = Query(None),
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    include_archived: bool = Query(False),
    priority: str | None = Query(None),
    resolution: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista insights persistidos em ordem cronologica (mais recente primeiro)."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    items = InsightService.list_insights(
        user_id=user_id,
        include_archived=include_archived,
        limit=limit,
        batch_id=batch_id,
        company_slug=company_slug or company_id,
        period_from=period_from,
        period_to=period_to,
        priority=priority,
        resolution=resolution,
    )
    return {"items": items}


@app.post("/api/insights/generate")
async def generate_insight(
    payload: InsightGenerateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Enfileira e processa um insight para batch elegivel, respeitando limiar minimo."""
    user_id = str(current_user.get("_id") or current_user.get("id"))

    try:
        generated = await InsightService.generate_insight(
            user_id=user_id,
            batch_id=payload.batch_id,
            force=payload.force,
        )
        return {"ok": True, "item": generated}
    except InsightGenerationError as exc:
        safe_message = str(exc.message or "Nao foi possivel gerar insight")
        details = exc.details if isinstance(exc.details, dict) else {}
        expected_state = str(exc.code or "") == "threshold_not_met"
        reason = str(details.get("reason") or exc.code or "insight_generation_error")
        actionable_message = str(details.get("actionable_message") or safe_message)
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": safe_message,
                "detail": safe_message,
                "code": str(exc.code or "insight_generation_error"),
                "reason": reason,
                "expected_state": expected_state,
                "business_state": {
                    "type": "expected_business_rule" if expected_state else "error",
                    "expected": expected_state,
                    "reason": reason,
                    "actionable_message": actionable_message,
                },
                "meta": details,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/insights/{insight_id}/archive")
async def archive_insight(
    insight_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    changed = InsightService.archive_insight(user_id=user_id, insight_id=insight_id)
    if not changed:
        raise HTTPException(status_code=404, detail="Insight nao encontrado")
    return {"ok": True}


@app.delete("/api/insights/{insight_id}")
async def delete_insight(
    insight_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    deleted = InsightService.delete_insight(user_id=user_id, insight_id=insight_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Insight nao encontrado")
    return {"ok": True}


@app.post("/api/insights/{insight_id}/regenerate")
async def regenerate_insight(
    insight_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        regenerated = await InsightService.regenerate_insight(user_id=user_id, insight_id=insight_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "item": regenerated}


@app.get("/api/settings")
async def get_settings(current_user: dict[str, Any] = Depends(get_current_user)):
    """Retorna configuracoes persistidas do usuario autenticado."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return InsightService.get_user_settings(user_id=user_id)


@app.put("/api/settings")
async def update_settings(
    payload: UserSettingsUpdateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Atualiza tema, idioma e limiar minimo da LLM para o usuario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return InsightService.update_user_settings(
            user_id=user_id,
            locale=payload.locale,
            theme=payload.theme,
            llm_trigger_min_comments=payload.llm_trigger_min_comments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/chat/threads")
async def list_chat_threads(
    limit: int = Query(20, ge=1, le=200),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista threads de chat do usuario autenticado."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return {"items": ChatService.list_threads(user_id=user_id, limit=limit)}


@app.post("/api/chat/threads")
async def create_chat_thread(
    payload: ChatThreadCreateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Cria nova thread de chat no escopo do usuario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    locale = InsightService.get_user_settings(user_id=user_id).get("locale", "pt-BR")
    item = ChatService.create_thread(user_id=user_id, title=payload.title, locale=str(locale))
    return {"item": item}


@app.get("/api/chat/threads/{thread_id}/messages")
async def list_chat_messages(
    thread_id: str,
    limit: int = Query(100, ge=1, le=500),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista mensagens de uma thread valida do usuario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        items = ChatService.list_messages(user_id=user_id, thread_id=thread_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items}


@app.delete("/api/chat/threads/{thread_id}")
async def delete_chat_thread(
    thread_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exclui uma thread de chat e todas as suas mensagens (escopo do usuário)."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        deleted = ChatService.delete_thread(user_id=user_id, thread_id=thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread nao encontrada")
    return {"ok": True}


@app.delete("/api/chat/threads/{thread_id}/messages/{message_id}")
async def delete_chat_thread_message(
    thread_id: str,
    message_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exclui uma mensagem da thread do usuário."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        deleted = ChatService.delete_message(user_id=user_id, thread_id=thread_id, message_id=message_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Mensagem nao encontrada")
    return {"ok": True}


@app.delete("/api/chat/threads")
async def delete_all_chat_threads(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exclui todas as threads e mensagens de chat do usuário autenticado."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    result = ChatService.delete_all_threads(user_id=user_id)
    return {"ok": True, **result}


@app.post("/api/chat/threads/{thread_id}/messages")
async def send_chat_message(
    thread_id: str,
    payload: ChatMessageCreateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Recebe mensagem do usuario e responde via chat restrito ao dominio."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    locale = str(InsightService.get_user_settings(user_id=user_id).get("locale", "pt-BR"))
    try:
        result = await ChatService.send_message(
            user_id=user_id,
            thread_id=thread_id,
            content=payload.content,
            locale=locale,
        )
    except ChatUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        safe_detail = str(exc)
        status_code = 404 if "thread nao encontrada" in safe_detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=safe_detail) from exc
    return result


@app.post("/api/analyze")
@limiter.limit(f"{settings.rate_limit_analyze_per_minute}/minute")
async def analyze(
    request: Request,
    payload: AnalyzeRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Analisa um texto manualmente e salva como menção do usuário."""
    db = get_db()
    user_id = str(current_user.get("_id") or current_user.get("id"))
    text = payload.text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="Texto não pode estar vazio")

    search_id = f"manual-{user_id}"
    query = payload.brand_name or "Análise Manual"
    mention = normalize_mention(
        query=query,
        source=payload.source or "manual",
        text=text,
        author="manual",
        published_at=utcnow(),
        raw={"manual": True},
    )
    if not mention:
        raise HTTPException(status_code=400, detail="Texto não pode estar vazio")

    enrichment = EnrichmentService.analyze_mention(mention["text"], mention.get("rating"))
    llm_analysis = await LLMService.analyze_single_mention(mention["text"])

    merged_aspects = list(enrichment.get("aspects") or [])
    for aspect in (llm_analysis.get("aspect_sentiment") or {}).keys():
        if aspect not in merged_aspects:
            merged_aspects.append(aspect)

    mention.update(enrichment)
    mention.update(
        {
            "sentiment": llm_analysis.get("sentiment", enrichment.get("sentiment", "neutro")),
            "confidence": round(float(llm_analysis.get("confidence_score", enrichment.get("confidence", 0.55)) or 0.55), 3),
            "confidence_score": round(float(llm_analysis.get("confidence_score", enrichment.get("confidence", 0.55)) or 0.55), 3),
            "urgency_score": round(float(llm_analysis.get("urgency_score", enrichment.get("urgency_score", 0.0)) or 0.0), 4),
            "criticality": llm_analysis.get("criticality", enrichment.get("criticality", "baixa")),
            "urgency_factors": llm_analysis.get("urgency_factors") or [],
            "aspect_sentiment": llm_analysis.get("aspect_sentiment") or {},
            "summary": llm_analysis.get("summary") or "",
            "aspects": merged_aspects,
        }
    )
    mention.update({
        "user_id": user_id,
        "search_id": search_id,
        "brand_name": query,
    })

    result = db.mentions.insert_one(mention)
    mention["_id"] = result.inserted_id
    del request
    return SearchService.serialize(mention)


@app.get("/api/search/history")
async def search_history(
    limit: int = Query(20, ge=1, le=100),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Histórico de buscas do usuário."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return {"history": SearchService.history(user_id, limit=limit)}


@app.get("/api/alerts")
async def alerts(
    search_id: str | None = Query(None),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista alertas internos, com filtro opcional por search_id."""
    db = get_db()
    user_id = str(current_user.get("_id") or current_user.get("id"))
    query = {"user_id": user_id}
    if search_id:
        query["search_id"] = search_id

    data = list(db.alerts.find(query).sort("created_at", -1).limit(100))
    return {"alerts": SearchService.serialize_many(data)}


@app.get("/api/reports")
async def list_reports(
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista relatórios por empresa e período sem expor IDs internos."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return ReportService.list_reports_filtered(
            user_id=user_id,
            company_id=company_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
            limit=limit,
            offset=offset,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/reports/export")
async def export_filtered_report(
    report_format: str = Query("csv", alias="format"),
    company_id: str | None = Query(None, alias="companyId"),
    company_slug: str | None = Query(None, alias="companySlug"),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exporta relatórios por filtro de empresa/período (CSV ou PDF)."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        return ReportService.export_filtered(
            user_id=user_id,
            report_format=report_format,
            company_id=company_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "nenhum" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.get("/api/reports/csv")
async def export_csv(
    search_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exporta CSV real filtrado pelo search_id."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return ReportService.export_csv(user_id, search_id)


@app.get("/api/reports/pdf")
async def export_pdf(
    search_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exporta PDF executivo profissional filtrado pelo search_id."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return ReportService.export_pdf(user_id, search_id)


@app.get("/api/reports/export/{report_format}")
async def export_latest_report(
    report_format: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Compatibilidade com o frontend: exporta CSV/PDF da última busca concluída."""
    db = get_db()
    user_id = str(current_user.get("_id") or current_user.get("id"))
    last = db.search_jobs.find_one({"user_id": user_id, "status": "completed"}, sort=[("created_at", -1)])
    if not last:
        raise HTTPException(status_code=404, detail="Nenhuma busca concluída para exportar")

    search_id = last["search_id"]
    if report_format == "csv":
        return ReportService.export_csv(user_id, search_id)
    if report_format == "pdf":
        return ReportService.export_pdf(user_id, search_id)

    raise HTTPException(status_code=400, detail="Formato inválido. Use csv ou pdf")


@app.get("/api/insights/export/markdown")
async def export_insights_markdown(
    priority: str | None = Query(None),
    resolution: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exporta insights filtrados em Markdown."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return ReportService.export_insights_markdown(
        user_id=user_id,
        priority=priority,
        resolution=resolution,
        limit=limit,
    )


@app.get("/api/insights/export/pdf")
async def export_insights_pdf(
    priority: str | None = Query(None),
    resolution: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Exporta insights filtrados em PDF executivo."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return ReportService.export_insights_pdf(
        user_id=user_id,
        priority=priority,
        resolution=resolution,
        limit=limit,
    )


@app.delete("/api/dev/clear-data")
async def clear_data(current_user: dict[str, Any] = Depends(get_current_user)):
    """Limpa dados do usuário logado.

    Uso apenas em desenvolvimento/testes.
    """
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Banco de dados indisponivel")

    user_id = str(current_user.get("_id") or current_user.get("id"))
    collections = [
        "mentions",
        "search_jobs",
        "alerts",
        "reports",
        "insight_jobs",
        "insights",
        "chat_messages",
        "chat_threads",
        "comment_batches",
        "nps_responses",
        "privacyconsents",
        "user_consents",
    ]
    for collection_name in collections:
        collection = getattr(db, collection_name)
        collection.delete_many({"user_id": user_id})
    db.dashboard_settings.delete_many({"user_id": user_id})
    return {"ok": True, "message": "Dados do usuário removidos"}

# ==================== DATA MANAGEMENT (DELETE) ====================

@app.delete("/api/conversations/{id}")
async def delete_conversation(id: str, current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        deleted = ChatService.delete_thread(user_id=user_id, thread_id=id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    return {"ok": True}

@app.delete("/api/conversations/{id}/messages/{msg_id}")
async def delete_message(id: str, msg_id: str, current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        deleted = ChatService.delete_message(user_id=user_id, thread_id=id, message_id=msg_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Mensagem não encontrada")
    return {"ok": True}

@app.delete("/api/conversations/all")
async def delete_all_conversations(current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    result = ChatService.delete_all_threads(user_id=user_id)
    return {"ok": True, **result}

@app.delete("/api/searches/{id}")
async def delete_search(id: str, current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    db = get_db()
    result = db.search_jobs.delete_one({"search_id": id, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Pesquisa não encontrada")
    db.mentions.delete_many({"search_id": id, "user_id": user_id})
    return {"ok": True}

@app.delete("/api/searches/all")
async def delete_all_searches(current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    db = get_db()
    db.search_jobs.delete_many({"user_id": user_id})
    db.mentions.delete_many({"user_id": user_id})
    return {"ok": True}

@app.delete("/api/insights/all")
async def delete_all_insights(current_user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    db = get_db()
    db.insights.delete_many({"user_id": user_id})
    return {"ok": True}

@app.delete("/api/user/data/all")
@app.delete("/api/user-data/all")
async def delete_all_user_data(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(current_user.get("_id") or current_user.get("id"))
    db = get_db()
    collections = [
        "mentions", "search_jobs", "alerts", "reports", "insight_jobs", 
        "insights", "chat_messages", "chat_threads", "comment_batches", 
        "nps_responses", "scraped_items", "scrape_cache", "privacyconsents", "user_consents"
    ]
    for coll in collections:
        db[coll].delete_many({"user_id": user_id})
    db.dashboard_settings.delete_many({"user_id": user_id})

    db.audit_logs.insert_one(
        {
            "user_id": user_id,
            "action": "data_deletion_request",
            "timestamp": utcnow(),
            "ip_hash": _hash_ip_prefix(request.client.host if request.client else None),
        }
    )

    return {"ok": True, "message": "Todos os dados do usuário foram apagados com sucesso"}


# ==================== NPS ====================

@app.post("/api/nps/submit")
async def nps_submit(
    payload: NpsSubmitRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Registra resposta NPS do usuario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    try:
        result = NpsService.submit_response(
            user_id=user_id,
            session_id=payload.session_id,
            module_key=payload.module_key,
            score=payload.score,
            comment=payload.comment,
            route=payload.route,
            context_metadata=payload.context_metadata,
        )
        return {"ok": True, "item": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/nps/dismiss")
async def nps_dismiss(
    payload: NpsDismissRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Registra que o usuario adiou/fechou o NPS."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    result = NpsService.submit_dismiss(
        user_id=user_id,
        session_id=payload.session_id,
        module_key=payload.module_key,
        route=payload.route,
    )
    return {"ok": True, "item": result}


@app.get("/api/nps/check")
async def nps_check(
    session_id: str = Query(...),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Verifica se deve mostrar NPS ao usuario."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    return NpsService.should_show_nps(user_id=user_id, session_id=session_id)


@app.get("/api/nps/metrics")
async def nps_metrics(
    period_days: int | None = Query(None, ge=1, le=365),
    module_key: str | None = Query(None),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Retorna metricas NPS agregadas."""
    return NpsService.get_metrics(period_days=period_days, module_key=module_key)


# Routers adicionais para compatibilidade com endpoints especializados.
app.include_router(mentions_router, prefix="/api/mentions")
app.include_router(metrics_router, prefix="/api/metrics", tags=["Métricas"])
app.include_router(reports_router, prefix="/api/reports-admin")
app.include_router(admin_router, prefix="/api/admin")
