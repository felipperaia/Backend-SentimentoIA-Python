from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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
    rating: float | None = Field(default=None, ge=0, le=5)

    external_id: str | None = Field(default=None, min_length=1, max_length=180)
    source_item_id: str | None = Field(default=None, min_length=1, max_length=180)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            raise ValueError("source nao pode estar vazio")
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

    @model_validator(mode="after")
    def validate_company_reference(self) -> "IngestionComment":
        candidates = [self.company_slug, self.company_name, self.query, self.entity]
        if not any(str(item or "").strip() for item in candidates):
            raise ValueError("company_slug, company_name, query ou entity deve ser informado")
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
