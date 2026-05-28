import csv
import io
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from fastapi.responses import Response, StreamingResponse

from app.database import get_db
from app.services.company_utils import normalize_company_filter, slugify_company
from app.services.dashboard_service import DashboardService
from app.services.search_service import SearchService


class ReportService:
    """Exportação real CSV/PDF baseada no MongoDB, compatível com search_id e batch_id."""

    @staticmethod
    def _effective_period_range(
        *,
        period_from: datetime | None,
        period_to: datetime | None,
        period_days: int | None,
    ) -> tuple[datetime | None, datetime | None]:
        if isinstance(period_from, datetime) or isinstance(period_to, datetime):
            return period_from, period_to

        if period_days and int(period_days) > 0:
            now = datetime.now(timezone.utc)
            return now - timedelta(days=int(period_days)), now

        return None, None

    @staticmethod
    def _sort_timestamp(value: Any) -> float:
        if isinstance(value, datetime):
            try:
                if value.tzinfo is None:
                    return value.replace(tzinfo=timezone.utc).timestamp()
                return value.timestamp()
            except Exception:
                return 0.0
        return 0.0

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
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = [{"user_id": user_id}, {"archived": {"$ne": True}}]

        normalized_priority = ReportService._normalize_priority_filter(priority)
        normalized_resolution = ReportService._normalize_resolution_filter(resolution)
        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_slug)

        if not normalized_company_slug:
            return []

        if normalized_priority:
            conditions.append({"priority": normalized_priority})
        if normalized_resolution:
            conditions.append({"resolution": normalized_resolution})

        conditions.append({"company_slug": normalized_company_slug})

        if period_from:
            conditions.append({"period_to": {"$gte": period_from}})
        if period_to:
            conditions.append({"period_from": {"$lte": period_to}})

        query: dict[str, Any] = {"$and": conditions}

        return list(
            db.insights.find(query).sort("created_at", -1).limit(max(1, min(limit, 500)))
        )

    @staticmethod
    def export_insights_markdown(
        user_id: str,
        priority: str | None = None,
        resolution: str | None = None,
        limit: int = 100,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> StreamingResponse:
        db = get_db()
        insights = ReportService._load_insights(
            db=db,
            user_id=user_id,
            priority=priority,
            resolution=resolution,
            limit=limit,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
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
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
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
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
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

    @staticmethod
    def export_insights_csv(
        user_id: str,
        priority: str | None = None,
        resolution: str | None = None,
        limit: int = 100,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> StreamingResponse:
        db = get_db()
        insights = ReportService._load_insights(
            db=db,
            user_id=user_id,
            priority=priority,
            resolution=resolution,
            limit=limit,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
        )

        def _serialize_dt(value: Any) -> str:
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value or "")

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "insight_id",
                "company_name",
                "company_slug",
                "priority",
                "urgency",
                "status",
                "resolution",
                "period_from",
                "period_to",
                "created_at",
                "root_cause",
                "recommended_action",
                "executive_summary",
                "decision_guidance",
            ]
        )

        for item in insights:
            snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
            company_name = str(
                item.get("company_name")
                or item.get("company")
                or snapshot.get("company_name")
                or snapshot.get("brand")
                or ""
            )
            company_slug_value = str(item.get("company_slug") or snapshot.get("company_slug") or "")

            writer.writerow(
                [
                    item.get("insight_id") or item.get("id") or "",
                    company_name,
                    company_slug_value,
                    item.get("priority") or "",
                    item.get("urgency") or "",
                    item.get("status") or "",
                    item.get("resolution") or "",
                    _serialize_dt(item.get("period_from")),
                    _serialize_dt(item.get("period_to")),
                    _serialize_dt(item.get("created_at")),
                    (item.get("root_cause") or ""),
                    (item.get("recommended_action") or ""),
                    (item.get("executive_summary") or ""),
                    (item.get("decision_guidance") or ""),
                ]
            )

        content = buffer.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="insights.csv"'},
        )

    @staticmethod
    def _company_regex_from_slug(company_slug: str) -> str:
        escaped = re.escape(company_slug).replace("\\-", "[-_\\s]*")
        return f"^{escaped}$"

    @staticmethod
    def _resolve_search_jobs(
        db,
        user_id: str,
        *,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        resolved_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_id)

        query: dict[str, Any] = {
            "user_id": user_id,
            "status": "completed",
        }

        if period_from or period_to:
            date_query: dict[str, Any] = {}
            if period_from:
                date_query["$gte"] = period_from
            if period_to:
                date_query["$lte"] = period_to
            query["created_at"] = date_query

        if resolved_company_slug:
            company_regex = ReportService._company_regex_from_slug(resolved_company_slug)
            query["$or"] = [
                {"company_slug": resolved_company_slug},
                {"query": {"$regex": company_regex, "$options": "i"}},
                {"company_name": {"$regex": company_regex, "$options": "i"}},
                {"brand": {"$regex": company_regex, "$options": "i"}},
            ]

        jobs: list[dict[str, Any]] = []
        for collection_name in ["search_jobs", "searchjobs"]:
            if collection_name not in db.list_collection_names():
                continue
            jobs.extend(
                list(
                    db[collection_name].find(
                        query,
                        {
                            "search_id": 1,
                            "query": 1,
                            "company_name": 1,
                            "company_slug": 1,
                            "created_at": 1,
                            "updated_at": 1,
                        },
                    )
                )
            )

        unique_by_search: dict[str, dict[str, Any]] = {}
        for job in jobs:
            search_id = str(job.get("search_id") or "").strip()
            if not search_id:
                continue
            if search_id not in unique_by_search:
                unique_by_search[search_id] = job

        ordered = sorted(
            unique_by_search.values(),
            key=lambda item: ReportService._sort_timestamp(item.get("created_at")),
            reverse=True,
        )
        return ordered

    @staticmethod
    def _mentions_query_for_search_ids(
        *,
        user_id: str,
        search_ids: list[str],
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "user_id": user_id,
            "$or": [
                {"search_id": {"$in": search_ids}},
                {"batch_id": {"$in": search_ids}},
            ],
        }

        if period_from or period_to:
            date_filter: dict[str, Any] = {}
            if period_from:
                date_filter["$gte"] = period_from
            if period_to:
                date_filter["$lte"] = period_to
            query["created_at"] = date_filter

        return query

    @staticmethod
    def list_reports_filtered(
        user_id: str,
        *,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_id)
        if not normalized_company_slug:
            return {
                "total": 0,
                "limit": max(1, min(int(limit), 200)),
                "offset": max(0, int(offset)),
                "items": [],
            }

        effective_period_from, effective_period_to = ReportService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=None,
        )
        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            return {
                "total": 0,
                "limit": max(1, min(int(limit), 200)),
                "offset": max(0, int(offset)),
                "items": [],
            }

        query: dict[str, Any] = {
            "user_id": user_id,
            "company_slug": normalized_company_slug,
        }
        published_filter: dict[str, Any] = {}
        if isinstance(effective_period_from, datetime):
            published_filter["$gte"] = effective_period_from
        if isinstance(effective_period_to, datetime):
            published_filter["$lte"] = effective_period_to
        if published_filter:
            query["published_at"] = published_filter

        mentions = list(
            db.mentions.find(
                query,
                {"company_name": 1, "query": 1, "published_at": 1, "created_at": 1},
            )
            .sort("published_at", -1)
            .limit(1000)
        )

        if not mentions:
            return {
                "total": 0,
                "limit": max(1, min(int(limit), 200)),
                "offset": max(0, int(offset)),
                "items": [],
            }

        company_name = str(
            mentions[0].get("company_name")
            or mentions[0].get("query")
            or normalized_company_slug
        )

        values = [
            item.get("published_at") or item.get("created_at")
            for item in mentions
            if isinstance(item.get("published_at") or item.get("created_at"), datetime)
        ]
        period_start = min(values).isoformat() if values else None
        period_end = max(values).isoformat() if values else None

        all_items = [
            {
                "report_id": f"report_{normalized_company_slug}_mentions_csv",
                "company_name": company_name,
                "company_slug": normalized_company_slug,
                "period_from": period_start,
                "period_to": period_end,
                "report_type": "mentions_csv",
                "export_key": "mentions_csv",
            },
            {
                "report_id": f"report_{normalized_company_slug}_dashboard_pdf",
                "company_name": company_name,
                "company_slug": normalized_company_slug,
                "period_from": period_start,
                "period_to": period_end,
                "report_type": "dashboard_pdf",
                "export_key": "dashboard_pdf",
            },
            {
                "report_id": f"report_{normalized_company_slug}_insights_pdf",
                "company_name": company_name,
                "company_slug": normalized_company_slug,
                "period_from": period_start,
                "period_to": period_end,
                "report_type": "insights_pdf",
                "export_key": "insights_pdf",
            },
            {
                "report_id": f"report_{normalized_company_slug}_metrics_pdf",
                "company_name": company_name,
                "company_slug": normalized_company_slug,
                "period_from": period_start,
                "period_to": period_end,
                "report_type": "metrics_pdf",
                "export_key": "metrics_pdf",
            },
        ]

        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        paged = all_items[safe_offset : safe_offset + safe_limit]
        return {
            "total": len(all_items),
            "limit": safe_limit,
            "offset": safe_offset,
            "items": SearchService.serialize_many(paged),
        }

    @staticmethod
    def _export_csv_mentions(mentions: list[dict[str, Any]], file_label: str) -> StreamingResponse:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "search_id",
                "query",
                "source",
                "author",
                "published_at",
                "rating",
                "sentiment",
                "confidence",
                "criticality",
                "urgency_score",
                "reputation_score",
                "aspects",
                "url",
                "text",
            ]
        )

        for mention in mentions:
            writer.writerow(
                [
                    mention.get("search_id") or mention.get("batch_id") or "",
                    mention.get("query") or mention.get("company_name") or mention.get("entity") or "",
                    mention.get("source") or "",
                    mention.get("author") or "",
                    mention.get("published_at") or mention.get("created_at") or "",
                    mention.get("rating") or "",
                    mention.get("sentiment") or "",
                    mention.get("confidence") or mention.get("confidence_score") or "",
                    mention.get("criticality") or "",
                    mention.get("urgency_score") or "",
                    mention.get("reputation_score") or "",
                    ";".join(mention.get("aspects") or []),
                    mention.get("url") or "",
                    (mention.get("text") or "").replace("\n", " "),
                ]
            )

        content = buffer.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{file_label}.csv"'},
        )

    @staticmethod
    def _export_pdf_mentions(mentions: list[dict[str, Any]], file_label: str) -> Response:
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

        from app.services.enrichment_service import EnrichmentService

        metrics = EnrichmentService.aggregate(mentions) if mentions else {}

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story: list[Any] = []

        story.append(Paragraph("Relatorio Consolidado", styles["Title"]))
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", styles["Normal"]))
        story.append(Spacer(1, 8))

        table_data = [
            ["Metrica", "Valor"],
            ["Total de mencoes", str(metrics.get("total_mentions", len(mentions)))],
            ["Score de reputacao", str(metrics.get("reputation_score", 0))],
            ["Tendencia", str(metrics.get("trend") or "indefinido")],
            ["Mencoes criticas", str(metrics.get("critical_mentions", 0))],
        ]
        table = Table(table_data, colWidths=[220, 220])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 12))

        sample = [["Fonte", "Autor", "Sentimento", "Texto"]]
        for mention in mentions[:20]:
            sample.append(
                [
                    str(mention.get("source", ""))[:20],
                    str(mention.get("author", ""))[:20],
                    str(mention.get("sentiment", ""))[:16],
                    str(mention.get("text", ""))[:120],
                ]
            )
        sample_table = Table(sample, colWidths=[60, 90, 70, 260])
        sample_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(sample_table)

        doc.build(story)
        payload = buffer.getvalue()
        buffer.close()

        return Response(
            content=payload,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{file_label}.pdf"'},
        )

    @staticmethod
    def export_filtered(
        user_id: str,
        *,
        report_format: str,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
    ) -> StreamingResponse | Response:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        jobs = ReportService._resolve_search_jobs(
            db=db,
            user_id=user_id,
            company_id=company_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
        )
        if not jobs:
            raise ValueError("Nenhum relatório disponível para o filtro informado")

        search_ids = [str(job.get("search_id") or "").strip() for job in jobs if job.get("search_id")]
        if not search_ids:
            raise ValueError("Nenhuma busca correspondente foi encontrada")

        mentions_query = ReportService._mentions_query_for_search_ids(
            user_id=user_id,
            search_ids=search_ids,
            period_from=period_from,
            period_to=period_to,
        )
        mentions = list(db.mentions.find(mentions_query, {"raw": 0}).sort("created_at", -1))

        normalized_format = str(report_format or "").strip().lower()
        file_label = "relatorio-filtrado"
        if normalized_format == "csv":
            return ReportService._export_csv_mentions(mentions=mentions, file_label=file_label)
        if normalized_format == "pdf":
            return ReportService._export_pdf_mentions(mentions=mentions, file_label=file_label)

        raise ValueError("Formato invalido. Use csv ou pdf")

    @staticmethod
    def _build_mentions_scope_query(
        *,
        user_id: str,
        company_id: str | None,
        company_slug: str | None,
        period_from: datetime | None,
        period_to: datetime | None,
        period_days: int | None,
    ) -> tuple[str, datetime | None, datetime | None, dict[str, Any]]:
        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_id)
        if not normalized_company_slug:
            raise ValueError("companySlug e obrigatorio para exportacao de relatorios")

        effective_period_from, effective_period_to = ReportService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )

        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            raise ValueError("Faixa de datas invalida: from deve ser menor ou igual a to")

        query: dict[str, Any] = {
            "user_id": user_id,
            "company_slug": normalized_company_slug,
        }
        published_filter: dict[str, Any] = {}
        if isinstance(effective_period_from, datetime):
            published_filter["$gte"] = effective_period_from
        if isinstance(effective_period_to, datetime):
            published_filter["$lte"] = effective_period_to
        if published_filter:
            query["published_at"] = published_filter

        return normalized_company_slug, effective_period_from, effective_period_to, query

    @staticmethod
    def _load_mentions_by_scope(
        *,
        db: Any,
        query: dict[str, Any],
        limit: int = 20000,
    ) -> list[dict[str, Any]]:
        return list(
            db.mentions.find(query, {"raw": 0})
            .sort("published_at", -1)
            .limit(max(1, min(int(limit), 20000)))
        )

    @staticmethod
    def _report_file_label(
        *,
        prefix: str,
        company_slug: str,
        period_from: datetime | None,
        period_to: datetime | None,
    ) -> str:
        safe_slug = slugify_company(company_slug) or "empresa"
        from_label = period_from.strftime("%Y%m%d") if isinstance(period_from, datetime) else "inicio"
        to_label = period_to.strftime("%Y%m%d") if isinstance(period_to, datetime) else "fim"
        return f"{prefix}-{safe_slug}-{from_label}-{to_label}"

    @staticmethod
    def export_mentions_csv_canonical(
        user_id: str,
        *,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        period_days: int | None = None,
    ) -> StreamingResponse:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        normalized_company_slug, effective_period_from, effective_period_to, query = ReportService._build_mentions_scope_query(
            user_id=user_id,
            company_id=company_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )
        mentions = ReportService._load_mentions_by_scope(db=db, query=query)
        if not mentions:
            raise ValueError("Nenhuma mencao encontrada para o filtro informado")

        file_label = ReportService._report_file_label(
            prefix="mentions",
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
        )
        return ReportService._export_csv_mentions(mentions=mentions, file_label=file_label)

    @staticmethod
    def export_dashboard_pdf_canonical(
        user_id: str,
        *,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        period_days: int | None = None,
    ) -> Response:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        normalized_company_slug, effective_period_from, effective_period_to, query = ReportService._build_mentions_scope_query(
            user_id=user_id,
            company_id=company_id,
            company_slug=company_slug,
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )
        mentions = ReportService._load_mentions_by_scope(db=db, query=query)
        if not mentions:
            raise ValueError("Nenhuma mencao encontrada para o filtro informado")

        file_label = ReportService._report_file_label(
            prefix="dashboard",
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
        )
        return ReportService._export_pdf_mentions(mentions=mentions, file_label=file_label)

    @staticmethod
    def export_insights_pdf_canonical(
        user_id: str,
        *,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        period_days: int | None = None,
        limit: int = 300,
    ) -> Response:
        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_id)
        if not normalized_company_slug:
            raise ValueError("companySlug e obrigatorio para exportacao de relatorios")

        effective_period_from, effective_period_to = ReportService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )
        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            raise ValueError("Faixa de datas invalida: from deve ser menor ou igual a to")

        return ReportService.export_insights_pdf(
            user_id=user_id,
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
            limit=max(1, min(int(limit), 500)),
        )

    @staticmethod
    def export_metrics_pdf_canonical(
        user_id: str,
        *,
        company_id: str | None = None,
        company_slug: str | None = None,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        period_days: int | None = None,
    ) -> Response:
        normalized_company_slug = normalize_company_filter(company_slug=company_slug, company_id=company_id)
        if not normalized_company_slug:
            raise ValueError("companySlug e obrigatorio para exportacao de relatorios")

        effective_period_from, effective_period_to = ReportService._effective_period_range(
            period_from=period_from,
            period_to=period_to,
            period_days=period_days,
        )
        if (
            isinstance(effective_period_from, datetime)
            and isinstance(effective_period_to, datetime)
            and effective_period_from > effective_period_to
        ):
            raise ValueError("Faixa de datas invalida: from deve ser menor ou igual a to")

        metrics = DashboardService.aggregate_metrics(
            user_id=user_id,
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
            period_days=period_days,
            include_raw=False,
        )

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

        positive_ratio = float((metrics.get("sentiment_distribution") or {}).get("positive", 0.0) or 0.0)
        neutral_ratio = float((metrics.get("sentiment_distribution") or {}).get("neutral", 0.0) or 0.0)
        negative_ratio = float((metrics.get("sentiment_distribution") or {}).get("negative", 0.0) or 0.0)

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story: list[Any] = []

        story.append(Paragraph("Relatorio de Metricas", styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Empresa: {metrics.get('company_name') or normalized_company_slug}", styles["Normal"]))
        story.append(
            Paragraph(
                f"Periodo: {metrics.get('period_from') or '-'} ate {metrics.get('period_to') or '-'}",
                styles["Normal"],
            )
        )
        story.append(Paragraph(f"Gerado em: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", styles["Normal"]))
        story.append(Spacer(1, 12))

        summary_table = Table(
            [
                ["Metrica", "Valor"],
                ["Total de mencoes", str(int(metrics.get("total_mentions", 0) or 0))],
                ["Urgencia media", str(round(float(metrics.get("average_urgency", 0.0) or 0.0), 4))],
                ["Sentimento positivo", f"{positive_ratio * 100:.2f}%"],
                ["Sentimento neutro", f"{neutral_ratio * 100:.2f}%"],
                ["Sentimento negativo", f"{negative_ratio * 100:.2f}%"],
            ],
            colWidths=[220, 220],
        )
        summary_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ]
            )
        )
        story.append(summary_table)
        story.append(Spacer(1, 12))

        top_negative_aspects = metrics.get("top_negative_aspects") or []
        if top_negative_aspects:
            story.append(Paragraph("Top Aspectos Negativos", styles["Heading2"]))
            negative_table = Table(
                [["Aspecto", "Mencoes"]]
                + [
                    [
                        str(item.get("label") or ""),
                        str(int(item.get("mentions", 0) or 0)),
                    ]
                    for item in top_negative_aspects[:10]
                ],
                colWidths=[320, 120],
            )
            negative_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ]
                )
            )
            story.append(negative_table)
            story.append(Spacer(1, 12))

        most_cited_aspects = metrics.get("most_cited_aspects") or []
        if most_cited_aspects:
            story.append(Paragraph("Aspectos Mais Citados", styles["Heading2"]))
            cited_table = Table(
                [["Aspecto", "Mencoes"]]
                + [
                    [
                        str(item.get("label") or ""),
                        str(int(item.get("mentions", 0) or 0)),
                    ]
                    for item in most_cited_aspects[:10]
                ],
                colWidths=[320, 120],
            )
            cited_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ]
                )
            )
            story.append(cited_table)

        doc.build(story)
        payload = buffer.getvalue()
        buffer.close()

        file_label = ReportService._report_file_label(
            prefix="metrics",
            company_slug=normalized_company_slug,
            period_from=effective_period_from,
            period_to=effective_period_to,
        )
        return Response(
            content=payload,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{file_label}.pdf"'},
        )
