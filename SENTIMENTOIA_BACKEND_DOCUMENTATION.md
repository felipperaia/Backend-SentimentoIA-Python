# SentimentoIA Backend — Complete Technical Documentation

> **Version:** MVP/1.0  
> **Repository:** https://github.com/felipperaia/Backend-SentimentoIA-Python  
> **Language:** Python 3.11+  
> **Last documented:** May 2026

---

## 1. Visão Geral do Backend

### Produto e Propósito

**SentimentoIA** is a brand-reputation intelligence platform. The backend is the single source of truth for all data collection, sentiment analysis, AI-powered insights, user management, and reporting. Its mission is to allow businesses to monitor what is being said about their brand across multiple online sources and to receive actionable, AI-generated guidance.

### Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Web Framework | FastAPI (ASGI) |
| Database | MongoDB (pymongo sync driver) |
| AI/LLM | Ollama (self-hosted or remote, via HTTP) |
| Auth | JWT (python-jose), bcrypt, pyotp (TOTP MFA) |
| HTTP Client | httpx (async) |
| HTML Parsing | BeautifulSoup4 |
| Config | pydantic-settings + `.env` file |
| Email | SMTP (smtplib/aiosmtplib, configurable) |
| QR Codes | qrcode + Pillow |
| Testing | pytest |
| Dev DB Fallback | mongomock |
| Optional Browser Automation | Playwright (opt-in) |

### Main Responsibilities

- **API Gateway:** Exposes all product features via a RESTful FastAPI interface.
- **Business Logic:** Search pipeline, sentiment enrichment, reputation scoring, alert generation.
- **Data Collection:** Multi-source scraping (Reddit, YouTube, App Store, Play Store, Glassdoor, Trustpilot, Reclame Aqui, Mastodon, Web).
- **AI Integration:** Calls Ollama LLM for snapshot analysis, mention summarisation, and domain chat.
- **Authentication:** JWT-based auth with optional TOTP MFA and password reset via email.
- **NPS:** Net Promoter Score collection and module-level scoring.
- **Reports:** Per-search report generation and retrieval.
- **Ingestion:** External data push endpoint.
- **Admin:** User management endpoints scoped to the `admin` role.

### High-Level Architecture

The backend is a **synchronous-first monolith** exposing async FastAPI routes. All business logic lives in service classes under `app/services/`. Data is stored in a single MongoDB database. External sources are accessed via HTTP scraping (no official APIs for most sources). The LLM is consumed via Ollama's HTTP API.

```text
[Client / Frontend]
        │
        ▼ HTTP REST
[FastAPI Application  ← app/main.py]
        │
        ├── Routers (app/api/)
        │       ├── auth_router
        │       ├── mentions_router
        │       ├── reports_router
        │       ├── ingestion_router
        │       └── admin_router
        │
        ├── Services (app/services/)
        │       ├── AuthService
        │       ├── SearchService   ← main pipeline
        │       ├── CollectorService
        │       ├── Scraper collectors (scraper.py)
        │       ├── EnrichmentService
        │       ├── LLMService      ← Ollama HTTP
        │       ├── SentimentService
        │       ├── NormalizationService
        │       └── EmailService
        │
        ├── Models + Schemas (app/models.py, app/schemas/)
        │
        └── Database (app/database.py → MongoDB)
```

---

## 2. Arquitetura e Estrutura de Pastas

```text
Backend-SentimentoIA-Python/
└── apps/
    └── api/
        ├── .env                  ← Runtime secrets (not committed)
        ├── requirements.txt      ← Python dependencies
        ├── Dockerfile            ← (UNCERTAIN – not inspected)
        └── app/
            ├── main.py           ← FastAPI app, lifespan, CORS, router registration
            ├── config.py         ← Pydantic Settings (all env vars)
            ├── database.py       ← MongoDB connection manager + index creation
            ├── auth_utils.py     ← JWT creation/decoding, get_current_user dependency
            ├── models.py         ← Pydantic domain models (UserInDB, MentionInDB, etc.)
            ├── api/              ← FastAPI routers (HTTP boundary)
            │   ├── __init__.py
            │   ├── auth_router.py
            │   ├── mentions_router.py
            │   ├── reports_router.py
            │   ├── ingestion_router.py
            │   └── admin_router.py
            ├── schemas/          ← Request/response Pydantic schemas (DTOs)
            │   ├── __init__.py
            │   ├── user.py
            │   ├── chat.py
            │   ├── ingestion.py
            │   ├── mention.py
            │   ├── report.py
            │   └── settings.py
            └── services/         ← Business logic
                ├── auth_service.py
                ├── search_service.py
                ├── collector_service.py
                ├── scraper.py        ← All collector implementations
                ├── enrichment_service.py
                ├── llm_service.py
                ├── sentiment_service.py
                ├── normalization_service.py
                ├── email_service.py
                └── (others – UNCERTAIN: nps_service, report_service, chat_service)
```

### Folder Responsibilities

| Folder/File | Responsibility |
|---|---|
| `app/api/` | HTTP routing, request/response serialization, HTTP error handling. No business logic. |
| `app/services/` | All domain logic, external calls (Ollama, scrapers, SMTP), DB writes. |
| `app/schemas/` | Pydantic DTOs for validating incoming requests and shaping outgoing responses. |
| `app/models.py` | Domain Pydantic models for internal representation (DB documents). |
| `app/database.py` | MongoDB lifecycle management and index creation. |
| `app/auth_utils.py` | JWT encode/decode and `get_current_user` FastAPI dependency. |
| `app/config.py` | Centralised settings loaded from `.env`. Single source of truth for all configuration. |

---

## 3. Tecnologias e Stack

### Language

Python 3.11+ is assumed based on syntax usage (e.g., `str | None` union types, `match` expressions not present but pipe union used everywhere).

### Web Framework

**FastAPI** with Uvicorn as ASGI server. Routing is split by domain into separate `APIRouter` instances, all registered in `main.py`.

### Database

**MongoDB** via **pymongo** (synchronous driver). A comment in `database.py` explicitly states:

> *"Usa pymongo síncrono por simplicidade. Como as operações são pequenas no MVP, isso é suficiente. Para alta escala, migrar para Motor async."*

Connection parameters:
- `serverSelectionTimeoutMS=5000`
- `connectTimeoutMS=10000`
- `retryWrites=True`
- `w="majority"`

In `ENV=development`, if MongoDB is unreachable, the app falls back to **mongomock** (in-memory mock).

### ORM/ODM

No ORM. Raw `pymongo` driver calls with manual document construction and serialization via `SearchService.serialize()`.

### Background Jobs

No Celery, RQ, or similar task queue is present. Long-running operations (scraping + LLM analysis) are executed **synchronously within the request** using `asyncio` and `httpx`. `asyncio.to_thread()` is used to run blocking scraper calls (e.g., `PlayStoreCollector`, `AppStoreCollector`) without blocking the event loop.

### Caching

**No Redis.** Caching is implemented as a **MongoDB-backed TTL cache**: when `use_cache=True`, `SearchService.run_search()` queries `search_jobs` for a matching completed job within `CACHE_TTL_MINUTES`. If found, it returns the cached result instead of re-running the pipeline.

### Logging

Standard Python `logging` module. Logger instances are created per module with `logging.getLogger(__name__)`. Log level controlled by `LOG_LEVEL` env var (default: `"INFO"`). No structured logging (JSON) or external observability platform is configured.

### Testing

**pytest**. Test files are located in a `tests/` directory (path UNCERTAIN – not inspected in this session). `mongomock` is used as a DB fallback in `ENV=development`, enabling tests without a live MongoDB.

---

## 4. Rotas e Endpoints

All routes are prefixed in `main.py`. Exact prefix strings are UNCERTAIN (not inspected), but inferred from router filenames and conventions.

### Auth — `/api/auth`

#### `POST /api/auth/register`
- **Purpose:** Register a new user.
- **Auth:** None required.
- **Request body:**
  ```json
  {
    "email": "user@example.com",
    "name": "Full Name",
    "password": "strongpassword",
    "phone": "+55..."
  }
  ```
- **Response (201):**
  ```json
  {
    "access_token": "<JWT>",
    "token_type": "bearer",
    "user": { "id": "...", "email": "...", "name": "...", "role": "user" }
  }
  ```
- **Errors:**
  - `400` — Email already registered.
  - `500` — Internal error.
- **Side effects:** Creates document in `users` collection; writes `user_registered` audit log.

#### `POST /api/auth/login`
- **Purpose:** Authenticate with email + password. Returns JWT or MFA challenge.
- **Auth:** None.
- **Request body:**
  ```json
  {
    "email": "user@example.com",
    "password": "...",
    "mfa_code": "123456"
  }
  ```
- **Response (200):** `TokenResponse`.
- **Response (202):**
  ```json
  { "mfa_required": true, "message": "Codigo MFA necessario para concluir o login." }
  ```
- **Errors:**
  - `401` — Invalid credentials or invalid MFA code.
- **Side effects:** Updates `last_signed_in`; writes `user_login` or `user_login_mfa_*` audit logs.

#### `GET /api/auth/me`
- **Purpose:** Returns the currently authenticated user.
- **Auth:** Bearer JWT required.
- **Response (200):** `UserResponse` object.

#### `PATCH /api/auth/me`
- **Purpose:** Update `name` and/or `username`.
- **Auth:** Bearer JWT required.
- **Request body:** `{ "name": "...", "username": "..." }`.
- **Errors:** `400` if invalid or duplicate username.
- **Side effects:** Updates `users`; writes `user_profile_updated` audit log.

#### `POST /api/auth/change-password`
- **Purpose:** Change password by confirming current one.
- **Auth:** Bearer JWT.
- **Request body:** `{ "current_password": "...", "new_password": "..." }`.
- **Business rules:** New password must be ≥ 8 chars and different from the current one.
- **Errors:** `400` (validation), `401` (wrong current password).
- **Side effects:** Updates `password_hash`; clears any pending reset token.

#### `POST /api/auth/logout`
- **Purpose:** Stateless logout. Always returns success.
- **Auth:** None.

#### `GET /api/auth/mfa/status`
- **Auth:** Bearer JWT.
- **Response:** `{ "mfa_enabled": bool, "mfa_verified": bool }`.

#### `POST /api/auth/mfa/setup`
- **Purpose:** Generate TOTP secret and return QR code image.
- **Auth:** Bearer JWT.
- **Response:** `{ "status": "success", "secret": "...", "qr_code": "data:image/png;base64,..." }`.
- **Side effects:** Saves `mfa_secret` to user.

#### `POST /api/auth/mfa/enable`
- **Purpose:** Enable MFA after TOTP confirmation.
- **Auth:** Bearer JWT.
- **Request:** `{ "code": "123456" }`.

#### `POST /api/auth/mfa/verify`
- **Purpose:** Verify TOTP code without state change.
- **Auth:** Bearer JWT.
- **Request:** `{ "code": "123456" }`.

#### `POST /api/auth/mfa/disable`
- **Purpose:** Disable MFA with password confirmation.
- **Auth:** Bearer JWT.
- **Request:** `{ "password": "..." }`.

#### `POST /api/auth/password/forgot`
- **Purpose:** Request a password reset link.
- **Auth:** None.
- **Request:** `{ "email": "user@example.com" }`.
- **Response:** Always success to prevent user enumeration.

#### `POST /api/auth/password/reset`
- **Purpose:** Confirm password reset using token.
- **Auth:** None.
- **Request:** `{ "token": "...", "new_password": "..." }`.
- **Errors:** `400` — invalid or expired token.

### Mentions — `/api/mentions`

#### `POST /api/mentions/analyze`
- **Purpose:** Analyze a single mention text and return sentiment classification.
- **Request body:**
  ```json
  {
    "text": "The product is terrible...",
    "brand_id": "abc123",
    "source": "reclameaqui",
    "source_id": "ext-001",
    "author": "user123",
    "rating": 1.5,
    "url": "https://...",
    "published_at": "2025-01-01T00:00:00Z"
  }
  ```
- **Response:** `SentimentAnalysisResponse`.
- **Errors:** `400` for empty or oversized text; `500` on analysis failure.
- **Side effects:** Inserts into `sentiment_analysis` collection.

#### `GET /api/mentions/brand/{brand_id}/reputation`
- **Purpose:** Compute reputation score for a brand.
- **Response:** `ReputationScore`.

#### `GET /api/mentions/brand/{brand_id}/summary`
- **Purpose:** Aggregate summary of analyses for a brand.
- **Query params:** `start_date`, `end_date`.
- **Response:** totals, top themes, critical issues, average urgency.

#### `GET /api/mentions/brand/{brand_id}/critical`
- **Purpose:** Retrieve mentions with `urgency_score > 0.7`.
- **Query params:** `limit` (1–100).

### Reports — `/api/reports`

Routes defined in `reports_router.py`. Exact signatures UNCERTAIN. Expected operations:
- `POST /api/reports/`
- `GET /api/reports/`
- `GET /api/reports/{report_id}`
- `DELETE /api/reports/{report_id}`

### Ingestion — `/api/ingestion`

Defined in `ingestion_router.py`. Exact signatures UNCERTAIN.

### Admin — `/api/admin`

Defined in `admin_router.py`. Routes are scoped to `role=admin`.

### Search (UNCERTAIN path)

The `SearchService` is the core pipeline. It is likely exposed via a dedicated search router not captured in the inspected file list.

### Chat (UNCERTAIN path)

`LLMService.answer_domain_chat()` and chat collections exist. Router path UNCERTAIN.

### NPS (UNCERTAIN path)

`nps_responses` collection and settings exist. Router path UNCERTAIN.

---

## 5. Autenticação e Autorização

### Mechanism

**JWT (JSON Web Tokens)** using `python-jose` with `HS256`.

### Token Structure

```json
{
  "sub": "<user_id>",
  "role": "user",
  "type": "access",
  "exp": 1710000000,
  "iat": 1710000000
}
```

### Token Lifecycle

| Setting | Default | Description |
|---|---|---|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 30 | Access token expiry |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 7 | Refresh token (UNCERTAIN if implemented) |
| `SECRET_KEY` | `change-me-in-production` | HMAC signing secret |
| `ALGORITHM` | `HS256` | Signing algorithm |

### Validation Flow

1. `HTTPBearer` extracts the token.
2. `decode_access_token()` verifies signature and expiry.
3. `get_current_user()` loads the user from MongoDB.
4. Admin routes check `role == "admin"`.

### MFA

Optional TOTP-based MFA with `pyotp`. QR codes are generated server-side and returned as base64 PNG.

### Password Security

Passwords are hashed with bcrypt. Reset tokens are random, stored hashed with SHA-256, and expire by configuration.

### Audit Logging

Important auth actions are written to `audit_logs`. IP addresses are stored only as hashed prefixes.

---

## 6. Modelos de Dados e Base

### Database Choice

MongoDB was chosen for schema flexibility. No migration framework is present.

### Collections

#### `users`
Stores user account, auth, MFA, and password reset fields.

#### `search_jobs`
Tracks searches, metrics, LLM output, cache, and source errors.

#### `mentions`
Stores collected and enriched mentions, dedup signatures, and ranking.

#### Other collections
- `scraped_items`
- `source_checkpoints`
- `monitor_sources`
- `comment_batches`
- `insight_jobs`
- `insights`
- `chat_threads`
- `chat_messages`
- `dashboard_settings`
- `alerts`
- `reports`
- `audit_logs`
- `nps_responses`
- `sentiment_analysis`

### Indexes and Performance

Indexes are created in `database.py` for users, mentions, search jobs, reports, alerts, chat, insights, and support collections. `sentiment_analysis` indexing is UNCERTAIN.

---

## 7. Schemas e Validação

All schemas use Pydantic v2.

### User schemas
- `UserCreate`
- `UserLogin`
- `UserResponse`
- `UserProfileUpdate`
- `ChangePasswordRequest`
- `TokenResponse`
- `MFAVerify`
- `MFADisable`
- `MFAStatusResponse`
- `MFALoginChallenge`
- `PasswordReset`
- `PasswordResetConfirm`
- `PasswordResetResponse`

### Mention schemas
- `MentionCreate`
- `MentionResponse`
- `SentimentAnalysisResponse`
- `MentionFilter`
- `ReputationScore`

### Other schema modules
- `chat.py` — UNCERTAIN details
- `ingestion.py` — UNCERTAIN details
- `report.py` — UNCERTAIN details
- `settings.py` — UNCERTAIN details

### Enums
- `UserRole`
- `SentimentType`
- `CriticalityLevel`
- `DataSource`
- `ScrapeSource`

---

## 8. Serviços e Regra de Negócio

### `AuthService`

Handles registration, login, password hashing, profile updates, MFA setup/verification, password reset token generation, and audit logging.

### `SearchService`

Main pipeline:
1. Cache lookup.
2. Create `search_jobs` row.
3. Collect via `CollectorService`.
4. Deduplicate in memory.
5. Filter old mentions.
6. Deduplicate against DB.
7. Enrich via `EnrichmentService`.
8. Rank mentions.
9. Persist mentions.
10. Aggregate metrics.
11. Call `LLMService.analyze_mentions()`.
12. Generate alerts.
13. Finalize search job.

### Scraper collectors (`scraper.py`)

Collectors implemented:
- `RedditCollector`
- `YouTubeCollector`
- `AppStoreCollector`
- `PlayStoreCollector`
- `TrustpilotCollector`
- `GlassdoorCollector`
- `ReclameAquiCollector`
- `WebSearchCollector`
- `MastodonCollector`

Shared logic in `BaseCollector`:
- retries
- backoff
- user-agent rotation
- normalization
- dedup
- scoring

### `LLMService`

Integrates with Ollama for:
- snapshot analysis
- mention analysis
- domain chat
- health check

It sanitizes sensitive context before sending data to the model.

### `EnrichmentService`

UNCERTAIN implementation. Based on usage, it enriches mentions and aggregates metrics.

### `CollectorService`

UNCERTAIN implementation. Based on usage, it orchestrates source collectors and returns collected items plus errors.

### `SentimentService`

Used by mentions routes for detailed sentiment analysis and reputation score calculation.

### `EmailService`

Sends password reset email via SMTP and returns boolean success/failure.

### `NormalizationService`

Provides `utcnow()`, `make_search_id()`, and URL canonicalization helpers.

---

## 9. Integrações Externas

### Ollama

Configured by:
- `OLLAMA_BASE_URL`
- `OLLAMA_API_KEY`
- `OLLAMA_MODEL`
- `OLLAMA_TIMEOUT_SECONDS`

### Scraping targets
- Reddit
- YouTube
- Apple App Store
- Google Play Store
- Trustpilot
- Glassdoor
- Reclame Aqui
- DuckDuckGo
- Bing
- Mastodon

### SMTP

Configured by `SMTP_*` variables for password reset email sending.

### Error Handling and Fallbacks

- Retry + backoff for scraper HTTP requests.
- Bing fallback for generic web search.
- DuckDuckGo fallback for Glassdoor discovery.
- Safe empty analysis if Ollama is unavailable.
- Email failure does not block password reset response.

---

## 10. Segurança

### Main Security Practices
- Pydantic input validation.
- JWT auth.
- bcrypt password hashing.
- Optional MFA.
- CORS control.
- Sanitized public errors.
- Sensitive-data redaction before LLM calls.
- IP hashing in audit logs.

### Sensitive Data Handling
- Passwords are hashed, never stored in plain text.
- Reset tokens are hashed before storage.
- IPs are hashed by prefix.
- Sensitive context is stripped/redacted before LLM requests.

### Data Retention

`DATA_RETENTION_YEARS=2` exists in config, but automatic enforcement is UNCERTAIN.

---

## 11. Arquivos de Configuração e Variáveis de Ambiente

Configuration is loaded from `.env` via `pydantic-settings`.

### Important Variables
- `ENV`
- `DEBUG`
- `MONGODB_URI`
- `DATABASE_NAME`
- `SECRET_KEY`
- `ALGORITHM`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `REFRESH_TOKEN_EXPIRE_DAYS`
- `OLLAMA_BASE_URL`
- `OLLAMA_API_KEY`
- `OLLAMA_MODEL`
- `OLLAMA_TIMEOUT_SECONDS`
- `FRONTEND_URL`
- `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL`
- `SMTP_FROM_NAME`
- `SCRAPER_TIMEOUT_SECONDS`
- `SCRAPER_RETRY_ATTEMPTS`
- `SCRAPER_RETRY_BACKOFF_SECONDS`
- `SCRAPER_MIN_TEXT_LENGTH`
- `SCRAPER_DEFAULT_LIMIT`
- `SCRAPER_MAX_ITEMS_PER_SOURCE`
- `SCRAPER_MAX_TOTAL_ITEMS`
- `ENABLE_RECLAME_AQUI`
- `ENABLE_PLAYWRIGHT`
- `CACHE_TTL_MINUTES`
- `AUTO_REFRESH_ENABLED`
- `AUTO_REFRESH_INTERVAL_MINUTES`
- `CORS_ORIGINS_CSV`
- `MAX_TEXT_LENGTH`
- `BATCH_SIZE`
- `LLM_TRIGGER_MIN_COMMENTS`
- `LLM_MAX_SAMPLE_MENTIONS`
- `LOG_LEVEL`
- `NPS_COOLDOWN_DAYS`
- `NPS_MIN_INTERACTIONS`
- `NPS_ENABLED`
- `DATA_RETENTION_YEARS`
- `PRIVACY_CONTACT_EMAIL`

---

## 12. Background Jobs e Tarefas

No dedicated worker or queue system was found. Search and LLM processing run within the HTTP request lifecycle.

`AUTO_REFRESH_ENABLED` exists but implementation is UNCERTAIN.

---

## 13. Logs, Observabilidade e Monitoramento

### Logging Strategy
- Python `logging`
- Module-local loggers
- `LOG_LEVEL` from config
- stdout/stderr output

### Logged Areas
- DB connection
- Index creation
- auth events
- scraper errors
- LLM failures
- audit logs in MongoDB

### Debug Tips
- 401 errors: check JWT and `SECRET_KEY`
- empty LLM output: check Ollama URL/model
- no mentions: inspect source logs and feature flags
- CORS issues: inspect `FRONTEND_URL` and effective origins

---

## 14. Testes

### Expected Test Setup
- `pytest`
- `mongomock`
- FastAPI test client (UNCERTAIN)

### Run Locally

```bash
cd apps/api
pip install -r requirements.txt
pytest
```

Test directory details are UNCERTAIN because tests were not fully inspected.

---

## 15. Build, Deploy e CI/CD

### Local Run

```bash
git clone https://github.com/felipperaia/Backend-SentimentoIA-Python
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Minimum `.env`

```dotenv
ENV=development
MONGODB_URI=mongodb://localhost:27017
DATABASE_NAME=sentimento_db
SECRET_KEY=local-dev-secret-change-me
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3
FRONTEND_URL=http://localhost:5173
```

### CI/CD

No CI workflow was inspected. Status: UNCERTAIN.

---

## 16. Decisões de Arquitetura e Dívidas Técnicas

### Decisions
- Monolith for simplicity.
- MongoDB for schema flexibility.
- Synchronous pymongo in MVP.
- Ollama instead of hosted LLM gateway.
- MongoDB-backed cache instead of Redis.
- Stateless JWT logout.

### Technical Debt
- Sync DB driver inside async app.
- Long-running search requests.
- No visible refresh-token endpoint.
- No API rate limiting.
- Auto-refresh settings without confirmed worker implementation.
- LGPD retention policy not automatically enforced.
- Missing structured logging.
- Some routers and schemas not fully verified in this pass.

### Refactoring Suggestions
- Migrate to `motor`.
- Add background workers.
- Add refresh token flow.
- Add rate limiting.
- Add retention cleanup job.
- Add stronger indexing around `sentiment_analysis`.

---

## 17. Guia de Contribuição

### Onboarding
1. Clone repo.
2. Create `.env`.
3. Install dependencies.
4. Run app locally.
5. Review `config.py`, `database.py`, `search_service.py`, and `auth_service.py` first.

### Add New Endpoint
1. Add route in `app/api/`.
2. Add schemas in `app/schemas/`.
3. Implement service logic in `app/services/`.
4. Register route in `main.py`.
5. Create indexes if needed.
6. Add tests.

### Add New Source Integration
1. Extend enums.
2. Create collector subclass.
3. Register it in collector orchestration.
4. Add configuration in `config.py`.
5. Test normalization, dedup, and error handling.

### Conventions
- `snake_case` for identifiers.
- Services own business rules.
- Routers stay thin.
- Use `utcnow()`.
- Never hardcode secrets.
- Never expose internal upstream details in public errors.

---

## 18. Glossário de Termos

| Term | Definition |
|---|---|
| Search | A user-triggered monitoring run. |
| Search ID | Identifier linking search results and artifacts. |
| Mention | A collected review/post/comment about a brand. |
| Collector | Source-specific scraping class. |
| Enrichment | Post-processing of raw mentions. |
| Insight | AI-generated analysis of a data batch. |
| Snapshot | Structured aggregate payload sent to the LLM. |
| Dashboard | Aggregated view of a search. |
| Alert | Automated warning generated from critical data. |
| Report | Persisted document derived from a search. |
| NPS | Net Promoter Score feature. |
| Chat | Conversational AI over brand/domain data. |
| Reputation Score | 0–100 aggregate brand perception score. |
| Criticality | Urgency/severity classification. |
| Source Tier | Relative source quality/stability classification. |
| Cache | MongoDB-backed recent result reuse. |
| Batch | Grouped ingestion unit. |
| Audit Log | Security event record in MongoDB. |
| MFA | Multi-factor authentication. |
| TOTP | Time-based one-time password. |
| Ollama | Self-hosted LLM serving system. |
| LGPD | Brazilian data protection law. |
| mongomock | In-memory MongoDB mock for development/tests. |

---

## Status Notes

This document is grounded in direct source inspection of the repository. Any item labeled **UNCERTAIN** indicates that the related file or implementation detail was not fully inspected in this pass and should not be treated as confirmed behavior without checking the source.
