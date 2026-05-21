from __future__ import annotations

from typing import ClassVar, List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7

    # DB
    mongodb_uri: str
    database_name: str = "sentimento_db"

    # CORS
    cors_origins_csv: str = "https://sentimento-ai.netlify.app"

    @property
    def cors_origins(self) -> List[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins_csv.split(",") if origin.strip()]

    # Frontend
    frontend_url: str = "https://sentimento-ai.netlify.app"

    # Ollama
    ollama_base_url: str = ""
    ollama_api_key: str = ""
    ollama_model: str = "llama3"
    ollama_timeout_seconds: int = 120

    # Scraper
    scraper_timeout_seconds: int = 30
    scraper_retry_attempts: int = 2
    scraper_retry_backoff_seconds: float = 2.0
    scraper_request_delay_seconds: float = 1.0
    scraper_min_text_length: int = 20
    scraper_default_limit: int = 10
    scraper_max_items_per_source: int = 20
    scraper_max_total_items: int = 100

    # Features
    enable_reclame_aqui: bool = True
    enable_playwright: bool = False

    # Cache
    cache_ttl_minutes: int = 60

    # Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "SentimentoIA"

    # LLM thresholds
    llm_trigger_min_comments: int = 5
    llm_max_sample_mentions: int = 20
    batch_size: int = 50
    max_text_length: int = 5000

    # NPS
    nps_enabled: bool = True
    nps_cooldown_days: int = 30
    nps_min_interactions: int = 5

    # Logs
    log_level: str = "INFO"

    # LGPD
    data_retention_years: int = 2
    privacy_contact_email: str = ""

    # Auto refresh
    auto_refresh_enabled: bool = False
    auto_refresh_interval_minutes: int = 60

    _LEGACY_MAP: ClassVar[dict[str, str]] = {
        "ACCESS_TOKEN_EXPIRE_MINUTES": "access_token_expire_minutes",
        "ALGORITHM": "algorithm",
        "AUTO_REFRESH_ENABLED": "auto_refresh_enabled",
        "AUTO_REFRESH_INTERVAL_MINUTES": "auto_refresh_interval_minutes",
        "BATCH_SIZE": "batch_size",
        "CACHE_TTL_MINUTES": "cache_ttl_minutes",
        "CORS_ORIGINS": "cors_origins",
        "CORS_ORIGINS_CSV": "cors_origins_csv",
        "DATA_RETENTION_YEARS": "data_retention_years",
        "DATABASE_NAME": "database_name",
        "ENABLE_PLAYWRIGHT": "enable_playwright",
        "ENABLE_RECLAMEAQUI": "enable_reclame_aqui",
        "ENABLE_RECLAME_AQUI": "enable_reclame_aqui",
        "FRONTEND_URL": "frontend_url",
        "LLM_MAX_SAMPLE_MENTIONS": "llm_max_sample_mentions",
        "LLM_MODEL_EFFECTIVE": "ollama_model",
        "LLM_TRIGGER_MIN_COMMENTS": "llm_trigger_min_comments",
        "LOG_LEVEL": "log_level",
        "MAX_TEXT_LENGTH": "max_text_length",
        "MONGODB_URI": "mongodb_uri",
        "NPS_COOLDOWN_DAYS": "nps_cooldown_days",
        "NPS_ENABLED": "nps_enabled",
        "NPS_MIN_INTERACTIONS": "nps_min_interactions",
        "OLLAMA_API_KEY": "ollama_api_key",
        "OLLAMA_BASE_URL": "ollama_base_url",
        "OLLAMA_MODEL": "ollama_model",
        "OLLAMA_TIMEOUT_SECONDS": "ollama_timeout_seconds",
        "PRIVACY_CONTACT_EMAIL": "privacy_contact_email",
        "REFRESH_TOKEN_EXPIRE_DAYS": "refresh_token_expire_days",
        "SCRAPER_DEFAULT_LIMIT": "scraper_default_limit",
        "SCRAPER_DELAY_SECONDS": "scraper_request_delay_seconds",
        "SCRAPER_MAX_ITEMS_PER_SOURCE": "scraper_max_items_per_source",
        "SCRAPER_MAX_TOTAL_ITEMS": "scraper_max_total_items",
        "SCRAPER_MIN_TEXT_LENGTH": "scraper_min_text_length",
        "SCRAPER_REQUEST_DELAY_SECONDS": "scraper_request_delay_seconds",
        "SCRAPER_RETRY_ATTEMPTS": "scraper_retry_attempts",
        "SCRAPER_RETRY_BACKOFF_SECONDS": "scraper_retry_backoff_seconds",
        "SCRAPER_TIMEOUT_SECONDS": "scraper_timeout_seconds",
        "SECRET_KEY": "secret_key",
        "SMTP_EFFECTIVE_FROM_EMAIL": "smtp_from_email",
        "SMTP_EFFECTIVE_USERNAME": "smtp_user",
        "SMTP_FROM_EMAIL": "smtp_from_email",
        "SMTP_FROM_NAME": "smtp_from_name",
        "SMTP_HOST": "smtp_host",
        "SMTP_PASSWORD": "smtp_password",
        "SMTP_PORT": "smtp_port",
        "SMTP_USER": "smtp_user",
    }

    def __getattr__(self, name: str):
        if name == "OLLAMA_EFFECTIVE_URL":
            base_url = str(self.ollama_base_url or "").strip().rstrip("/")
            if not base_url:
                return ""
            if base_url.lower().endswith("/api"):
                return base_url
            return f"{base_url}/api"

        mapped = self._LEGACY_MAP.get(name)
        if mapped is not None:
            value = getattr(self, mapped)
            if name in {"SMTP_EFFECTIVE_USERNAME", "SMTP_EFFECTIVE_FROM_EMAIL"}:
                return str(value).strip()
            return value
        raise AttributeError(name)


settings = Settings()
