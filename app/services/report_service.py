import csv
import io
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import Response, StreamingResponse

from app.database import get_db


class ReportService:
    """Exportação real CSV/PDF baseada no MongoDB, compatível com search_id e batch_id."""

    @staticmethod
    def _load_reportlab_components() -> dict[str, Any] | None:
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        except ModuleNotFoundError:
            return None

        return {
            "colors": colors,
            "A4": A4,
            "getSampleStyleSheet": getSampleStyleSheet,
            "Paragraph": Paragraph,
            "SimpleDocTemplate": SimpleDocTemplate,
            "Spacer": Spacer,
            "Table": Table,
            "TableStyle": TableStyle,
        }

    @staticmethod
    def _load_mentions(db, user_id: str, search_id: str) -> list[dict[str, Any]]:
        """Carrega mencoes por search_id OU batch_id."""
        mentions = list(
            db.mentions.find(
                {"user_id": user_id, "$or": [{"search_id": search_id}, {"batch_id": search_id}]},
                {"raw": 0},
            ).sort("published_at", -1)
        )
        return mentions

    @staticmethod
    def _load_analysis(db, user_id: str, search_id: str) -> dict[str, Any]:
        """Carrega llm_analysis do search_job ou insight mais recente."""
        # Tenta search_job primeiro
        job = db.search_jobs.find_one(
            {"user_id": user_id, "search_id": search_id, "status": "completed"},
            {"llm_analysis": 1, "metrics": 1, "query": 1},
        )
        if job and job.get("llm_analysis"):
            return {
                "query": job.get("query", ""),
                "metrics": job.get("metrics", {}),
                "llm_analysis": job.get("llm_analysis", {}),
            }

        # Tenta insight mais recente
        insight = db.insights.find_one(
            {
                "user_id": user_id,
                "$or": [{"batch_id": search_id}, {"search_id": search_id}, {"context_id": search_id}],
                "archived": False,
            },
            sort=[("created_at", -1)],
        )
        if insight:
            return {
                "query": (insight.get("snapshot") or {}).get("brand", ""),
                "metrics": {},
                "llm_analysis": {
                    "executive_summary": insight.get("executive_summary", ""),
                    "risks": insight.get("risks", []),
                    "opportunities": insight.get("opportunities", []),
                    "recommended_actions": insight.get("recommended_actions", []),
                    "decision_guidance": insight.get("decision_guidance", ""),
                    "trend": insight.get("trend", "indefinido"),
                },
            }

        return {"query": "", "metrics": {}, "llm_analysis": {}}

    @staticmethod
    def export_csv(user_id: str, search_id: str) -> StreamingResponse:
        db = get_db()
        mentions = ReportService._load_mentions(db, user_id, search_id)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "search_id", "query", "source", "author", "published_at", "rating",
            "sentiment", "confidence", "criticality", "urgency_score",
            "reputation_score", "aspects", "url", "text"
        ])

        for m in mentions:
            writer.writerow([
                m.get("search_id") or m.get("batch_id") or search_id,
                m.get("query") or m.get("entity") or "",
                m.get("source") or "",
                m.get("author") or "",
                m.get("published_at") or "",
                m.get("rating") or "",
                m.get("sentiment") or "",
                m.get("confidence") or "",
                m.get("criticality") or "",
                m.get("urgency_score") or "",
                m.get("reputation_score") or "",
                ";".join(m.get("aspects") or []),
                m.get("url") or "",
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
        reportlab = ReportService._load_reportlab_components()
        if reportlab is None:
            return Response(
                content="Exportacao PDF indisponivel neste ambiente (dependencia reportlab ausente).",
                media_type="text/plain",
                status_code=503,
            )

        colors = reportlab["colors"]
        A4 = reportlab["A4"]
        getSampleStyleSheet = reportlab["getSampleStyleSheet"]
        Paragraph = reportlab["Paragraph"]
        SimpleDocTemplate = reportlab["SimpleDocTemplate"]
        Spacer = reportlab["Spacer"]
        Table = reportlab["Table"]
        TableStyle = reportlab["TableStyle"]

        db = get_db()
        mentions = ReportService._load_mentions(db, user_id, search_id)
        analysis_data = ReportService._load_analysis(db, user_id, search_id)
        llm = analysis_data.get("llm_analysis", {})
        query_name = analysis_data.get("query", "")

        # Calcula metricas a partir das mencoes reais
        from app.services.enrichment_service import EnrichmentService
        metrics = EnrichmentService.aggregate(mentions) if mentions else {}

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story: list[Any] = []

        story.append(Paragraph("Relatório Executivo de Reputação Digital", styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Busca: <b>{query_name}</b>", styles["Normal"]))
        story.append(Paragraph(f"ID: {search_id}", styles["Normal"]))
        story.append(Paragraph(f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", styles["Normal"]))
        story.append(Spacer(1, 16))

        story.append(Paragraph("1. Resumo Executivo", styles["Heading2"]))
        story.append(Paragraph(llm.get("executive_summary") or "Resumo indisponível.", styles["BodyText"]))
        story.append(Spacer(1, 12))

        story.append(Paragraph("2. Indicadores", styles["Heading2"]))
        table_data = [
            ["Métrica", "Valor"],
            ["Total de menções", str(metrics.get("total_mentions", len(mentions)))],
            ["Score de reputação", str(metrics.get("reputation_score", 0))],
            ["Tendência", str(metrics.get("trend") or llm.get("trend") or "indefinido")],
            ["Menções críticas", str(metrics.get("critical_mentions", 0))],
        ]

        # Inclui distribuicao por fonte
        source_dist = metrics.get("source_distribution", {})
        for src, count in source_dist.items():
            table_data.append([f"  Fonte: {src}", str(count)])

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
        story.append(Paragraph("<b>Riscos:</b> " + ("; ".join(str(r) for r in risks) if risks else "Nenhum risco estruturado."), styles["BodyText"]))
        story.append(Paragraph("<b>Oportunidades:</b> " + ("; ".join(str(o) for o in opportunities) if opportunities else "Nenhuma oportunidade estruturada."), styles["BodyText"]))
        story.append(Spacer(1, 12))

        story.append(Paragraph("4. Recomendações de Decisão", styles["Heading2"]))
        actions = llm.get("recommended_actions") or []
        decision = llm.get("decision_guidance") or "Sem recomendação disponível."
        story.append(Paragraph(str(decision), styles["BodyText"]))
        if actions:
            for action in actions[:8]:
                story.append(Paragraph(f"• {action}", styles["BodyText"]))
        story.append(Spacer(1, 12))

        story.append(Paragraph("5. Amostra de Menções", styles["Heading2"]))
        sample = [["Fonte", "Autor", "Sentimento", "Texto"]]
        for m in mentions[:12]:
            sample.append([
                str(m.get("source", ""))[:20],
                (str(m.get("author", "") or ""))[:20],
                str(m.get("sentiment", "")),
                (str(m.get("text", "") or ""))[:120],
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
            headers={"Content-Disposition": f'attachment; filename="relatorio-{search_id}.pdf"'},
        )

    @staticmethod
    def _normalize_priority_filter(priority: str | None) -> str | None:
        candidate = str(priority or "").strip().lower().replace(" ", "_")
        if candidate in {"alta", "high", "critica", "critical"}:
            return "high"
        if candidate in {"media", "medium", "moderada", "moderate"}:
            return "medium"
        if candidate in {"baixa", "low", "ok"}:
            return "low"
        return None

    @staticmethod
    def _normalize_resolution_filter(resolution: str | None) -> str | None:
        candidate = str(resolution or "").strip().lower().replace(" ", "_")
        if candidate in {"resolved", "resolvido", "done", "concluido", "concluído"}:
            return "resolved"
        if candidate in {"in_progress", "em_andamento", "processing", "working"}:
            return "in_progress"
        if candidate in {"pending", "pendente", "open", "novo", "new"}:
            return "pending"
        return None

    @staticmethod
    def _load_insights(
        db,
        user_id: str,
        priority: str | None,
        resolution: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"user_id": user_id, "archived": {"$ne": True}}

        normalized_priority = ReportService._normalize_priority_filter(priority)
        normalized_resolution = ReportService._normalize_resolution_filter(resolution)

        if normalized_priority:
            query["priority"] = normalized_priority
        if normalized_resolution:
            query["resolution"] = normalized_resolution

        return list(
            db.insights.find(query).sort("created_at", -1).limit(max(1, min(limit, 500)))
        )

    @staticmethod
    def export_insights_markdown(
        user_id: str,
        priority: str | None = None,
        resolution: str | None = None,
        limit: int = 100,
    ) -> StreamingResponse:
        db = get_db()
        insights = ReportService._load_insights(
            db=db,
            user_id=user_id,
            priority=priority,
            resolution=resolution,
            limit=limit,
        )

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines: list[str] = [
            "# Exportacao de Insights - SentimentoIA",
            "",
            f"Gerado em: {generated_at}",
            f"Total de insights: {len(insights)}",
            "",
        ]

        if not insights:
            lines.append("Nenhum insight encontrado para os filtros informados.")
        else:
            for idx, insight in enumerate(insights, start=1):
                timestamp = insight.get("timestamp") or insight.get("created_at") or "-"
                lines.extend(
                    [
                        f"## {idx}. {insight.get('company') or (insight.get('snapshot') or {}).get('brand') or 'Empresa nao informada'}",
                        "",
                        f"- Priority: {insight.get('priority') or 'medium'}",
                        f"- Urgency: {insight.get('urgency') or 'medium'}",
                        f"- Status: {insight.get('status') or 'open'}",
                        f"- Resolution: {insight.get('resolution') or 'pending'}",
                        f"- Timestamp: {timestamp}",
                        "",
                        f"**Root Cause**: {insight.get('root_cause') or 'Nao informado'}",
                        "",
                        f"**Recommended Action**: {insight.get('recommended_action') or 'Nao informado'}",
                        "",
                        f"**Executive Summary**: {insight.get('executive_summary') or 'Nao informado'}",
                        "",
                        f"**Decision Guidance**: {insight.get('decision_guidance') or 'Nao informado'}",
                        "",
                        "---",
                        "",
                    ]
                )

        payload = "\n".join(lines).encode("utf-8")
        return StreamingResponse(
            io.BytesIO(payload),
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="insights.md"'},
        )

    @staticmethod
    def export_insights_pdf(
        user_id: str,
        priority: str | None = None,
        resolution: str | None = None,
        limit: int = 100,
    ) -> Response:
        reportlab = ReportService._load_reportlab_components()
        if reportlab is None:
            return Response(
                content="Exportacao PDF indisponivel neste ambiente (dependencia reportlab ausente).",
                media_type="text/plain",
                status_code=503,
            )

        colors = reportlab["colors"]
        A4 = reportlab["A4"]
        getSampleStyleSheet = reportlab["getSampleStyleSheet"]
        Paragraph = reportlab["Paragraph"]
        SimpleDocTemplate = reportlab["SimpleDocTemplate"]
        Spacer = reportlab["Spacer"]
        Table = reportlab["Table"]
        TableStyle = reportlab["TableStyle"]

        db = get_db()
        insights = ReportService._load_insights(
            db=db,
            user_id=user_id,
            priority=priority,
            resolution=resolution,
            limit=limit,
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story: list[Any] = []

        story.append(Paragraph("Relatorio de Insights", styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", styles["Normal"]))
        story.append(Paragraph(f"Total de insights: {len(insights)}", styles["Normal"]))
        story.append(Spacer(1, 12))

        if not insights:
            story.append(Paragraph("Nenhum insight encontrado para os filtros informados.", styles["BodyText"]))
        else:
            table_data = [["Empresa", "Prioridade", "Urgencia", "Status", "Resolucao", "Resumo"]]
            for insight in insights:
                table_data.append(
                    [
                        str(insight.get("company") or (insight.get("snapshot") or {}).get("brand") or "-")[:32],
                        str(insight.get("priority") or "medium"),
                        str(insight.get("urgency") or "medium"),
                        str(insight.get("status") or "open"),
                        str(insight.get("resolution") or "pending"),
                        str(insight.get("executive_summary") or "-")[:120],
                    ]
                )

            table = Table(table_data, colWidths=[90, 60, 60, 60, 70, 180])
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(table)

        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="insights.pdf"'},
        )
