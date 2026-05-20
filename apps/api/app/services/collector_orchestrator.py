from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from app.config import settings
from app.services.scraper import (
    AppStoreCollector,
    BaseCollector,
    GlassdoorCollector,
    MastodonCollector,
    PlayStoreCollector,
    ReclameAquiCollector,
    RedditCollector,
    TrustpilotCollector,
    WebSearchCollector,
    YouTubeCollector,
)
from app.services.source_registry_service import SourceRegistryService

logger = logging.getLogger(__name__)


class CollectorOrchestrator:
    """Executa coletores de forma resiliente sem propagar falhas para o chamador."""

    def __init__(self, active_sources: list[str] | None = None):
        self._source_order = [
            str(source or "").strip().lower()
            for source in (active_sources or [])
            if str(source or "").strip()
        ]
        self.last_errors: list[dict[str, str]] = []

    @staticmethod
    def _public_source_error(source_name: str) -> str:
        del source_name
        return "Falha temporaria na coleta desta fonte"

    @staticmethod
    def _collectors_map() -> dict[str, type[BaseCollector]]:
        return {
            "reddit": RedditCollector,
            "youtube": YouTubeCollector,
            "appstore": AppStoreCollector,
            "playstore": PlayStoreCollector,
            "trustpilot": TrustpilotCollector,
            "glassdoor": GlassdoorCollector,
            "reclameaqui": ReclameAquiCollector,
            "web": WebSearchCollector,
            "mastodon": MastodonCollector,
        }

    def _resolve_sources(self, sources: list[str] | None) -> list[str]:
        if isinstance(sources, list) and sources:
            raw_sources = sources
        elif self._source_order:
            raw_sources = self._source_order
        else:
            raw_sources = SourceRegistryService.default_sources()

        normalized: list[str] = []
        for source in raw_sources:
            value = SourceRegistryService.normalize_source_name(str(source or "").strip().lower())
            if value and value not in normalized:
                normalized.append(value)

        return normalized

    async def gather_all(self, query: str, limit: int, sources: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        self.last_errors = []

        term = str(query or "").strip()
        if not term:
            return {}

        max_items = max(1, int(limit))
        max_per_source = max(1, int(getattr(settings, "SCRAPER_MAX_ITEMS_PER_SOURCE", 10) or 10))

        collectors_map = self._collectors_map()
        selected_sources = self._resolve_sources(sources)

        results: dict[str, list[dict[str, Any]]] = {}

        for source_name in selected_sources:
            if source_name not in collectors_map:
                self.last_errors.append(
                    {
                        "source": source_name,
                        "error": "Fonte indisponivel no coletor atual",
                    }
                )
                results[source_name] = []
                continue

            # Delay entre dominios diferentes para reduzir bloqueios.
            await asyncio.sleep(random.uniform(3.0, 7.0))

            try:
                collector = collectors_map[source_name]()
                items = await collector.collect(term, max_items)
                if not isinstance(items, list):
                    raise TypeError("payload invalido")

                valid_items = [item for item in items if isinstance(item, dict)]
                results[source_name] = valid_items[:max_per_source]
            except Exception as exc:
                logger.warning("Coletor %s falhou: %s", source_name, type(exc).__name__)
                self.last_errors.append(
                    {
                        "source": source_name,
                        "error": CollectorOrchestrator._public_source_error(source_name),
                    }
                )
                results[source_name] = []

        return results
