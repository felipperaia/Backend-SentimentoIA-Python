from datetime import datetime, timezone
from typing import Any

from app.services.exporting.contracts import ExportContext
from app.services.exporting.templates.base_theme import format_period


def _priority_value(priority: str) -> int:
    normalized = str(priority or "").strip().lower()
    if normalized in {"high", "alta", "critical", "critica"}:
        return 3
    if normalized in {"medium", "media", "moderada"}:
        return 2
    return 1


def build_insights_pdf_template(view_model: dict[str, Any], context: ExportContext) -> dict[str, Any]:
    filename = str(context.extra.get("filename") or "insights.pdf")
    insights = view_model.get("insights") or []

    sorted_insights = sorted(
        insights,
        key=lambda item: (
            _priority_value(str(item.get("priority") or "")),
            float(item.get("urgency_score") or 0.0),
        ),
        reverse=True,
    )

    risk_items: list[str] = []
    opportunity_items: list[str] = []
    action_items: list[str] = []
    table_rows: list[list[str]] = []
    for insight in sorted_insights[:20]:
        summary = str(insight.get("executive_summary") or "")
        root_cause = str(insight.get("root_cause") or "")
        recommendation = str(insight.get("recommended_action") or "")
        if summary:
            risk_items.append(summary[:200])
        if root_cause:
            opportunity_items.append(root_cause[:200])
        if recommendation:
            action_items.append(recommendation[:200])

        table_rows.append(
            [
                str(insight.get("priority") or "medium"),
                str(insight.get("urgency") or "medium"),
                str(insight.get("status") or "open"),
                str(insight.get("resolution") or "pending"),
                summary[:120] or "Sem resumo",
            ]
        )

    if not risk_items:
        risk_items = ["Nenhum risco estruturado para os filtros informados."]
    if not opportunity_items:
        opportunity_items = ["Nenhuma oportunidade estruturada para os filtros informados."]
    if not action_items:
        action_items = ["Sem acoes recomendadas para os filtros informados."]

    subtitle_lines = [
        f"Empresa: {view_model.get('company_slug') or '-'}",
        f"Periodo: {format_period(view_model.get('period_from'), view_model.get('period_to'))}",
        f"Total de insights: {int(view_model.get('total_insights', 0) or 0)}",
        f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}",
    ]

    executive_summary = [
        f"Riscos prioritarios: {int(view_model.get('risk_count', 0) or 0)}",
        f"Itens abertos ou em andamento: {int(view_model.get('open_count', 0) or 0)}",
    ]

    return {
        "filename": filename,
        "title": "Relatorio de Insights",
        "subtitle_lines": subtitle_lines,
        "sections": [
            {"kind": "text", "title": "Resumo executivo", "paragraphs": executive_summary},
            {"kind": "bullets", "title": "Riscos", "items": risk_items[:8]},
            {"kind": "bullets", "title": "Oportunidades", "items": opportunity_items[:8]},
            {
                "kind": "table",
                "title": "Insights priorizados",
                "columns": ["Prioridade", "Urgencia", "Status", "Resolucao", "Resumo"],
                "rows": table_rows or [["-", "-", "-", "-", "Nenhum insight encontrado."]],
                "col_widths": [70, 70, 70, 80, 220],
            },
            {"kind": "bullets", "title": "Acoes recomendadas", "items": action_items[:10]},
            {"kind": "footer", "text": ""},
        ],
    }

