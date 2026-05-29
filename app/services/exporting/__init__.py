from app.services.exporting.contracts import ExportContext
from app.services.exporting.pipeline import ExportPipeline
from app.services.exporting.registry import ExportRegistry, build_default_registry

_PIPELINE: ExportPipeline | None = None


def get_export_pipeline() -> ExportPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = ExportPipeline(registry=build_default_registry())
    return _PIPELINE


__all__ = [
    "ExportContext",
    "ExportPipeline",
    "ExportRegistry",
    "build_default_registry",
    "get_export_pipeline",
]
