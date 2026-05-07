import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.services.normalization_service import normalize_mention

logger = logging.getLogger(__name__)


class CollectorService:
    """Coleta dados reais sem depender de Apify.

    Fontes implementadas:
    - Google Places API: avaliações locais.
    - Reddit público: discussões/opiniões públicas.
    - X via snscrape: menções públicas quando a lib estiver disponível.

    Regra de produção:
    - Falha em uma fonte não derruba a busca.
    - Cada fonte retorna dados normalizados + erros separados.
    """

    @staticmethod
    async def collect(query: str, sources: list[str], period_days: int = 30, locality: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        tasks = []
        errors: list[dict[str, Any]] = []
        normalized_sources = [s.lower() for s in sources]

        if "google" in normalized_sources:
            tasks.append(CollectorService.google_places(query, locality=locality))

        if "reddit" in normalized_sources:
            tasks.append(CollectorService.reddit_public(query))

        if "x" in normalized_sources or "twitter" in normalized_sources:
            if settings.X_SNSCRAPE_ENABLED:
                tasks.append(CollectorService.x_snscrape(query, limit=settings.X_MAX_RESULTS))
            else:
                errors.append({
                    "source": "x",
                    "error": "Coleta do X/Twitter desativada. O snscrape usa endpoints não oficiais que hoje retornam blocked (404).",
                })

        if not tasks:
            if errors:
                return [], errors
            return [], [{"source": "system", "error": "Nenhuma fonte válida selecionada. Use google, reddit ou x."}]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        mentions: list[dict[str, Any]] = []

        for result in results:
            if isinstance(result, Exception):
                errors.append({"source": "unknown", "error": str(result)})
                continue

            source_mentions, source_errors = result
            mentions.extend(source_mentions)
            errors.extend(source_errors)

        return mentions, errors

    @staticmethod
    async def google_places(query: str, locality: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Busca lugares e reviews via Google Places API.

        Observação:
        - Google Places normalmente retorna poucas reviews por place.
        - Para reviews completas em massa, o Google não oferece endpoint público amplo.
        """
        if not settings.GOOGLE_PLACES_API_KEY or settings.GOOGLE_PLACES_API_KEY == "SUA_CHAVE_GOOGLE_PLACES":
            return [], [{"source": "google", "error": "GOOGLE_PLACES_API_KEY não configurada"}]

        search_text = f"{query} {locality}".strip() if locality else query
        base = "https://maps.googleapis.com/maps/api/place"

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                find_resp = await client.get(
                    f"{base}/textsearch/json",
                    params={
                        "query": search_text,
                        "key": settings.GOOGLE_PLACES_API_KEY,
                        "language": "pt-BR",
                    },
                )
                find_data = find_resp.json()
                status = find_data.get("status")

                if status not in ("OK", "ZERO_RESULTS"):
                    return [], [{"source": "google", "error": f"Google Places status={status}: {find_data.get('error_message')}"}]

                mentions: list[dict[str, Any]] = []
                for place in find_data.get("results", [])[:5]:
                    place_id = place.get("place_id")
                    if not place_id:
                        continue

                    details_resp = await client.get(
                        f"{base}/details/json",
                        params={
                            "place_id": place_id,
                            "fields": "name,rating,url,reviews,user_ratings_total,formatted_address",
                            "key": settings.GOOGLE_PLACES_API_KEY,
                            "language": "pt-BR",
                        },
                    )
                    details = details_resp.json().get("result", {})
                    place_url = details.get("url")

                    for review in details.get("reviews", []) or []:
                        published = datetime.fromtimestamp(review.get("time", 0), tz=timezone.utc) if review.get("time") else None
                        item = normalize_mention(
                            query=query,
                            source="google",
                            text=review.get("text"),
                            author=review.get("author_name"),
                            published_at=published,
                            url=review.get("author_url") or place_url,
                            rating=float(review.get("rating")) if review.get("rating") is not None else None,
                            raw={"place": details.get("name"), **review},
                        )
                        if item:
                            mentions.append(item)

                return mentions, []

            except Exception as exc:
                logger.exception("Erro Google Places")
                return [], [{"source": "google", "error": str(exc)}]

    @staticmethod
    async def reddit_public(query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Busca posts públicos do Reddit sem OAuth usando search.json.

        Funciona para protótipo/MVP. Para produção com alto volume, recomenda-se OAuth oficial.
        """
        headers = {"User-Agent": settings.REDDIT_USER_AGENT}
        url = "https://www.reddit.com/search.json"

        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            try:
                resp = await client.get(url, params={"q": query, "sort": "new", "limit": 25, "t": "month"})
                if resp.status_code >= 400:
                    return [], [{"source": "reddit", "error": f"Reddit HTTP {resp.status_code}: {resp.text[:300]}"}]

                data = resp.json()
                mentions: list[dict[str, Any]] = []

                for child in data.get("data", {}).get("children", []):
                    post = child.get("data", {})
                    text = (post.get("title") or "") + "\n" + (post.get("selftext") or "")
                    published = datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc) if post.get("created_utc") else None
                    item = normalize_mention(
                        query=query,
                        source="reddit",
                        text=text,
                        author=post.get("author"),
                        published_at=published,
                        url=f"https://reddit.com{post.get('permalink', '')}" if post.get("permalink") else None,
                        rating=float(post.get("score")) if post.get("score") is not None else None,
                        raw=post,
                    )
                    if item:
                        mentions.append(item)

                return mentions, []

            except Exception as exc:
                logger.exception("Erro Reddit")
                return [], [{"source": "reddit", "error": str(exc)}]

    @staticmethod
    async def x_snscrape(query: str, limit: int = 20) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Busca posts no X/Twitter via snscrape.

        Observação:
        - snscrape é não oficial e pode quebrar se o X mudar bloqueios.
        - Não usa API paga.
        - Executa em thread para não travar o loop async do FastAPI.
        """
        try:
            return await asyncio.to_thread(CollectorService._x_snscrape_sync, query, limit)
        except Exception as exc:
            logger.exception("Erro X/snscrape")
            return [], [{"source": "x", "error": str(exc)}]

    @staticmethod
    def _x_snscrape_sync(query: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            import snscrape.modules.twitter as sntwitter
        except Exception:
            return [], [{"source": "x", "error": "snscrape não instalado. Rode: pip install snscrape"}]

        mentions: list[dict[str, Any]] = []
        search_query = f'{query} lang:pt OR lang:en'

        try:
            for i, tweet in enumerate(sntwitter.TwitterSearchScraper(search_query).get_items()):
                if i >= limit:
                    break

                item = normalize_mention(
                    query=query,
                    source="x",
                    text=getattr(tweet, "rawContent", None) or getattr(tweet, "content", None),
                    author=getattr(getattr(tweet, "user", None), "username", None),
                    published_at=getattr(tweet, "date", None),
                    url=getattr(tweet, "url", None),
                    rating=float(getattr(tweet, "likeCount", 0) or 0),
                    raw={
                        "id": str(getattr(tweet, "id", "")),
                        "replyCount": getattr(tweet, "replyCount", 0),
                        "retweetCount": getattr(tweet, "retweetCount", 0),
                        "likeCount": getattr(tweet, "likeCount", 0),
                    },
                )
                if item:
                    mentions.append(item)

            return mentions, []

        except Exception as exc:
            return [], [{"source": "x", "error": str(exc)}]
