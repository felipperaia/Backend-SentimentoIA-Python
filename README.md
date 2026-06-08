<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/FastAPI-0.111+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/MongoDB-PyMongo%204.7+-47A248?style=for-the-badge&logo=mongodb&logoColor=white" alt="MongoDB" />
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="MIT License" />
  <img src="https://img.shields.io/badge/Build-Passing-brightgreen?style=for-the-badge" alt="Build Status" />
</p>

<h1 align="center">SentimentoIA — Backend API</h1>

<p align="center">
High-performance REST API for AI-powered sentiment analysis, advanced authentication, and LLM-based processing.
</p>

## ✨ Features

- Fast and scalable REST API built with FastAPI
- JWT authentication with access + refresh token strategy
- 2FA with TOTP (Google Authenticator compatible)
- Built-in rate limiting for sensitive/expensive routes
- Ollama integration for local LLM-powered analysis
- Configurable urgency analysis engine (critical/high/medium)
- Batch comment ingestion and staging workflow
- NPS engine with cooldown and interaction rules
- Asynchronous SMTP email delivery
- PDF report generation and QR code support
- Dual MongoDB architecture (primary + staging)
- Configurable CORS for frontend integration
- Interactive API docs via Swagger UI (/docs) and ReDoc (/redoc)

## 🛠️ Stack

| Category | Technologies |
| --- | --- |
| Framework | FastAPI, Uvicorn[standard] |
| Database | MongoDB (PyMongo), dual-DB setup (primary + staging) |
| Auth | python-jose (JWT), passlib[bcrypt], pyotp (TOTP), slowapi (rate limiting), python-multipart |
| AI / LLM | Ollama HTTP integration, configurable model (default: llama3), httpx |
| Utilities | Pydantic v2, pydantic-settings, aiosmtplib, reportlab, qrcode[pil], Pillow |

## 📁 Project Structure

```text
Backend-SentimentoIA-Python/
├── app/
│   ├── __init__.py
│   ├── main.py                      # Main FastAPI entry point
│   ├── main_real.py                 # Alternative API entry point
│   ├── config.py                    # Application settings (Pydantic Settings)
│   ├── database.py                  # MongoDB connection and helpers
│   ├── models.py                    # Data models used by the application
│   ├── auth_utils.py                # JWT auth and MFA helper utilities
│   ├── hostingguru_start.py         # Hosting/start helper script
│   ├── api/
│   │   ├── __init__.py
│   │   ├── auth_router.py           # Auth routes (register, login, refresh, MFA, password flows)
│   │   ├── admin_router.py          # Admin routes and system management endpoints
│   │   ├── companies_router.py      # Company listing/management routes
│   │   └── ingestion_router.py      # Data/comment ingestion routes
│   ├── schemas/                     # Pydantic schemas for request/response payloads
│   │   ├── __init__.py
│   │   ├── chat.py
│   │   ├── ingestion.py
│   │   ├── mention.py
│   │   ├── privacy.py
│   │   ├── report.py
│   │   ├── settings.py
│   │   └── user.py
│   └── services/                    # Business logic and domain services
│       ├── __init__.py
│       ├── auth_service.py
│       ├── chat_service.py
│       ├── company_service.py
│       ├── company_utils.py
│       ├── controlledcontextservice.py
│       ├── dashboard_service.py
│       ├── email_service.py
│       ├── enrichment_service.py
│       ├── ingestion_service.py
│       ├── insight_service.py
│       ├── llm_service.py
│       ├── normalization_service.py
│       ├── nps_service.py
│       ├── processing_service.py
│       ├── report_service.py
│       ├── search_service.py
│       ├── urgency_engine.py
│       └── exporting/
│           ├── __init__.py
│           ├── contracts.py
│           ├── loaders.py
│           ├── pipeline.py
│           ├── registry.py
│           ├── datasets/
│           │   ├── __init__.py
│           │   ├── insights_dataset.py
│           │   ├── mentions_dataset.py
│           │   └── metrics_dataset.py
│           ├── renderers/
│           │   ├── __init__.py
│           │   ├── chart_renderer.py
│           │   ├── csv_renderer.py
│           │   └── pdf_renderer.py
│           └── templates/
│               ├── __init__.py
│               ├── base_theme.py
│               ├── csv_raw_template.py
│               ├── pdf_dashboard_template.py
│               ├── pdf_insights_template.py
│               └── pdf_metrics_template.py
├── apps/
│   └── worker/
│       ├── app/
│       │   ├── __init__.py
│       │   └── worker.py
│       └── tests/
├── docs/
│   ├── Auditoria-Correção.md
│   ├── Documentação Completa Backend - OLD.md
│   ├── Documentação Completa Backend.md
│   ├── endpoints.md
│   ├── entidade-exemplo.json
│   ├── espelho-json-explicacao.md
│   ├── kanban-back.md
│   ├── plano-correcao.md
│   ├── plano-refatoracao-exportacao.md
│   ├── relatorio-exportacao-finalizado.md
│   └── tests/
│       ├── conftest.py
│       ├── test_auth.py
│       ├── test_fixes.py
│       ├── test_insights_contract.py
│       ├── test_llm_gateway.py
│       ├── test_main_flow.py
│       ├── test_reports_export_contract.py
│       └── test_sentiment.py
├── examples/
├── .env.example
├── .gitignore
├── .python-version                # Python 3.12
├── requirements.txt               # Runtime dependencies
└── README.md
```

## ⚙️ Installation and Local Run

### Prerequisites

- Python 3.12+
- MongoDB running locally
- Ollama (optional, for local LLM features)

### 1) Clone the repository

```bash
git clone https://github.com/felipperaia/Backend-SentimentoIA-Python.git
cd Backend-SentimentoIA-Python
```

### 2) Create and activate a virtual environment

```bash
python -m venv .venv && source .venv/bin/activate
```

PowerShell alternative:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Configure environment variables

```bash
cp .env.example .env
```

PowerShell alternative:

```powershell
Copy-Item .env.example .env
```

### 5) Run the API

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 6) Access API documentation

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## 🌐 Environment Variables

The table below lists all environment variables with development-focused localhost examples.

| Variable | Description | Local Development Example |
| --- | --- | --- |
| `SECRET_KEY` | Secret used to sign JWT tokens | `change-me-with-a-strong-random-secret` |
| `ALGORITHM` | JWT algorithm | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access token lifetime in minutes | `60` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh token lifetime in days | `7` |
| `MONGODB_URI` | Primary MongoDB URI (source of truth) | `mongodb://localhost:27017` |
| `DATABASE_NAME` | Primary MongoDB database name | `sentimento_db` |
| `SECONDARY_MONGODB_URI` | Secondary MongoDB URI (staging ingestion) | `mongodb://localhost:27017` |
| `SECONDARY_DATABASE_NAME` | Secondary MongoDB database name | `sentimento_staging_db` |
| `CORS_ORIGINS_CSV` | Allowed CORS origins (CSV) | `http://localhost:5173` |
| `FRONTEND_URL` | Frontend base URL | `http://localhost:5173` |
| `OLLAMA_BASE_URL` | Ollama HTTP endpoint | `http://localhost:11434` |
| `OLLAMA_API_KEY` | Optional API key for Ollama gateway | `` |
| `OLLAMA_MODEL` | Ollama model name | `llama3` |
| `OLLAMA_TIMEOUT_SECONDS` | LLM request timeout in seconds | `120` |
| `SMTP_HOST` | SMTP server host | `smtp.provider.com` |
| `SMTP_PORT` | SMTP server port | `587` |
| `SMTP_USER` | SMTP username | `user@domain.com` |
| `SMTP_PASSWORD` | SMTP password | `change-me` |
| `SMTP_FROM_EMAIL` | Sender email used by the API | `noreply@domain.com` |
| `SMTP_FROM_NAME` | Sender display name | `SentimentoIA` |
| `LLM_TRIGGER_MIN_COMMENTS` | Minimum comments to trigger LLM processing | `5` |
| `LLM_MAX_SAMPLE_MENTIONS` | Max mentions sampled for LLM | `20` |
| `BATCH_SIZE` | Batch size for processing/ingestion | `50` |
| `MAX_TEXT_LENGTH` | Maximum allowed text length | `5000` |
| `URGENCY_THRESHOLD_CRITICAL` | Critical urgency score threshold | `0.80` |
| `URGENCY_THRESHOLD_HIGH` | High urgency score threshold | `0.60` |
| `URGENCY_THRESHOLD_MEDIUM` | Medium urgency score threshold | `0.35` |
| `URGENCY_PATTERNS_CRITICAL` | Regex-like keywords for critical urgency | `cancelamento\|processo judicial\|procon\|fraude` |
| `URGENCY_PATTERNS_HIGH` | Regex-like keywords for high urgency | `nunca mais\|horrivel\|cade meu dinheiro` |
| `URGENCY_PATTERNS_MEDIUM` | Regex-like keywords for medium urgency | `insatisfeito\|demora\|decepcionado` |
| `NPS_ENABLED` | Enable NPS prompts and scoring | `true` |
| `NPS_COOLDOWN_DAYS` | Cooldown between NPS prompts | `30` |
| `NPS_MIN_INTERACTIONS` | Minimum interactions before NPS prompt | `0` |
| `LOG_LEVEL` | Application log verbosity | `INFO` |
| `DATA_RETENTION_YEARS` | Data retention policy in years | `2` |
| `PRIVACY_CONTACT_EMAIL` | Privacy/governance contact email | `privacy@domain.com` |
| `AUTO_REFRESH_ENABLED` | Enable optional background refresh loop | `false` |
| `RATE_LIMIT_SEARCH_PER_MINUTE` | Search route requests/min limit | `5` |
| `RATE_LIMIT_ANALYZE_PER_MINUTE` | Analyze route requests/min limit | `10` |

## 🔐 Authentication

Authentication flow:

1. User registration: `POST /api/auth/register`
2. Login with credentials: `POST /api/auth/login`
3. API returns:
   - Access token (JWT, default 60 minutes)
   - Refresh token (default 7 days)
4. Token renewal: `POST /api/auth/refresh`
5. Optional 2FA (TOTP / Google Authenticator):
   - Setup: `POST /api/auth/mfa/setup`
   - Enable/verify: `POST /api/auth/mfa/enable` and `POST /api/auth/mfa/verify`

Additional route groups:

- Admin: `/api/admin/*`
- Companies: `/api/companies`
- Ingestion: `/api/ingestion/*`

## 🤖 LLM Integration (Ollama)

This backend can call a local Ollama instance via HTTP for sentiment and insight workflows.

### Local setup

1. Install Ollama: https://ollama.com
2. Pull a model:

```bash
ollama pull llama3
```

3. Ensure Ollama is running locally and reachable at:

```text
http://localhost:11434
```

4. Configure your `.env` values:

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3
OLLAMA_TIMEOUT_SECONDS=120
```

## 📊 Urgency Engine

The urgency engine classifies mentions/comments using configurable thresholds and pattern matching.

- CRITICAL when score >= `URGENCY_THRESHOLD_CRITICAL` (default `0.80`)
- HIGH when score >= `URGENCY_THRESHOLD_HIGH` (default `0.60`)
- MEDIUM when score >= `URGENCY_THRESHOLD_MEDIUM` (default `0.35`)

Pattern-based overrides can be tuned via:

- `URGENCY_PATTERNS_CRITICAL`
- `URGENCY_PATTERNS_HIGH`
- `URGENCY_PATTERNS_MEDIUM`

## 🚀 Deploy

Deploy on any Linux server with Python 3.12+ using Uvicorn with process supervision (for example, systemd) or Docker.

### Option A: Uvicorn + process manager

- Install dependencies in a virtual environment
- Configure `.env` with production-safe values
- Start with Uvicorn:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Run under a process manager (such as systemd) for restart/recovery

### Option B: Docker

Basic Dockerfile example:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 🤝 Contributing

1. Fork this repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Commit your changes: `git commit -m "feat: describe your change"`
4. Push your branch: `git push origin feature/your-feature-name`
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License.
