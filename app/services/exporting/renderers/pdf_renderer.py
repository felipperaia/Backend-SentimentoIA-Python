import io
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import Response

from app.services.exporting.contracts import ExportContext


def _load_reportlab_components() -> dict[str, Any]:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Exportacao PDF indisponivel no momento. Verifique a dependencia reportlab no backend."
        ) from exc

    return {
        "colors": colors,
        "A4": A4,
        "ParagraphStyle": ParagraphStyle,
        "getSampleStyleSheet": getSampleStyleSheet,
        "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate,
        "Spacer": Spacer,
        "Table": Table,
        "TableStyle": TableStyle,
    }


def _build_styles(components: dict[str, Any]) -> dict[str, Any]:
    colors = components["colors"]
    styles = components["getSampleStyleSheet"]()
    ParagraphStyle = components["ParagraphStyle"]
    return {
        "title": ParagraphStyle(
            name="ExportTitle",
            parent=styles["Title"],
            textColor=colors.HexColor("#0B1220"),
            fontSize=20,
            leading=24,
            spaceAfter=10,
        ),
        "section": ParagraphStyle(
            name="ExportSection",
            parent=styles["Heading2"],
            textColor=colors.HexColor("#0B1220"),
            fontSize=13,
            leading=16,
            spaceBefore=6,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            name="ExportBody",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#1E293B"),
            fontSize=10,
            leading=13,
        ),
        "small": ParagraphStyle(
            name="ExportSmall",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#334155"),
            fontSize=9,
            leading=12,
        ),
    }


def _render_table(
    *,
    story: list[Any],
    components: dict[str, Any],
    table_model: dict[str, Any],
    col_widths: list[int] | None = None,
) -> None:
    colors = components["colors"]
    Table = components["Table"]
    TableStyle = components["TableStyle"]
    columns = table_model.get("columns") or []
    rows = table_model.get("rows") or []
    if not columns:
        return

    raw_data: list[list[str]] = [[str(column) for column in columns]]
    for row in rows:
        raw_data.append([str(item) for item in row])

    table = Table(raw_data, colWidths=col_widths)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)


def render_pdf_document(document_model: dict[str, Any], _: ExportContext) -> Response:
    components = _load_reportlab_components()
    styles = _build_styles(components)

    Paragraph = components["Paragraph"]
    Spacer = components["Spacer"]
    SimpleDocTemplate = components["SimpleDocTemplate"]
    from app.services.exporting.renderers.chart_renderer import render_chart

    filename = str(document_model.get("filename") or "report.pdf")
    title = str(document_model.get("title") or "Relatorio")
    subtitle_lines = document_model.get("subtitle_lines") or []
    sections = document_model.get("sections") or []

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=components["A4"],
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    story: list[Any] = []
    story.append(Paragraph(title, styles["title"]))
    for line in subtitle_lines:
        story.append(Paragraph(str(line), styles["small"]))
    story.append(Spacer(1, 10))

    for section in sections:
        kind = str(section.get("kind") or "").strip().lower()
        section_title = str(section.get("title") or "").strip()
        if section_title:
            story.append(Paragraph(section_title, styles["section"]))

        if kind == "text":
            for paragraph in section.get("paragraphs") or []:
                story.append(Paragraph(str(paragraph), styles["body"]))
            story.append(Spacer(1, 8))
            continue

        if kind == "kpi_cards":
            items = section.get("items") or []
            table_model = {
                "columns": [str(item.get("label") or "") for item in items],
                "rows": [[str(item.get("value") or "-") for item in items]],
            }
            _render_table(story=story, components=components, table_model=table_model)
            story.append(Spacer(1, 10))
            continue

        if kind == "table":
            _render_table(
                story=story,
                components=components,
                table_model={
                    "columns": section.get("columns") or [],
                    "rows": section.get("rows") or [],
                },
                col_widths=section.get("col_widths"),
            )
            story.append(Spacer(1, 10))
            continue

        if kind == "bullets":
            bullets = section.get("items") or []
            if bullets:
                for item in bullets:
                    story.append(Paragraph(f"• {item}", styles["body"]))
            else:
                story.append(Paragraph("Sem itens para este bloco.", styles["body"]))
            story.append(Spacer(1, 8))
            continue

        if kind == "charts":
            charts = section.get("items") or []
            if not charts:
                story.append(Paragraph("Sem dados para graficos.", styles["body"]))
                story.append(Spacer(1, 8))
                continue
            for chart in charts:
                drawing = render_chart(chart_spec=chart, width=470, height=210)
                if drawing is None:
                    story.append(Paragraph(f"{chart.get('title') or 'Grafico'}: sem dados.", styles["small"]))
                else:
                    story.append(drawing)
                story.append(Spacer(1, 8))
            continue

        if kind == "footer":
            generated_at = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
            story.append(Paragraph(str(section.get("text") or f"Gerado em {generated_at}"), styles["small"]))
            story.append(Spacer(1, 6))
            continue

        fallback_lines = section.get("paragraphs") or []
        for paragraph in fallback_lines:
            story.append(Paragraph(str(paragraph), styles["body"]))
        story.append(Spacer(1, 8))

    doc.build(story)
    payload = buffer.getvalue()
    buffer.close()

    return Response(
        content=payload,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
