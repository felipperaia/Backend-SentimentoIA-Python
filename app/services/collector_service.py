import logging
from typing import Any

from app.services.normalization_service import normalize_mention
from app.services.scraper_service import ScraperService

logger = logging.getLogger(__name__)


class CollectorService:
    """Converte itens do ScraperService em menções normalizadas para o pipeline."""

    @staticmethod
    def _public_source_error(
        raw_error: Any,
        *,
        reason: str | None = None,
        timeout: bool | None = None,
    ) -> str:
        text = str(raw_error or "").strip().lower()
        normalized_reason = str(reason or "").strip().lower()

        if timeout is True or normalized_reason == "timeout":
            return "Tempo limite excedido na coleta desta fonte"
        if normalized_reason == "rate_limited":
            return "Limite temporario da fonte atingido"
        if normalized_reason == "unsupported_source":
            return "Fonte nao suportada no backend atual"
        if normalized_reason == "source_unavailable":
            return "Fonte indisponivel no momento"

        if not text:
            return "Falha temporaria na coleta da fonte"
        if "timeout" in text or "timed out" in text:
            return "Tempo limite excedido na coleta desta fonte"
        if "limit" in text or "429" in text:
            return "Limite temporario da fonte atingido"
        if "nao suportada" in text or "indisponivel" in text:
            if "nao suportada" in text:
                return "Fonte nao suportada no backend atual"
            return "Fonte indisponivel no momento"
        return "Falha temporaria na coleta da fonte"

    @staticmethod
    def _normalize_source_error_entry(source_error: Any) -> dict[str, Any]:
        if isinstance(source_error, dict):
            source_name = str(source_error.get("source") or "unknown")
            reason = str(source_error.get("reason") or "").strip().lower() or None
            timeout_flag = bool(source_error.get("timeout", False)) or reason == "timeout"
            public_error = CollectorService._public_source_error(
                source_error.get("error") or "",
                reason=reason,
                timeout=timeout_flag,
            )
            return {
                "source": source_name,
                "error": public_error,
                "reason": reason or "temporary_failure",
                "timeout": timeout_flag,
            }

        return {
            "source": "unknown",
            "error": CollectorService._public_source_error(source_error),
            "reason": "temporary_failure",
            "timeout": False,
        }

    @staticmethod
    async def collect(
        query: str,
        sources: list[str],
        period_days: int = 30,
        locality: str | None = None,
        user_id: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        del period_days
        term = f"{query} {locality}".strip() if locality else query
        mentions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        try:
            scraped = await ScraperService.scrape_async(
                query=term,
                sources=sources,
                user_id=user_id,
            )
        except Exception as exc:
            logger.exception("Falha geral na coleta por scraping")
            del exc
            return [], [
                {
                    "source": "system",
                    "error": "Falha temporaria no processamento de coleta",
                    "reason": "collector_unavailable",
                    "timeout": False,
                }
            ]

        for source, source_items in (scraped.get("results") or {}).items():
            for item in source_items:
                title = str(item.get("title") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                text_fallback = str(item.get("text") or "").strip()
                text = "\n".join(part for part in [title, snippet, text_fallback] if part).strip()

                source_name = str(item.get("source") or source)

                normalized = normalize_mention(
                    query=query,
                    source=source_name,
                    text=text,
                    author=item.get("author"),
                    published_at=item.get("published_at"),
                    url=item.get("canonical_url") or item.get("url"),
                    rating=item.get("rating"),
                    raw=item,
                )
                if normalized:
                    normalized["source_tier"] = str(item.get("source_tier") or "B")
                    mentions.append(normalized)

        for source_error in scraped.get("errors") or []:
            errors.append(CollectorService._normalize_source_error_entry(source_error))

        return mentions, errors
