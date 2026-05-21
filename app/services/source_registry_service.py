from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.database import get_db
from app.services.normalization_service import utcnow


@dataclass(frozen=True)
class SourceConfig:
    name: str
    source_type: str
    base_url: str
    active: bool
    priority: int
    fetch_mode: str
    rate_limit_per_minute: int
    parser: str
    deprecated: bool = False


class SourceRegistryService:
    SOURCE_ALIASES = {
        "twitter": "x",
        "reclame_aqui": "reclameaqui",
        "reclame-aqui": "reclameaqui",
        "app-store": "appstore",
        "app_store": "appstore",
        "play-store": "playstore",
        "play_store": "playstore",
        "googleplay": "playstore",
        "google-play": "playstore",
        "yt": "youtube",
    }

    @staticmethod
    def _definitions() -> dict[str, SourceConfig]:
        return {
            "reclameaqui": SourceConfig(
                name="reclameaqui",
                source_type="reputation",
                base_url="https://www.reclameaqui.com.br",
                active=bool(settings.enable_reclame_aqui),
                priority=35,
                fetch_mode="public_api_or_html",
                rate_limit_per_minute=10,
                parser="reclameaqui_public_parser",
            ),
            "reddit": SourceConfig(
                name="reddit",
                source_type="community",
                base_url="https://www.reddit.com",
                active=True,
                priority=100,
                fetch_mode="public_json",
                rate_limit_per_minute=60,
                parser="reddit_public_json_parser",
            ),
            "youtube": SourceConfig(
                name="youtube",
                source_type="video_reviews",
                base_url="https://www.youtube.com",
                active=True,
                priority=95,
                fetch_mode="html_embedded_json",
                rate_limit_per_minute=30,
                parser="youtube_ytinitialdata_parser",
            ),
            "appstore": SourceConfig(
                name="appstore",
                source_type="mobile_reviews",
                base_url="https://itunes.apple.com",
                active=True,
                priority=92,
                fetch_mode="library_scraper",
                rate_limit_per_minute=20,
                parser="app_store_scraper_parser",
            ),
            "playstore": SourceConfig(
                name="playstore",
                source_type="mobile_reviews",
                base_url="https://play.google.com",
                active=True,
                priority=90,
                fetch_mode="library_scraper",
                rate_limit_per_minute=20,
                parser="play_store_scraper_parser",
            ),
            "trustpilot": SourceConfig(
                name="trustpilot",
                source_type="review_portal",
                base_url="https://br.trustpilot.com",
                active=True,
                priority=60,
                fetch_mode="html_or_playwright",
                rate_limit_per_minute=10,
                parser="trustpilot_html_parser",
            ),
            "glassdoor": SourceConfig(
                name="glassdoor",
                source_type="review_portal",
                base_url="https://www.glassdoor.com.br",
                active=True,
                priority=55,
                fetch_mode="html_plus_fallback",
                rate_limit_per_minute=8,
                parser="glassdoor_html_parser",
            ),
            "mastodon": SourceConfig(
                name="mastodon",
                source_type="federated_social",
                base_url="https://mastodon.social",
                active=True,
                priority=80,
                fetch_mode="public_json",
                rate_limit_per_minute=30,
                parser="mastodon_status_parser",
                deprecated=False,
            ),
            "web": SourceConfig(
                name="web",
                source_type="open_web",
                base_url="https://html.duckduckgo.com/html/",
                active=True,
                priority=50,
                fetch_mode="ddg_then_bing",
                rate_limit_per_minute=30,
                parser="web_search_snippet_parser",
                deprecated=False,
            ),
            "google": SourceConfig(
                name="google",
                source_type="legacy",
                base_url="https://news.google.com",
                active=False,
                priority=5,
                fetch_mode="deprecated",
                rate_limit_per_minute=0,
                parser="disabled",
                deprecated=True,
            ),
            "x": SourceConfig(
                name="x",
                source_type="legacy",
                base_url="https://x.com",
                active=False,
                priority=5,
                fetch_mode="deprecated",
                rate_limit_per_minute=0,
                parser="disabled",
                deprecated=True,
            ),
        }

    @staticmethod
    def normalize_source_name(source: str) -> str:
        value = str(source or "").strip().lower()
        return SourceRegistryService.SOURCE_ALIASES.get(value, value)

    @staticmethod
    def default_sources() -> list[str]:
        definitions = SourceRegistryService._definitions()
        fallback = ["reddit", "youtube", "appstore", "playstore", "web", "mastodon", "reclameaqui"]
        return [source for source in fallback if source in definitions and definitions[source].active]

    @staticmethod
    def active_sources() -> list[str]:
        definitions = SourceRegistryService._definitions()
        active = [source for source, cfg in definitions.items() if cfg.active]
        return sorted(active, key=lambda item: definitions[item].priority, reverse=True)

    @staticmethod
    def get_source_config(source: str) -> SourceConfig | None:
        normalized = SourceRegistryService.normalize_source_name(source)
        return SourceRegistryService._definitions().get(normalized)

    @staticmethod
    def source_priority(source: str) -> int:
        config = SourceRegistryService.get_source_config(source)
        if not config:
            return 0
        return int(config.priority)

    @staticmethod
    def normalize_sources(sources: list[str] | None) -> tuple[list[str], list[dict[str, str]]]:
        definitions = SourceRegistryService._definitions()
        normalized: list[str] = []
        errors: list[dict[str, str]] = []

        for raw_source in sources or []:
            raw_name = str(raw_source or "").strip().lower()
            source = SourceRegistryService.normalize_source_name(raw_name)
            config = definitions.get(source)

            if not config:
                errors.append({"source": raw_name or "unknown", "error": "Fonte nao suportada"})
                continue

            if not config.active:
                message = "Fonte despriorizada e inativa no nucleo atual"
                if config.deprecated:
                    message = "Fonte legada removida do nucleo. Use Reddit, YouTube, App Store, Play Store, Web ou Mastodon"
                errors.append({"source": source, "error": message})
                continue

            if source not in normalized:
                normalized.append(source)

        if not normalized:
            normalized = SourceRegistryService.default_sources()

        return normalized, errors

    @staticmethod
    def source_metadata() -> list[dict[str, Any]]:
        definitions = SourceRegistryService._definitions()
        ordered = sorted(definitions.values(), key=lambda item: item.priority, reverse=True)
        return [
            {
                "name": config.name,
                "type": config.source_type,
                "base_url": config.base_url,
                "active": config.active,
                "priority": config.priority,
                "fetch_mode": config.fetch_mode,
                "rate_limit_per_minute": config.rate_limit_per_minute,
                "parser": config.parser,
                "deprecated": config.deprecated,
            }
            for config in ordered
        ]

    @staticmethod
    def sync_defaults_to_db() -> None:
        db = get_db()
        if db is None:
            return

        now = utcnow()
        for config in SourceRegistryService._definitions().values():
            db.monitor_sources.update_one(
                {"name": config.name},
                {
                    "$set": {
                        "name": config.name,
                        "type": config.source_type,
                        "baseUrl": config.base_url,
                        "active": config.active,
                        "priority": config.priority,
                        "fetchMode": config.fetch_mode,
                        "rateLimitPerMinute": config.rate_limit_per_minute,
                        "parser": config.parser,
                        "deprecated": config.deprecated,
                        "updatedAt": now,
                    },
                    "$setOnInsert": {
                        "createdAt": now,
                    },
                },
                upsert=True,
            )
