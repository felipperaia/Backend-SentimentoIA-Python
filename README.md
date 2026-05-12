# backend-api-python — SentimentoIA (FastAPI + Worker + Prompts)

Visão geral

Repositório contendo a API principal (FastAPI), o worker de processamento assíncrono e os prompts usados pelo ChatService. Mantidos juntos para preservar caminhos relativos (ex.: `packages/prompts` lidos pelo backend).

Localização de arquivos importantes

- App FastAPI: `apps/api/app/` (`main.py`, `config.py`, `services/`, `api/`)
- Requirements: `apps/api/requirements.txt`
- Worker: `apps/worker/app/worker.py`
- Prompts LLM: `packages/prompts/` (contém `domain-closed-system-prompt.md` e `domain-knowledge-base.md`)
- Variáveis de exemplo: `apps/api/.env.example`, `apps/worker/.env.example`

Pré-requisitos

- Python 3.11+
- pip
- Opcional: Docker (se preferir containerizar)

Setup local (passos mínimos)

No PowerShell (Windows):

```powershell
cd repos-separados-20260506/backend-api-python/apps/api
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env -ErrorAction SilentlyContinue
python -m uvicorn app.main:app --reload --port 8000 --app-dir .
```

No Prompt de Comando (cmd):

```bat
cd repos-separados-20260506\backend-api-python\apps\api
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload --port 8000 --app-dir .
```

Observacao: em `cmd.exe`, comandos como `Copy-Item` e linhas iniciadas com `#` nao funcionam.

Em bash/macOS:

```bash
cd repos-separados-20260506/backend-api-python/apps/api
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
# editar .env
uvicorn app.main:app --reload --port 8000 --app-dir .
```

Worker (terminal separado)

```powershell
cd repos-separados-20260506/backend-api-python
# ativar venv de apps/api (ou o venv que preferir)
.\apps\api\.venv\Scripts\Activate.ps1
# certifique-se de ter apps/worker/.env configurado; o worker carrega ambos apps/worker/.env e apps/api/.env
python -m apps.worker.app.worker
```

Comandos importantes

- Instalar dependências: `pip install -r apps/api/requirements.txt`
- Iniciar API: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir apps/api`
- Iniciar worker: `python -m apps.worker.app.worker`
- Testes: `apps/api/venv/Scripts/python -m pytest apps/api/tests` (ajuste conforme seu venv)

Variáveis de ambiente críticas (copiar de `apps/api/.env.example`)

- `MONGODB_URI` — string de conexão do MongoDB Atlas
- `DATABASE_NAME` — nome do DB (ex.: `sentimento_db`)
- `SECRET_KEY` — chave JWT
- `FRONTEND_URL` — URL do frontend (CORS)
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`/`SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`/`SMTP_FROM_EMAIL`
- `LLM_PROVIDER`, `OLLAMA_BASE_URL`, `OLLAMA_API_KEY`, `OLLAMA_MODEL`
- `SCRAPER_DELAY_SECONDS`, `SCRAPER_TIMEOUT_SECONDS`, `SCRAPER_DEFAULT_LIMIT`, `SCRAPER_DEFAULT_SOURCES`
- `SCRAPER_RECLAMEAQUI_SEARCH_URL`, `SCRAPER_REDDIT_URL`, `SCRAPER_MASTODON_BASE_URL`, `SCRAPER_WEB_SEARCH_URL`
- `ENABLE_CHAT`, `ENABLE_INGESTION_API`, `ENABLE_EXPORTS` (flags)

Endpoint de scraping para POC

- `POST /api/scrape`
- Payload: `{ "query": "marca", "sources": ["reclameaqui", "reddit", "mastodon"], "limit_per_source": 5 }`
- Retorno: itens agrupados por fonte com retry/backoff, deduplicação incremental e persistência de checkpoints por fonte.

Observações sobre `packages/prompts` e `worker`

- `apps/api/app/services/chat_service.py` lê prompts de `packages/prompts` por caminho relativo:
  - `PROMPTS_DIR = Path(__file__).resolve().parents[4] / "packages" / "prompts"`
  - Por isso `packages/prompts` foi mantido dentro deste repo.
- O worker faz `sys.path` injection para `apps/api` e carrega `apps/worker/.env` e `apps/api/.env`. Configure ambas com as mesmas credenciais (MONGODB_URI, OLLAMA_*). No hosting, rode o worker como um serviço/process separado.

Deploy no hostingguru.io (recomendações)

1. Criar repositório `backend-api-python` no GitHub e push para `main`.
2. No hostingguru.io: criar novo App → conectar ao repositório.
3. Build step / instalar dependências:
   - Comando build (se aplica): `pip install -r apps/api/requirements.txt`
4. Start command (runtime):
   - `uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir apps/api`
   - Alternativa (Gunicorn): `gunicorn -k uvicorn.workers.UvicornWorker app.main:app -b 0.0.0.0:$PORT -w 2`
5. Configurar variáveis de ambiente no painel (MONGODB_URI, SECRET_KEY, FRONTEND_URL, OLLAMA_*, OPENROUTER_* etc.)
6. Health check: `GET /health` (defina no painel do provedor)
7. Worker: criar um segundo serviço/job apontando para o mesmo repositório e start command:
   - `python -m apps.worker.app.worker`
   - Garantir que o worker use as mesmas envs do app.

Recomendações de produção

- `ENV=production` e `DEBUG=false`.
- Não habilite `ENABLE_DEV_CLEAR_DATA` em produção.
- Proteja `OLLAMA_API_KEY` e outros segredos no painel de secrets do provedor.
- Use monitoramento de logs e alertas.

Healthcheck e testes pós-deploy

- `GET /health` para checar disponibilidade.
- `GET /api/status/integrations` para verificar integração com Mongo e LLM.
- Teste um fluxo simples de ingestão -> processamento pelo worker -> leitura no `/api/dashboard`.

Arquivos-chave e responsabilidades (resumo)

- `apps/api/app/main.py` — cria a FastAPI app, middleware CORS, inclui rotas e lifespan (conecta/desconecta MongoDB).
- `apps/api/app/config.py` — centraliza variáveis Pydantic (env_file `.env`).
- `apps/api/app/services/llm_service.py` — encapsula chamadas a Ollama/OpenRouter.
- `apps/worker/app/worker.py` — loop contínuo que processa menções; injeta `apps/api` no `sys.path` e carrega ambos `.env`.
- `packages/prompts/*` — prompts domain-closed e knowledge base usados pelo ChatService.

Nota sobre backups e migrações

- Não há migração automática incluída para Mongo (coleções são criadas dinamicamente). Faça backups e configure políticas no Atlas.

---
