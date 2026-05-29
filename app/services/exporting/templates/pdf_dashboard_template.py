from datetime import datetime, timezone
from typing import Any

from app.services.exporting.contracts import ExportContext
from app.services.exporting.templates.base_theme import format_period


def build_dashboard_pdf_template(view_model: dict[str, Any], context: ExportContext) -> dict[str, Any]:
    filename = str(context.extra.get("filename") or "dashboard.pdf")
    company_name = str(view_model.get("company_name") or view_model.get("company_slug") or "Empresa")
    kpis = view_model.get("kpis") or {}
    charts = view_model.get("charts") or {}
    highlights = view_model.get("highlights") or []

    subtitle_lines = [
        f"Empresa: {company_name}",
        f"Periodo: {format_period(view_model.get('period_from'), view_model.get('period_to'))}",
        f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}",
    ]

    chart_items = [
        {
            "type": "pie",
            "title": "Distribuicao de sentimento",
            "data": charts.get("sentiment_distribution") or [],
        },
        {
            "type": "bar",
            "title": "Distribuicao por fonte",
            "data": charts.get("source_distribution") or [],
        },
    ]

    time_series = charts.get("time_series") or []
    if time_series:
        chart_items.append(
            {
                "type": "line",
                "title": "Evolucao temporal de volume e urgencia",
                "labels": [str(item.get("label") or "") for item in time_series],
                "series": [
                    {"name": "Volume", "values": [int(item.get("volume") or 0) for item in time_series]},
                    {"name": "Urgencia Media", "values": [float(item.get("avg_urgency") or 0.0) for item in time_series]},
                ],
            }
        )

    table_rows = [
        [
            str(item.get("source") or ""),
            str(item.get("sentiment") or ""),
            str(item.get("criticality") or ""),
            str(item.get("urgency") or ""),
            str(item.get("text") or ""),
        ]
        for item in highlights
    ]

    return {
        "filename": filename,
        "title": "Relatorio de Dashboard",
        "subtitle_lines": subtitle_lines,
        "sections": [
            {"kind": "text", "title": "Resumo", "paragraphs": view_model.get("summary_lines") or []},
            {
                "kind": "kpi_cards",
                "title": "KPIs",
                "items": [
                    {"label": "Total de mencoes", "value": int(kpis.get("total_mentions", 0) or 0)},
                    {"label": "Score reputacao", "value": round(float(kpis.get("reputation_score", 0.0) or 0.0), 4)},
                    {"label": "Tendencia", "value": str(kpis.get("trend") or "indefinido")},
                    {"label": "Mencoes criticas", "value": int(kpis.get("critical_mentions", 0) or 0)},
                    {"label": "Urgencia media", "value": round(float(kpis.get("average_urgency", 0.0) or 0.0), 4)},
                ],
            },
            {"kind": "charts", "title": "Graficos", "items": chart_items},
            {
                "kind": "table",
                "title": "Tabela de highlights",
                "columns": ["Fonte", "Sentimento", "Criticidade", "Urgencia", "Resumo"],
                "rows": table_rows or [["-", "-", "-", "-", "Sem highlights para o periodo."]],
                "col_widths": [60, 70, 70, 60, 220],
            },
            {"kind": "footer", "text": ""},
        ],
    }

