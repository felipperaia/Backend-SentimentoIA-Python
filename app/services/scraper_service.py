from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.services.collector_orchestrator import CollectorOrchestrator
from app.services.source_registry_service import SourceRegistryService


class ScraperService:
    """Adaptador de compatibilidade para o contrato atual do endpoint /api/scrape."""

    @staticmethod
    def _resolve_limit(limit_per_source: int | None) -> int:
        configured_default = int(settings.SCRAPER_DEFAULT_LIMIT)
        configured_max = int(settings.SCRAPER_MAX_ITEMS_PER_SOURCE)
        raw_limit = int(limit_per_source or configured_default)
        return max(1, min(configured_max, raw_limit))

    @staticmethod
    def _truncate_total_results(results: dict[str, list[dict[str, Any]]], max_total: int) -> dict[str, list[dict[str, Any]]]:
        if max_total <= 0:
            return {source: [] for source in results}

        trimmed: dict[str, list[dict[str, Any]]] = {source: [] for source in results}
        ordered_sources = sorted(results.keys(), key=SourceRegistryService.source_priority, reverse=True)
        remaining = max_total

        for source in ordered_sources:
            if remaining <= 0:
                break
            source_items = results.get(source) or []
            take = min(len(source_items), remaining)
            trimmed[source] = source_items[:take]
            remaining -= take

        return trimmed

    @staticmethod
    def _normalize_error_entry(entry: Any) -> dict[str, Any]:
        if isinstance(entry, dict):
            source = str(entry.get("source") or "unknown").strip().lower() or "unknown"
            reason = str(entry.get("reason") or "").strip().lower()
            timeout = bool(entry.get("timeout", False)) or reason == "timeout"

            raw_error = str(entry.get("error") or "").strip()
            if timeout:
                error = "Tempo limite excedido na coleta desta fonte"
                reason = "timeout"
            elif reason == "rate_limited":
                error = "Limite temporario da fonte atingido"
            elif reason == "unsupported_source":
                error = "Fonte nao suportada no backend atual"
            elif reason == "source_unavailable":
                error = "Fonte indisponivel no momento"
            else:
                error = raw_error or "Falha temporaria na coleta da fonte"
                if not reason:
                    lowered = error.lower()
                    if "timeout" in lowered or "timed out" in lowered:
                        reason = "timeout"
                        timeout = True
                        error = "Tempo limite excedido na coleta desta fonte"
                    elif "429" in lowered or "limite" in lowered or "rate limit" in lowered:
                        reason = "rate_limited"
                        error = "Limite temporario da fonte atingido"
                    else:
                        reason = "temporary_failure"

            return {
                "source": source,
                "error": error,
                "reason": reason,
                "timeout": timeout,
            }

        return {
            "source": "unknown",
            "error": "Falha temporaria na coleta da fonte",
            "reason": "temporary_failure",
            "timeout": False,
        }

    @staticmethod
    def _build_status_summary(
        *,
        requested_sources: list[str],
        results: dict[str, list[dict[str, Any]]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_sources: list[str] = []
        for source in requested_sources:
            normalized = str(source or "").strip().lower()
            if normalized and normalized not in normalized_sources:
                normalized_sources.append(normalized)

        if not normalized_sources:
            normalized_sources = sorted(set(results.keys()))

        errors_by_source: dict[str, list[dict[str, Any]]] = {}
        timeout_sources: set[str] = set()
        unmapped_error_count = 0

        for entry in errors:
            source = str(entry.get("source") or "unknown").strip().lower() or "unknown"
            if source in {"unknown", "system"}:
                unmapped_error_count += 1
                continue
            errors_by_source.setdefault(source, []).append(entry)
            if bool(entry.get("timeout", False)) or str(entry.get("reason") or "") == "timeout":
                timeout_sources.add(source)

        source_status: list[dict[str, Any]] = []
        for source in normalized_sources:
            source_items = results.get(source) or []
            source_errors = errors_by_source.get(source, [])
            first_error = source_errors[0] if source_errors else {}
            source_status.append(
                {
                    "source": source,
                    "ok": len(source_items) > 0,
                    "count": len(source_items),
                    "error": first_error.get("error") if first_error else None,
                    "reason": first_error.get("reason") if first_error else None,
                    "timeout": any(bool(item.get("timeout", False)) for item in source_errors),
                }
            )

        sources_with_data = sum(1 for item in source_status if int(item.get("count", 0)) > 0)
        sources_failed = sum(1 for item in source_status if item.get("error")) + unmapped_error_count
        partial_success = sources_with_data > 0 and sources_failed > 0

        if partial_success:
            status = "partial_success"
            message = "Scraping concluido com falhas parciais em algumas fontes."
        elif sources_with_data == 0 and sources_failed > 0:
            status = "failed"
            message = "Scraping sem resultados devido a falhas nas fontes selecionadas."
        elif sources_with_data == 0:
            status = "empty"
            message = "Scraping concluido sem resultados novos."
        else:
            status = "success"
            message = "Scraping concluido com sucesso."

        if timeout_sources and status in {"partial_success", "failed"}:
            message = f"{message} Uma ou mais fontes atingiram tempo limite."

        return {
            "status": status,
            "partial_success": partial_success,
            "message": message,
            "sources_requested": len(normalized_sources),
            "sources_with_data": sources_with_data,
            "sources_failed": sources_failed,
            "timeout_sources": sorted(timeout_sources),
            "source_status": source_status,
            "unmapped_error_count": unmapped_error_count,
        }

    @staticmethod
    async def scrape_async(
        query: str,
        sources: list[str],
        limit_per_source: int | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        del user_id

        term = str(query or "").strip()
        if not term:
            raise ValueError("Termo de busca obrigatorio")

        normalized_sources, source_errors = SourceRegistryService.normalize_sources(sources)
        limit = ScraperService._resolve_limit(limit_per_source)
        max_total = max(limit, int(settings.SCRAPER_MAX_TOTAL_ITEMS))

        orchestrator = CollectorOrchestrator(active_sources=normalized_sources)
        grouped_results = await orchestrator.gather_all(
            query=term,
            limit=limit,
            sources=normalized_sources,
        )

        results: dict[str, list[dict[str, Any]]] = {source: [] for source in normalized_sources}
        for source, items in (grouped_results or {}).items():
            if source not in results:
                results[source] = []
            if isinstance(items, list):
                results[source] = [item for item in items if isinstance(item, dict)]

        total = sum(len(items) for items in results.values())
        if total > max_total:
            results = ScraperService._truncate_total_results(results, max_total=max_total)
            total = sum(len(items) for items in results.values())

        errors = [ScraperService._normalize_error_entry(item) for item in list(source_errors) + list(orchestrator.last_errors)]
        status_summary = ScraperService._build_status_summary(
            requested_sources=normalized_sources,
            results=results,
            errors=errors,
        )

        return {
            "query": term,
            "sources": normalized_sources,
            "limit_per_source": limit,
            "total": total,
            "results": results,
            "errors": errors,
            "status": status_summary.get("status", "success"),
            "partial_success": bool(status_summary.get("partial_success", False)),
            "status_summary": status_summary,
            "metadata": {
                "sources": SourceRegistryService.source_metadata(),
                "max_total_items": max_total,
                "incremental_mode": False,
            },
        }

    @staticmethod
    def scrape(
        query: str,
        sources: list[str],
        limit_per_source: int | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        coroutine = ScraperService.scrape_async(
            query=query,
            sources=sources,
            limit_per_source=limit_per_source,
            user_id=user_id,
        )

        try:
            return asyncio.run(coroutine)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coroutine)
            finally:
                loop.close()
