from app.services.exporting.contracts import ExportContext, ExportHttpResponse
from app.services.exporting.registry import ExportRegistry


class ExportPipeline:
    def __init__(self, *, registry: ExportRegistry):
        self._registry = registry

    def execute(self, context: ExportContext) -> ExportHttpResponse:
        dataset_builder = self._registry.resolve_dataset(context.export_key)
        template_builder = self._registry.resolve_template(context.export_key)
        renderer = self._registry.resolve_renderer(context.export_key)

        view_model = dataset_builder(context)
        document_model = template_builder(view_model, context)
        return renderer(document_model, context)

