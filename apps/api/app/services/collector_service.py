import logging
from typing import Any

from app.services.normalization_service import normalize_mention
from app.services.scraper_service import ScraperService

logger = logging.getLogger(__name__)


class CollectorService:
    """Converte itens do ScraperService em menções normalizadas para o pipeline."""

    @staticmethod
    def _public_source_error(raw_error: Any) -> str:
        text = str(raw_error or "").strip().lower()
        if not text:
            return "Falha temporaria na coleta da fonte"
        if "timeout" in text or "timed out" in text:
            return "Tempo limite excedido na coleta desta fonte"
        if "limit" in text or "429" in text:
            return "Limite temporario da fonte atingido"
        if "nao suportada" in text or "indisponivel" in text:
            return str(raw_error)
        return "Falha temporaria na coleta da fonte"

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
            return [], [{"source": "system", "error": "Falha temporaria no processamento de coleta"}]

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
            if isinstance(source_error, dict):
                errors.append(
                    {
                        "source": str(source_error.get("source") or "unknown"),
                        "error": CollectorService._public_source_error(source_error.get("error") or ""),
                    }
                )
            else:
                errors.append({"source": "unknown", "error": CollectorService._public_source_error(source_error)})

        return mentions, errors
