import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth_router import router as auth_router
from app.api.ingestion_router import router as ingestion_router
from app.auth_utils import get_current_user
from app.config import settings
from app.database import MongoDB, get_db
from app.models import SearchRequest
from app.schemas import ChatMessageCreateRequest, ChatThreadCreateRequest, UserSettingsUpdateRequest
from app.services.chat_service import ChatService
from app.services.dashboard_service import DashboardService
from app.services.enrichment_service import EnrichmentService
from app.services.insight_service import InsightService
from app.services.llm_service import LLMService
from app.services.normalization_service import normalize_mention, utcnow
from app.services.report_service import ReportService
from app.services.search_service import SearchService

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    text: str
    brand_name: str | None = None
    source: str = "manual"


class InsightGenerateRequest(BaseModel):
    batch_id: str | None = None
    force: bool = False


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
                        sources=job.get("sources", ["google", "reddit", "x"]),
                        period_days=job.get("period_days", 30),
                        locality=job.get("locality"),
                        use_cache=False,
                    )
        except Exception as exc:
            logger.error("Erro no auto refresh: %s", exc)

        await asyncio.sleep(settings.AUTO_REFRESH_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await MongoDB.connect_db()

    task = None
    if settings.AUTO_REFRESH_ENABLED:
        task = asyncio.create_task(auto_refresh_loop())

    try:
        yield
    finally:
        if task:
            task.cancel()
        await MongoDB.close_db()


app = FastAPI(
    title="SentimentoIA API",
    description="Backend sem Apify: Google Places, Reddit, X/snscrape, OpenRouter/Ollama, MongoDB, CSV/PDF.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["Autenticação"])
app.include_router(ingestion_router, prefix="/api/ingestion", tags=["Ingestao"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Evita que exceções inesperadas virem resposta HTML quebrada no frontend."""
    request_id = uuid4().hex
    logger.exception("Erro nao tratado request_id=%s path=%s", request_id, request.url.path)

    error_message = "Erro interno. Tente novamente em instantes."
    if settings.PUBLIC_ERROR_VERBOSE:
        error_message = f"Erro interno: {type(exc).__name__}"

    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": error_message,
            "request_id": request_id,
            "path": str(request.url.path),
        },
    )


@app.get("/health")
async def health():
    """Healthcheck simples para saber se o backend está ativo."""
    return {"ok": True, "service": "SentimentoIA API", "version": "2.0.0"}


@app.get("/api/status/integrations")
async def integrations_status():
    """Mostra se as integrações principais estão configuradas."""
    llm = await LLMService.healthcheck()
    return {
        "google_places_configured": bool(settings.GOOGLE_PLACES_API_KEY and settings.GOOGLE_PLACES_API_KEY != "SUA_CHAVE_GOOGLE_PLACES"),
        "reddit_public_enabled": True,
        "x_snscrape_enabled": settings.X_SNSCRAPE_ENABLED,
        "mongodb_configured": bool(settings.MONGODB_URI),
        "llm": llm,
        "apify_removed": True,
    }


@app.post("/api/search")
async def search_mentions(payload: SearchRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """Executa busca real e salva tudo por search_id.

    Entrada esperada:
    {
      "brand_name": "Nike",
      "sources": ["google", "reddit", "x"],
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
    return result


@app.get("/api/dashboard")
async def dashboard(
    batch_id: str | None = Query(None),
    search_id: str | None = Query(None),
    period_days: int | None = Query(None, ge=1, le=365),
    limit_mentions: int = Query(200, ge=1, le=1000),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Retorna dashboard do pipeline processado (mentions status=processed)."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    selected_batch_id = batch_id or search_id
    return DashboardService.get_dashboard(
        user_id=user_id,
        batch_id=selected_batch_id,
        period_days=period_days,
        limit_mentions=limit_mentions,
    )


@app.get("/api/mentions")
async def mentions(
    batch_id: str | None = Query(None),
    search_id: str | None = Query(None),
    status: str = Query("processed"),
    sentiment: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Lista mencoes do pipeline processado, com filtros por batch/status/sentimento."""
    user_id = str(current_user.get("_id") or current_user.get("id"))
    selected_batch_id = batch_id or search_id

    return DashboardService.list_mentions(
        user_id=user_id,
        batch_id=selected_batch_id,
        status=status,
        sentiment=sentiment,
        limit=limit,
    )


@app.get("/api/insights")
async def insights(
    batch_id: str | None = Query(None),
    include_archived: bool = Query(False),
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
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


@app.post("/api/analyze")
async def analyze(
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

    mention.update(EnrichmentService.analyze_mention(mention["text"], mention.get("rating")))
    mention.update({
        "user_id": user_id,
        "search_id": search_id,
        "brand_name": query,
    })

    result = db.mentions.insert_one(mention)
    mention["_id"] = result.inserted_id
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


@app.delete("/api/dev/clear-data")
async def clear_data(current_user: dict[str, Any] = Depends(get_current_user)):
    """Limpa dados do usuário logado.

    Uso apenas em desenvolvimento/testes.
    """
    env_name = (settings.ENV or "").strip().lower()
    if env_name in {"production", "prod", "release"} or not settings.ENABLE_DEV_CLEAR_DATA:
        raise HTTPException(status_code=404, detail="Endpoint indisponivel")

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
    ]
    for collection_name in collections:
        collection = getattr(db, collection_name)
        collection.delete_many({"user_id": user_id})
    db.dashboard_settings.delete_many({"user_id": user_id})
    return {"ok": True, "message": "Dados do usuário removidos"}
