from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from fastapi.responses import Response, StreamingResponse

ExportHttpResponse = Response | StreamingResponse
DatasetCallable = Callable[["ExportContext"], dict[str, Any]]
TemplateCallable = Callable[[dict[str, Any], "ExportContext"], dict[str, Any]]
RendererCallable = Callable[[dict[str, Any], "ExportContext"], ExportHttpResponse]


@dataclass(slots=True)
class ExportContext:
    export_key: str
    user_id: str
    company_id: str | None = None
    company_slug: str | None = None
    period_from: datetime | None = None
    period_to: datetime | None = None
    period_days: int | None = None
    limit: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExportSpec:
    key: str
    dataset_loader: str
    template_loader: str
    renderer_loader: str

