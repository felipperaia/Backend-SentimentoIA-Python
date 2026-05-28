# SentimentoIA Backend

Backend FastAPI da plataforma SentimentoIA.

## Arquitetura atual

- Sem scraper.
- Sem demo/seed/fallback.
- MongoDB primario (`MONGODB_URI`) e secundario (`SECONDARY_MONGODB_URI`).
- Banco secundario: staging de ingestao JSON.
- Banco primario: unica fonte de leitura da aplicacao.
- Chatbot responde via LLM (Ollama HTTP).
- Execucao em producao (HostingGuru), sem Docker e sem Redis.

## Fluxo de dados

1. Frontend envia lote JSON para `POST /api/ingestion/comments` (staging no Mongo secundario).
2. Frontend dispara `POST /api/search` para importar staging filtrado ao Mongo primario.
3. Dashboard/metrics/insights/chat leem somente do Mongo primario.

## Variaveis essenciais (.env)

- `SECRET_KEY`
- `ALGORITHM`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `REFRESH_TOKEN_EXPIRE_DAYS`
- `MONGODB_URI`
- `DATABASE_NAME`
- `SECONDARY_MONGODB_URI`
- `SECONDARY_DATABASE_NAME`
- `CORS_ORIGINS_CSV`
- `FRONTEND_URL`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `OLLAMA_TIMEOUT_SECONDS`

## Endpoints principais

- Auth: `/api/auth/*`
- Ingestao JSON: `POST /api/ingestion/comments`, `GET /api/ingestion/batches`, `GET /api/ingestion/batches/{batch_id}`
- Importacao para primario: `POST /api/search`
- Dashboard: `GET /api/dashboard`, `GET /api/mentions`, `GET /api/metrics`
- Insights: `GET /api/insights`, `POST /api/insights/generate`
- Chat: `/api/chat/threads/*`
- Relatorios: `/api/reports*`

## Deploy (HostingGuru)

- Build Command:

```bash
pip install -r requirements.txt
```

- Start Command (API + worker):

```bash
python -m app.hostingguru_start
```

- Health Check Path:

```text
/health
```
