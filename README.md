# SentimentoIA Backend (FastAPI + Worker)

Backend oficial da plataforma SentimentoIA para autenticacao, coleta de mencoes, processamento de sentimento, insights, chat restrito ao dominio e exportacoes.

## Sumario

- [Visao geral](#visao-geral)
- [Stack e arquitetura](#stack-e-arquitetura)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Pre-requisitos](#pre-requisitos)
- [Setup local](#setup-local)
- [Configuracao de ambiente](#configuracao-de-ambiente)
- [Como executar](#como-executar)
- [Documentacao da API](#documentacao-da-api)
- [Endpoints principais](#endpoints-principais)
- [Testes](#testes)
- [Deploy](#deploy)
- [Troubleshooting](#troubleshooting)

## Visao geral

Fluxo principal da aplicacao:

`frontend -> backend -> MongoDB + Ollama + pipelines de coleta/processamento`

Responsabilidades deste backend:

- autenticacao JWT e MFA
- ingestao e scraping de dados
- dashboard e mencoes por usuario autenticado
- geracao e exportacao de insights (Markdown/PDF)
- chat com contexto controlado por usuario
- processamento assincrono via worker

## Stack e arquitetura

- API: FastAPI
- Banco: MongoDB (Motor/PyMongo)
- IA: Ollama (integracao direta via backend)
- Worker: loop assincrono para processamento de fila e insights
- Testes: pytest + TestClient

## Estrutura de pastas

```text
apps/
   api/
      app/                # codigo da API (entrypoint: app.main:app)
      tests/              # testes da API
      .env.example        # template de configuracao
   worker/
      app/worker.py       # processo de worker
packages/
   prompts/              # prompts e base de conhecimento do dominio
```

## Pre-requisitos

- Python 3.11+
- MongoDB acessivel (local ou Atlas)
- Ollama acessivel para recursos de IA

## Setup local

Execute os comandos na raiz do repositorio.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r apps/api/requirements.txt
Copy-Item apps/api/.env.example apps/api/.env
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r apps/api/requirements.txt
cp apps/api/.env.example apps/api/.env
```

Opcional (somente se habilitar coletores baseados em Playwright):

```bash
python -m playwright install
```

## Configuracao de ambiente

O arquivo canonico de configuracao e `apps/api/.env` (utilizado pela API e pelo worker).

Variaveis essenciais:

- `MONGODB_URI`: conexao MongoDB
- `DATABASE_NAME`: nome do banco
- `SECRET_KEY`: chave JWT (obrigatorio trocar em producao)
- `FRONTEND_URL`: URL do frontend
- `CORS_ORIGINS_CSV`: origens CORS permitidas

Variaveis importantes para IA:

- `OLLAMA_BASE_URL`
- `OLLAMA_API_KEY` (quando necessario)
- `OLLAMA_MODEL`
- `OLLAMA_TIMEOUT_SECONDS`

Observacao de deploy:

- em producao, `OLLAMA_BASE_URL` deve apontar para endpoint remoto valido (sem `localhost`)

Variaveis importantes para worker:

- `WORKER_POLL_INTERVAL_SECONDS`
- `WORKER_BATCH_SIZE`
- `LLM_TRIGGER_MIN_COMMENTS`
- `LLM_MAX_SAMPLE_MENTIONS`

## Como executar

### 1) API

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir apps/api --reload
```

### 2) Worker (novo terminal, na raiz)

```bash
python -m apps.worker.app.worker
```

Observacao:

- o worker carrega automaticamente o mesmo `apps/api/.env`
- entrypoint recomendado da API: `app.main:app`
- `app.main_real:app` e legado e nao deve ser usado como padrao

## Documentacao da API

Com a API rodando localmente:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Healthcheck: `http://localhost:8000/health`

Autenticacao padrao dos endpoints protegidos:

- `Authorization: Bearer <token_jwt>`

## Endpoints principais

### Autenticacao (`/api/auth`)

- `POST /register`
- `POST /login`
- `GET /me`
- `PATCH /me`
- `POST /change-password`
- `GET /mfa/status`
- `POST /mfa/setup`
- `POST /mfa/enable`
- `POST /mfa/verify`
- `POST /mfa/disable`

### Ingestao (`/api/ingestion`)

- `POST /comments`
- `GET /batches`
- `GET /batches/{batch_id}`

### Busca, coleta e monitoramento

- `POST /api/search`
- `POST /api/scrape`
- `GET /api/search/history`
- `GET /api/dashboard`
- `GET /api/mentions`
- `GET /api/status/integrations`

Contrato de status para busca/coleta:

- `POST /api/search` e `POST /api/scrape` retornam `status` (`success|partial_success|empty|failed`)
- ambos retornam `status_summary` com falhas por fonte, incluindo `reason` e `timeout`

### Insights

- `GET /api/insights`
- `POST /api/insights/generate`
- `POST /api/insights/{insight_id}/regenerate`
- `POST /api/insights/{insight_id}/archive`
- `DELETE /api/insights/{insight_id}`
- `GET /api/insights/export/markdown`
- `GET /api/insights/export/pdf`

### Chat

- `GET /api/chat/threads`
- `POST /api/chat/threads`
- `GET /api/chat/threads/{thread_id}/messages`
- `POST /api/chat/threads/{thread_id}/messages`
- `DELETE /api/chat/threads/{thread_id}`
- `DELETE /api/chat/threads/{thread_id}/messages/{message_id}`
- `DELETE /api/chat/threads`

### Relatorios e privacidade

- `GET /api/reports/csv?search_id=...`
- `GET /api/reports/pdf?search_id=...`
- `GET /api/reports/export/{report_format}`
- `GET /api/privacy/policy`
- `POST /api/privacy/consent`

## Testes

Com ambiente virtual ativo, execute na raiz do repositorio:

```bash
python -m pytest apps/api/tests -q
```

Exemplo de execucao focada:

```bash
python -m pytest apps/api/tests/test_main_flow.py apps/api/tests/test_scrape.py -q
```

Observacao:

- os testes usam, por padrao, `DATABASE_NAME=sentimento_db_pytest`

## Deploy

Comandos de start recomendados:

- API:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir apps/api
```

- Worker:

```bash
python -m apps.worker.app.worker
```

### hostingguru.io (backend oficial)

Configurar o servico da API com:

- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir apps/api`
- Working directory: raiz do repositorio `backend-api-python`

Configurar o worker como servico separado com:

- Start command: `python -m apps.worker.app.worker`
- Mesmo arquivo de ambiente da API (`apps/api/.env`)

Checklist minimo de producao:

- `ENV=production`
- `DEBUG=false`
- `SECRET_KEY` forte e secreta
- `MONGODB_URI` de producao
- `FRONTEND_URL` publica
- `CORS_ORIGINS_CSV` sem localhost
- `OLLAMA_BASE_URL` apontando para endpoint remoto valido (sem localhost)

## Troubleshooting

- Erro `ModuleNotFoundError: app`:
   - execute a API com `--app-dir apps/api`
- Erro de CORS:
   - valide `FRONTEND_URL` e `CORS_ORIGINS_CSV`
- API sem recursos de IA:
   - valide `OLLAMA_BASE_URL` e timeout de rede
- Worker nao processa fila:
   - confirme conexao com MongoDB e valores de `WORKER_*`
- Falha em exportacoes:
   - confirme autenticacao JWT e existencia de dados do usuario
