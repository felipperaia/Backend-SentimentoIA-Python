from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Configurações centrais do backend.

    Manutenção:
    - Nunca coloque chaves reais aqui.
        - Use sempre o arquivo apps/api/.env.
    - O Apify foi removido do fluxo principal. As fontes reais agora são:
      Google Places API, Reddit público e X via snscrape.
    """

    ENV: str = "development"
    DEBUG: bool = True

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value):
        if isinstance(value, str) and value.lower() in {"release", "prod", "production"}:
            return False
        return value

    # MongoDB Atlas
    MONGODB_URI: str = "mongodb://localhost:27017"
    DATABASE_NAME: str = "sentimento_db"

    # LLM oficial do MVP: Ollama (local/cloud).
    LLM_PROVIDER: str = "ollama"
    OLLAMA_MODE: str = "local"  # local | cloud
    OLLAMA_LOCAL_URL: str = "http://localhost:11434"
    OLLAMA_CLOUD_URL: str = ""
    OLLAMA_API_KEY: str = ""
    OLLAMA_MODEL: str = "llama3.1:8b"

    # Compatibilidade com variáveis antigas do projeto.
    OLLAMA_ENABLED: bool = True
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Legado: mantido apenas para não quebrar ambientes antigos.
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_API_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "openrouter/free"
    GROK_API_KEY: str = ""
    GROK_API_URL: str = "https://openrouter.ai/api/v1"
    GROK_MODEL: str = "openrouter/free"

    @property
    def LLM_API_KEY(self) -> str:
        return (self.OPENROUTER_API_KEY or self.GROK_API_KEY or "").strip()

    @property
    def LLM_API_URL(self) -> str:
        return (self.OPENROUTER_API_URL or self.GROK_API_URL or "https://openrouter.ai/api/v1").strip()

    @property
    def LLM_MODEL(self) -> str:
        return (self.OPENROUTER_MODEL or self.GROK_MODEL or "openrouter/free").strip()

    @property
    def OLLAMA_EFFECTIVE_MODE(self) -> str:
        mode = (self.OLLAMA_MODE or "local").strip().lower()
        return "cloud" if mode == "cloud" else "local"

    @property
    def OLLAMA_EFFECTIVE_URL(self) -> str:
        if self.OLLAMA_EFFECTIVE_MODE == "cloud":
            return (self.OLLAMA_CLOUD_URL or "").strip()
        # Para modo local, prioriza OLLAMA_LOCAL_URL e mantém fallback legado.
        return (self.OLLAMA_LOCAL_URL or self.OLLAMA_BASE_URL or "").strip()

    # Coleta real sem Apify
    GOOGLE_PLACES_API_KEY: Optional[str] = None
    REDDIT_USER_AGENT: str = "webapp-sentimento/1.0"
    X_SNSCRAPE_ENABLED: bool = False
    X_MAX_RESULTS: int = 20

    # Cache e atualização automática
    CACHE_TTL_MINUTES: int = 30
    AUTO_REFRESH_ENABLED: bool = False
    AUTO_REFRESH_INTERVAL_MINUTES: int = 60

    # Segurança
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ENABLE_DEV_CLEAR_DATA: bool = False
    PUBLIC_ERROR_VERBOSE: bool = False

    # CORS local
    CORS_ORIGINS: list = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Limites operacionais
    MAX_TEXT_LENGTH: int = 5000
    BATCH_SIZE: int = 100
    WORKER_POLL_INTERVAL_SECONDS: int = 5
    WORKER_BATCH_SIZE: int = 50
    LLM_TRIGGER_MIN_COMMENTS: int = 20
    LLM_MAX_SAMPLE_MENTIONS: int = 40
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
