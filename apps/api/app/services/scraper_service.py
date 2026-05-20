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

        errors = list(source_errors) + list(orchestrator.last_errors)

        return {
            "query": term,
            "sources": normalized_sources,
            "limit_per_source": limit,
            "total": total,
            "results": results,
            "errors": errors,
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
