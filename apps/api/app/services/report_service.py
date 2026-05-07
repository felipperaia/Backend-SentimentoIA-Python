import csv
import io
from datetime import datetime
from typing import Any

from fastapi.responses import Response, StreamingResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.database import get_db
from app.services.search_service import SearchService


class ReportService:
    """Exportação real CSV/PDF baseada no MongoDB e filtrada por search_id."""

    @staticmethod
    def export_csv(user_id: str, search_id: str) -> StreamingResponse:
        db = get_db()
        mentions = list(db.mentions.find({"user_id": user_id, "search_id": search_id}, {"raw": 0}).sort("published_at", -1))

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "search_id", "query", "source", "author", "published_at", "rating",
            "sentiment", "confidence", "criticality", "urgency_score",
            "reputation_score", "aspects", "url", "text"
        ])

        for m in mentions:
            writer.writerow([
                m.get("search_id"),
                m.get("query"),
                m.get("source"),
                m.get("author"),
                m.get("published_at"),
                m.get("rating"),
                m.get("sentiment"),
                m.get("confidence"),
                m.get("criticality"),
                m.get("urgency_score"),
                m.get("reputation_score"),
                ";".join(m.get("aspects", [])),
                m.get("url"),
                (m.get("text") or "").replace("\n", " "),
            ])

        content = buffer.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="relatorio-{search_id}.csv"'},
        )

    @staticmethod
    def export_pdf(user_id: str, search_id: str) -> Response:
        db = get_db()
        dashboard = SearchService.dashboard(user_id, search_id)
        mentions = dashboard.get("mentions", [])
        metrics = dashboard.get("metrics", {})
        llm = dashboard.get("llm_analysis", {})

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story: list[Any] = []

        story.append(Paragraph("Relatório Executivo de Reputação Digital", styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Busca: <b>{dashboard.get('query') or ''}</b>", styles["Normal"]))
        story.append(Paragraph(f"Search ID: {search_id}", styles["Normal"]))
        story.append(Paragraph(f"Gerado em: {datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}", styles["Normal"]))
        story.append(Spacer(1, 16))

        story.append(Paragraph("1. Resumo Executivo", styles["Heading2"]))
        story.append(Paragraph(llm.get("executive_summary") or "Resumo indisponível.", styles["BodyText"]))
        story.append(Spacer(1, 12))

        story.append(Paragraph("2. Indicadores", styles["Heading2"]))
        table_data = [
            ["Métrica", "Valor"],
            ["Total de menções", str(metrics.get("total_mentions", 0))],
            ["Score de reputação", str(metrics.get("reputation_score", 0))],
            ["Tendência", str(metrics.get("trend", "indefinido"))],
            ["Menções críticas", str(metrics.get("critical_mentions", 0))],
        ]
        table = Table(table_data, colWidths=[220, 220])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f8fafc")),
        ]))
        story.append(table)
        story.append(Spacer(1, 12))

        story.append(Paragraph("3. Riscos e Oportunidades", styles["Heading2"]))
        risks = llm.get("risks") or []
        opportunities = llm.get("opportunities") or []
        story.append(Paragraph("<b>Riscos:</b> " + ("; ".join(risks) if risks else "Nenhum risco estruturado."), styles["BodyText"]))
        story.append(Paragraph("<b>Oportunidades:</b> " + ("; ".join(opportunities) if opportunities else "Nenhuma oportunidade estruturada."), styles["BodyText"]))
        story.append(Spacer(1, 12))

        story.append(Paragraph("4. Recomendações de Decisão", styles["Heading2"]))
        actions = llm.get("recommended_actions") or []
        decision = llm.get("decision_guidance") or "Sem recomendação disponível."
        story.append(Paragraph(decision, styles["BodyText"]))
        if actions:
            for action in actions[:8]:
                story.append(Paragraph(f"• {action}", styles["BodyText"]))
        story.append(Spacer(1, 12))

        story.append(Paragraph("5. Amostra de Menções", styles["Heading2"]))
        sample = [["Fonte", "Autor", "Sentimento", "Texto"]]
        for m in mentions[:12]:
            sample.append([
                m.get("source", ""),
                (m.get("author", "") or "")[:20],
                m.get("sentiment", ""),
                (m.get("text", "") or "")[:120],
            ])
        sample_table = Table(sample, colWidths=[60, 90, 70, 260])
        sample_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(sample_table)

        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="relatorio-executivo-{search_id}.pdf"'},
        )
