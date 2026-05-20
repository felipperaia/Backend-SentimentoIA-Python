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
        self.last_errors: list[dict[str, Any]] = []

    @staticmethod
    def _classify_source_error(exc: Exception) -> dict[str, Any]:
        raw_message = str(exc or "").strip()
        lowered = raw_message.lower()
        exc_name = type(exc).__name__.lower()

        timeout_markers = (
            "timeout",
            "timed out",
            "deadline exceeded",
            "read timed out",
            "connect timeout",
            "cancelled",
            "cancellederror",
        )
        if isinstance(exc, asyncio.TimeoutError) or any(marker in lowered or marker in exc_name for marker in timeout_markers):
            return {
                "error": "Tempo limite excedido na coleta desta fonte",
                "reason": "timeout",
                "timeout": True,
            }

        rate_limit_markers = ("429", "rate limit", "too many requests", "limite")
        if any(marker in lowered for marker in rate_limit_markers):
            return {
                "error": "Limite temporario da fonte atingido",
                "reason": "rate_limited",
                "timeout": False,
            }

        unavailable_markers = ("403", "forbidden", "captcha", "blocked", "denied")
        if any(marker in lowered for marker in unavailable_markers):
            return {
                "error": "Fonte indisponivel no momento",
                "reason": "source_unavailable",
                "timeout": False,
            }

        return {
            "error": "Falha temporaria na coleta desta fonte",
            "reason": "temporary_failure",
            "timeout": False,
        }

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

        inter_source_delay = max(0.0, float(getattr(settings, "SCRAPER_DELAY_SECONDS", 5.0) or 0.0))

        for index, source_name in enumerate(selected_sources):
            if source_name not in collectors_map:
                self.last_errors.append(
                    {
                        "source": source_name,
                        "error": "Fonte indisponivel no coletor atual",
                        "reason": "unsupported_source",
                        "timeout": False,
                    }
                )
                results[source_name] = []
                continue

            # Delay entre dominios diferentes para reduzir bloqueios.
            if index > 0 and inter_source_delay > 0:
                jitter = min(1.5, inter_source_delay * 0.3)
                await asyncio.sleep(max(0.1, inter_source_delay + random.uniform(-jitter, jitter)))

            try:
                collector = collectors_map[source_name]()
                items = await collector.collect(term, max_items)
                if not isinstance(items, list):
                    raise TypeError("payload invalido")

                valid_items = [item for item in items if isinstance(item, dict)]
                results[source_name] = valid_items[:max_per_source]

                collector_failure = getattr(collector, "last_failure", None)
                if not valid_items and isinstance(collector_failure, dict):
                    normalized_reason = str(collector_failure.get("reason") or "temporary_failure").strip().lower()
                    timeout_flag = bool(collector_failure.get("timeout", False)) or normalized_reason == "timeout"
                    self.last_errors.append(
                        {
                            "source": source_name,
                            "error": str(
                                collector_failure.get("error")
                                or ("Tempo limite excedido na coleta desta fonte" if timeout_flag else "Falha temporaria na coleta desta fonte")
                            ),
                            "reason": normalized_reason or "temporary_failure",
                            "timeout": timeout_flag,
                        }
                    )
            except Exception as exc:
                logger.warning("Coletor %s falhou: %s", source_name, type(exc).__name__)
                classified_error = CollectorOrchestrator._classify_source_error(exc)
                self.last_errors.append(
                    {
                        "source": source_name,
                        "error": str(classified_error.get("error") or "Falha temporaria na coleta desta fonte"),
                        "reason": str(classified_error.get("reason") or "temporary_failure"),
                        "timeout": bool(classified_error.get("timeout", False)),
                    }
                )
                results[source_name] = []

        return results
