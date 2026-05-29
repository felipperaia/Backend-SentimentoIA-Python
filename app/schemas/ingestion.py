from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


ALLOWED_INGESTION_SOURCES = {
    "reddit",
    "youtube",
    "appstore",
    "playstore",
    "trustpilot",
    "glassdoor",
    "reclameaqui",
    "mastodon",
    "web",
    "manual_import",
}

SOURCE_ALIAS_MAP = {
    "reddit": "reddit",
    "youtube": "youtube",
    "youtubecom": "youtube",
    "appstore": "appstore",
    "appstoreapple": "appstore",
    "applestore": "appstore",
    "playstore": "playstore",
    "playstoregoogle": "playstore",
    "googleplay": "playstore",
    "trustpilot": "trustpilot",
    "glassdoor": "glassdoor",
    "reclameaqui": "reclameaqui",
    "reclameaquibr": "reclameaqui",
    "mastodon": "mastodon",
    "web": "web",
    "maraberto": "web",
    "openweb": "web",
    "manual": "manual_import",
    "manualimport": "manual_import",
}


def normalize_source_name(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    compact = "".join(char for char in raw if char.isalnum())
    return SOURCE_ALIAS_MAP.get(compact, compact)


class IngestionComment(BaseModel):
    """Contrato de item alinhado ao schema normalizado de menção."""

    model_config = ConfigDict(extra="allow")

    query: str | None = Field(default=None, min_length=1, max_length=160)
    entity: str | None = Field(default=None, min_length=1, max_length=160)
    company_name: str | None = Field(default=None, min_length=1, max_length=160)
    company_slug: str | None = Field(default=None, min_length=1, max_length=160)

    source: str = Field(..., min_length=2, max_length=60)
    text: str = Field(..., min_length=1, max_length=5000)
    author: str | None = Field(default="")
    published_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("published_at", "date", "created_at"),
    )

    url: str | None = Field(default=None, max_length=2048)
    canonical_url: str | None = Field(default=None, max_length=2048)
    location: str | None = Field(default=None, max_length=200)
    rating: float | None = Field(default=None, ge=0, le=5)

    external_id: str | None = Field(default=None, min_length=1, max_length=180)
    source_item_id: str | None = Field(default=None, min_length=1, max_length=180)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        normalized = normalize_source_name(value)
        if not normalized:
            raise ValueError("source nao pode estar vazio")
        if normalized not in ALLOWED_INGESTION_SOURCES:
            allowed = ", ".join(sorted(ALLOWED_INGESTION_SOURCES))
            raise ValueError(f"source invalido. fontes permitidas: {allowed}")
        return normalized

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("text nao pode estar vazio")
        return text

    @field_validator("author")
    @classmethod
    def normalize_author(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("url", "canonical_url", "location", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @model_validator(mode="after")
    def validate_company_reference(self) -> "IngestionComment":
        candidates = [self.company_slug, self.company_name, self.query, self.entity]
        if not any(str(item or "").strip() for item in candidates):
            raise ValueError("company_slug, company_name, query ou entity deve ser informado")

        if not (self.url or self.canonical_url):
            raise ValueError("url ou canonical_url deve ser informado")

        if self.published_at is None:
            raise ValueError("published_at (ou date/created_at) deve ser informado")

        if not (self.external_id or self.source_item_id):
            raise ValueError("external_id ou source_item_id deve ser informado")

        return self


class IngestionBatchRequest(BaseModel):
    comments: list[dict[str, Any]] = Field(..., min_length=1)


class IngestionRejectedItem(BaseModel):
    index: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


class IngestionBatchResponse(BaseModel):
    batch_id: str
    received: int
    inserted: int
    duplicates: int
    status: str


class IngestionBatchSummary(BaseModel):
    batch_id: str
    status: str
    received_count: int
    inserted_count: int
    duplicate_count: int
    company_slugs: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class IngestionStagingListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]] = Field(default_factory=list)


class IngestionCommitRequest(BaseModel):
    batch_id: str | None = Field(default=None, min_length=1, max_length=80)
    staging_ids: list[str] = Field(default_factory=list, max_length=5000)
    company_slug: str | None = Field(
        default=None,
        validation_alias=AliasChoices("company_slug", "companySlug", "company_id", "companyId"),
    )
    source: str | None = Field(default=None, max_length=60)
    limit: int = Field(default=2000, ge=1, le=20000)

    @field_validator("staging_ids")
    @classmethod
    def normalize_staging_ids(cls, value: list[str]) -> list[str]:
        unique: list[str] = []
        for item in value:
            candidate = str(item or "").strip()
            if candidate and candidate not in unique:
                unique.append(candidate)
        return unique

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_source_name(value)
        if not normalized:
            return None
        if normalized not in ALLOWED_INGESTION_SOURCES:
            allowed = ", ".join(sorted(ALLOWED_INGESTION_SOURCES))
            raise ValueError(f"source invalido. fontes permitidas: {allowed}")
        return normalized

    @field_validator("batch_id", "company_slug", mode="before")
    @classmethod
    def normalize_optional_scope(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @model_validator(mode="after")
    def validate_scope(self) -> "IngestionCommitRequest":
        if not self.batch_id and not self.staging_ids:
            raise ValueError("informe batch_id ou staging_ids para efetuar o commit")
        return self


class IngestionCommitResponse(BaseModel):
    commit_id: str
    status: str
    user_id: str
    selected: int
    inserted: int
    committed_at: datetime
    batch_id: str | None = None
    company_slug: str | None = None
    source: str | None = None
