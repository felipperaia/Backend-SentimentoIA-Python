from datetime import datetime, timezone
from typing import Any

from app.services.exporting.contracts import ExportContext
from app.services.exporting.templates.base_theme import format_period, to_percent


def build_metrics_pdf_template(view_model: dict[str, Any], context: ExportContext) -> dict[str, Any]:
    filename = str(context.extra.get("filename") or "metrics.pdf")
    company_name = str(view_model.get("company_name") or view_model.get("company_slug") or "Empresa")
    sentiment_distribution = view_model.get("sentiment_distribution") or {}
    top_negative = view_model.get("top_negative_aspects") or []
    most_cited = view_model.get("most_cited_aspects") or []

    top_negative_rows = [
        [str(item.get("label") or item.get("aspect") or "-"), str(int(item.get("mentions") or item.get("count") or 0))]
        for item in top_negative[:10]
    ]
    most_cited_rows = [
        [str(item.get("label") or item.get("aspect") or "-"), str(int(item.get("mentions") or item.get("count") or 0))]
        for item in most_cited[:10]
    ]

    chart_items: list[dict[str, Any]] = [
        {
            "type": "pie",
            "title": "Distribuicao percentual de sentimento",
            "data": [
                ("Positivo", float(sentiment_distribution.get("positive", 0.0) or 0.0)),
                ("Neutro", float(sentiment_distribution.get("neutral", 0.0) or 0.0)),
                ("Negativo", float(sentiment_distribution.get("negative", 0.0) or 0.0)),
            ],
        }
    ]

    if top_negative_rows:
        chart_items.append(
            {
                "type": "bar",
                "title": "Comparativo de top aspectos negativos",
                "data": [(row[0], int(row[1])) for row in top_negative_rows],
            }
        )

    return {
        "filename": filename,
        "title": "Relatorio de Metricas",
        "subtitle_lines": [
            f"Empresa: {company_name}",
            f"Periodo: {format_period(view_model.get('period_from'), view_model.get('period_to'))}",
            f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}",
        ],
        "sections": [
            {
                "kind": "kpi_cards",
                "title": "KPIs",
                "items": [
                    {"label": "Total de mencoes", "value": int(view_model.get("total_mentions", 0) or 0)},
                    {"label": "Urgencia media", "value": round(float(view_model.get("average_urgency", 0.0) or 0.0), 4)},
                    {"label": "Sentimento positivo", "value": to_percent(float(sentiment_distribution.get("positive", 0.0) or 0.0))},
                    {"label": "Sentimento neutro", "value": to_percent(float(sentiment_distribution.get("neutral", 0.0) or 0.0))},
                    {"label": "Sentimento negativo", "value": to_percent(float(sentiment_distribution.get("negative", 0.0) or 0.0))},
                ],
            },
            {"kind": "charts", "title": "Comparativos", "items": chart_items},
            {
                "kind": "table",
                "title": "Top aspectos negativos",
                "columns": ["Aspecto", "Mencoes"],
                "rows": top_negative_rows or [["-", "0"]],
                "col_widths": [320, 120],
            },
            {
                "kind": "table",
                "title": "Aspectos mais citados",
                "columns": ["Aspecto", "Mencoes"],
                "rows": most_cited_rows or [["-", "0"]],
                "col_widths": [320, 120],
            },
            {"kind": "footer", "text": ""},
        ],
    }

