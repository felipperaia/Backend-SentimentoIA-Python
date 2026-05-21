# SentimentoIA Backend

Backend da plataforma SentimentoIA, responsavel por autenticacao, coleta de mencoes, analise de sentimento, insights e dados para dashboard.

## O que o sistema faz

- autentica usuarios (JWT)
- coleta mencoes de diferentes fontes
- classifica sentimento e urgencia
- gera dados para dashboard e relatorios
- oferece chat de apoio ao dominio da aplicacao

## Como funciona (visao simples)

Fluxo principal:

1. Usuario faz login.
2. Usuario executa busca/coleta.
3. Backend salva mencoes no MongoDB.
4. Sistema aplica analise (sentimento, urgencia, aspectos).
5. Dashboard e endpoints de metricas mostram os resultados.

## Componentes principais

- API: FastAPI
- Banco: MongoDB
- IA: Ollama (quando configurado)
- Worker: processamento de lote em segundo plano

## Estrutura resumida

```text
app/                    # codigo principal da API
app/api/                # rotas HTTP
app/services/           # regras de negocio
app/schemas/            # validacao de entrada/saida
apps/worker/            # worker de processamento
tests/                  # testes automatizados
.env.example            # exemplo de variaveis de ambiente
```

## Pre-requisitos

- Python 3.13 (recomendado no projeto atual)
- MongoDB acessivel
- Ollama acessivel (opcional, para recursos de IA)

## Setup rapido

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

## Variaveis essenciais (.env)

- `MONGODB_URI`
- `DATABASE_NAME`
- `SECRET_KEY`
- `FRONTEND_URL`
- `CORS_ORIGINS_CSV`
- `OLLAMA_BASE_URL` (se usar IA)
- `OLLAMA_MODEL` (se usar IA)

## Como executar

### API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Worker (terminal separado)

```bash
python -m apps.worker.app.worker
```

## Documentacao da API

- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Healthcheck: `http://localhost:8000/health`

## Endpoints principais

Autenticacao:

- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/refresh`
- `GET /api/auth/me`

Busca e analise:

- `POST /api/search`
- `POST /api/scrape`
- `POST /api/analyze`
- `GET /api/dashboard`
- `GET /api/mentions`
- `GET /api/metrics/classification`

Ingestao:

- `POST /api/ingestion/comments`
- `GET /api/ingestion/batches`

## Testes

Comando recomendado no ambiente atual:

```bash
py -3.13 -m pytest -q
```

## Resumo rapido

Se voce precisa subir o sistema localmente:

1. criar `.env` a partir de `.env.example`
2. configurar MongoDB
3. iniciar API
4. iniciar worker
5. acessar `/docs` para testar os endpoints
