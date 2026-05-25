from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class SeedInsight(BaseModel):
    title: str
    summary: str
    priority: str
    urgency: float
    period_from: datetime | None = None
    period_to: datetime | None = None


class SeedDashboardMetrics(BaseModel):
    total_mentions: int
    positive_ratio: float
    neutral_ratio: float
    negative_ratio: float
    average_urgency: float
    urgency_evolution: list[dict[str, Any]] = Field(default_factory=list)
    top_negative_aspects: list[dict[str, Any]] = Field(default_factory=list)
    most_cited_aspects: list[dict[str, Any]] = Field(default_factory=list)


class SeedBatch(BaseModel):
    batch_key: str
    period_from: datetime
    period_to: datetime
    dashboard_metrics: SeedDashboardMetrics
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    insights: list[SeedInsight] = Field(default_factory=list)


class SeedCompany(BaseModel):
    slug: str
    name: str
    segment: Optional[str] = None
    batches: list[SeedBatch]


class DemoSeedPayload(BaseModel):
    companies: list[SeedCompany]
