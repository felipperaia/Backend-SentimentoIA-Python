from dataclasses import dataclass

from app.services.exporting.contracts import ExportSpec
from app.services.exporting.loaders import load_object


@dataclass(slots=True)
class ExportRegistry:
    _specs: dict[str, ExportSpec]

    def get(self, export_key: str) -> ExportSpec:
        try:
            return self._specs[export_key]
        except KeyError as exc:
            raise ValueError(f"Export key invalida: {export_key}") from exc

    def resolve_dataset(self, export_key: str):
        return load_object(self.get(export_key).dataset_loader)

    def resolve_template(self, export_key: str):
        return load_object(self.get(export_key).template_loader)

    def resolve_renderer(self, export_key: str):
        return load_object(self.get(export_key).renderer_loader)


def build_default_registry() -> ExportRegistry:
    return ExportRegistry(
        _specs={
            "mentions_csv_raw": ExportSpec(
                key="mentions_csv_raw",
                dataset_loader="app.services.exporting.datasets.mentions_dataset:build_mentions_csv_dataset",
                template_loader="app.services.exporting.templates.csv_raw_template:build_csv_raw_template",
                renderer_loader="app.services.exporting.renderers.csv_renderer:render_csv_document",
            ),
            "dashboard_pdf": ExportSpec(
                key="dashboard_pdf",
                dataset_loader="app.services.exporting.datasets.mentions_dataset:build_dashboard_dataset",
                template_loader="app.services.exporting.templates.pdf_dashboard_template:build_dashboard_pdf_template",
                renderer_loader="app.services.exporting.renderers.pdf_renderer:render_pdf_document",
            ),
            "metrics_pdf": ExportSpec(
                key="metrics_pdf",
                dataset_loader="app.services.exporting.datasets.metrics_dataset:build_metrics_dataset",
                template_loader="app.services.exporting.templates.pdf_metrics_template:build_metrics_pdf_template",
                renderer_loader="app.services.exporting.renderers.pdf_renderer:render_pdf_document",
            ),
            "insights_pdf": ExportSpec(
                key="insights_pdf",
                dataset_loader="app.services.exporting.datasets.insights_dataset:build_insights_dataset",
                template_loader="app.services.exporting.templates.pdf_insights_template:build_insights_pdf_template",
                renderer_loader="app.services.exporting.renderers.pdf_renderer:render_pdf_document",
            ),
        }
    )

