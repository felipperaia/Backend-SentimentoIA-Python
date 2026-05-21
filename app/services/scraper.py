from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.services.normalization_service import utcnow

logger = logging.getLogger(__name__)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_for_match(value: str) -> str:
    lowered = str(value or "").lower()
    folded = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("utf-8")
    return re.sub(r"\s+", " ", folded).strip()


def _to_iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            dt = utcnow()
    else:
        dt = utcnow()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).isoformat()


def _extract_json_object_after_token(payload: str, token: str) -> dict[str, Any] | None:
    idx = payload.find(token)
    if idx < 0:
        return None

    start = payload.find("{", idx)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for pos in range(start, len(payload)):
        ch = payload[pos]

        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            depth += 1
            continue

        if ch == "}":
            depth -= 1
            if depth == 0:
                fragment = payload[start : pos + 1]
                try:
                    parsed = json.loads(fragment)
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None

    return None


def _extract_runs_text(value: Any) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return _clean_text(value.get("simpleText"))

        runs = value.get("runs")
        if isinstance(runs, list):
            return _clean_text(" ".join(str((item or {}).get("text") or "") for item in runs))

        return ""

    if isinstance(value, list):
        return _clean_text(" ".join(_extract_runs_text(item) for item in value))

    return _clean_text(value)


def _iter_video_renderers(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "videoRenderer" and isinstance(value, dict):
                yield value
            else:
                yield from _iter_video_renderers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_video_renderers(item)


def _extract_search_target_url(href: str) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    if parsed.path.startswith("/l/"):
        params = parse_qs(parsed.query)
        if "uddg" in params and params["uddg"]:
            return unquote(params["uddg"][0])

    if raw.startswith("/"):
        return urljoin("https://duckduckgo.com", raw)

    return raw


class BaseCollector:
    source_name = "unknown"
    source_tier = "B"

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }

    REDDIT_HEADERS = {"User-Agent": "SentimentoIA/1.0"}

    def __init__(self) -> None:
        self.last_failure: dict[str, Any] | None = None

    @property
    def source(self) -> str:
        return self.source_name

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _set_failure(self, *, reason: str, error: str, timeout: bool) -> None:
        self.last_failure = {
            "source": self.source,
            "reason": str(reason or "temporary_failure"),
            "error": str(error or "Falha temporaria na coleta desta fonte"),
            "timeout": bool(timeout),
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        expect_json: bool = False,
        include_response_headers: bool = False,
    ) -> Any:
        timeout = max(1, int(settings.scraper_timeout_seconds or 30))
        attempts = max(1, int(settings.scraper_retry_attempts or 2))
        backoff = max(0.1, float(settings.scraper_retry_backoff_seconds or 2.0))
        request_delay = max(0.0, float(settings.scraper_request_delay_seconds or 1.0))
        self.last_failure = None

        for attempt in range(attempts):
            if request_delay > 0:
                await asyncio.sleep(request_delay)

            request_headers = dict(self.DEFAULT_HEADERS)
            if headers:
                request_headers.update(headers)

            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=request_headers,
                        json=json_body,
                    )
            except (httpx.HTTPError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
                is_timeout = isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError))
                self._set_failure(
                    reason="timeout" if is_timeout else "temporary_failure",
                    error="Tempo limite excedido na coleta desta fonte" if is_timeout else "Falha temporaria na coleta desta fonte",
                    timeout=is_timeout,
                )
                if attempt < attempts - 1:
                    await asyncio.sleep(backoff * (2**attempt))
                    continue
                return None

            status = int(response.status_code)
            if status == 429:
                self._set_failure(
                    reason="rate_limited",
                    error="Limite temporario da fonte atingido",
                    timeout=False,
                )
                if attempt < attempts - 1:
                    await asyncio.sleep(backoff * (2**attempt))
                    continue
                return None

            if status >= 400:
                self._set_failure(
                    reason="source_unavailable",
                    error="Fonte indisponivel no momento",
                    timeout=False,
                )
                if 500 <= status < 600 and attempt < attempts - 1:
                    await asyncio.sleep(backoff * (2**attempt))
                    continue
                return None

            if expect_json:
                try:
                    payload = response.json()
                    if include_response_headers:
                        return payload, dict(response.headers)
                    return payload
                except ValueError:
                    self._set_failure(
                        reason="temporary_failure",
                        error="Falha temporaria na resposta da fonte",
                        timeout=False,
                    )
                    return None

            if include_response_headers:
                return response.text, dict(response.headers)
            return response.text

        return None

    def _score(self, raw: dict[str, Any], query: str) -> float:
        query_norm = _normalize_for_match(query)
        title_norm = _normalize_for_match(raw.get("title") or "")
        body_norm = _normalize_for_match(raw.get("text") or raw.get("snippet") or "")

        if query_norm and query_norm in title_norm:
            return 1.0
        if query_norm and query_norm in body_norm:
            return 0.7
        return 0.3

    def _normalize(self, raw: dict[str, Any], query: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        title = _clean_text(raw.get("title"))
        body = _clean_text(raw.get("text") or raw.get("snippet"))
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

        effective_text = body or title
        min_text_length = max(1, int(settings.scraper_min_text_length or 20))
        if len(effective_text) < min_text_length:
            return None

        created_at = raw.get("created_at") or raw.get("published_at") or utcnow()
        url = _clean_text(raw.get("url"))

        return {
            "text": effective_text,
            "title": title or effective_text[:160],
            "snippet": body[:500] if body else effective_text[:500],
            "source": self.source,
            "source_tier": self.source_tier,
            "url": url,
            "created_at": _to_iso_datetime(created_at),
            "published_at": _to_iso_datetime(created_at),
            "score": float(self._score(raw, query)),
            "sentiment": "neutral",
            "author": _clean_text(raw.get("author")) or None,
            "raw": raw,
        }

    def _dedupe(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_hashes: set[str] = set()
        output: list[dict[str, Any]] = []

        for item in items:
            digest = hashlib.sha256(_normalize_for_match(item.get("text") or "").encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            item["content_hash"] = digest
            output.append(item)

        return output

    def _normalize_many(self, raw_items: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in raw_items:
            item = self._normalize(raw, query)
            if item is not None:
                normalized.append(item)

        deduped = self._dedupe(normalized)
        max_items = max(1, int(limit))
        return deduped[:max_items]


class ReclameAquiCollector(BaseCollector):
    source_name = "reclameaqui"
    source_tier = "A"

    @staticmethod
    def _extract_items_from_any(node: Any, output: list[dict[str, Any]]) -> None:
        if isinstance(node, list):
            for item in node:
                ReclameAquiCollector._extract_items_from_any(item, output)
            return

        if not isinstance(node, dict):
            return

        title = _clean_text(
            node.get("title")
            or node.get("subject")
            or node.get("problem")
            or node.get("complaintTitle")
            or node.get("company")
        )
        text = _clean_text(
            node.get("description")
            or node.get("problemDescription")
            or node.get("content")
            or node.get("text")
            or title
        )
        url = _clean_text(node.get("url") or node.get("link") or node.get("href"))

        if title or text:
            output.append(
                {
                    "title": title,
                    "text": text,
                    "snippet": text,
                    "url": url,
                    "author": _clean_text(node.get("author") or node.get("consumer") or ""),
                    "created_at": node.get("created_at") or node.get("createdAt") or node.get("date") or utcnow(),
                    "metadata": node,
                }
            )

        for value in node.values():
            ReclameAquiCollector._extract_items_from_any(value, output)

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any] | None:
        match = re.search(r"window\.__NEXT_DATA__\s*=\s*(\{.*?\});", html, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(1))
                return payload if isinstance(payload, dict) else None
            except json.JSONDecodeError:
                pass

        soup = BeautifulSoup(html, "html.parser")
        script = soup.select_one("script#__NEXT_DATA__")
        if not script:
            return None

        raw_json = script.get_text(strip=True)
        if not raw_json:
            return None

        try:
            payload = json.loads(raw_json)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term or not bool(settings.enable_reclame_aqui):
                return []

            max_limit = max(1, int(limit))
            request_headers = {
                "Origin": "https://www.reclameaqui.com.br",
                "Referer": "https://www.reclameaqui.com.br/",
            }

            primary_url = f"https://iosearch.reclameaqui.com.br/raichu/search/query/0/{max_limit}"
            payload = await self._request(
                "GET",
                primary_url,
                headers=request_headers,
                params={"q": term},
                expect_json=True,
            )

            raw_items: list[dict[str, Any]] = []
            if isinstance(payload, (dict, list)):
                self._extract_items_from_any(payload, raw_items)

            if not raw_items:
                html = await self._request(
                    "GET",
                    "https://www.reclameaqui.com.br/busca/",
                    headers=request_headers,
                    params={"q": term},
                )
                if isinstance(html, str) and html.strip():
                    next_payload = self._extract_next_data(html)
                    if isinstance(next_payload, dict):
                        self._extract_items_from_any(next_payload, raw_items)

            return self._normalize_many(raw_items, term, max_limit)
        except Exception as exc:
            logger.warning("ReclameAquiCollector falhou: %s", exc)
            return []


class RedditCollector(BaseCollector):
    source_name = "reddit"
    source_tier = "A"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            result = await self._request(
                "GET",
                "https://www.reddit.com/search.json",
                params={
                    "q": term,
                    "limit": max(1, int(limit)),
                    "sort": "new",
                    "type": "link",
                },
                headers=self.REDDIT_HEADERS,
                expect_json=True,
                include_response_headers=True,
            )
            if not isinstance(result, tuple) or len(result) != 2:
                return []

            payload, response_headers = result
            if not isinstance(payload, dict):
                return []

            remaining = str((response_headers or {}).get("X-Ratelimit-Remaining") or "").strip()
            if remaining:
                logger.info("Reddit X-Ratelimit-Remaining=%s", remaining)

            children = ((payload.get("data") or {}).get("children") or [])
            raw_items: list[dict[str, Any]] = []
            for child in children:
                data = (child or {}).get("data") or {}
                created_utc = data.get("created_utc")
                created_at = utcnow()
                if isinstance(created_utc, (int, float)):
                    created_at = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)

                permalink = _clean_text(data.get("permalink"))
                post_url = urljoin("https://www.reddit.com", permalink) if permalink else _clean_text(data.get("url"))
                body = _clean_text(data.get("selftext"))

                raw_items.append(
                    {
                        "title": _clean_text(data.get("title")),
                        "text": _clean_text(f"{data.get('title') or ''} {body}"),
                        "snippet": body,
                        "url": post_url,
                        "author": _clean_text(data.get("author")),
                        "created_at": created_at,
                        "metadata": {
                            "id": data.get("id"),
                            "subreddit": data.get("subreddit"),
                            "score": data.get("score"),
                            "num_comments": data.get("num_comments"),
                        },
                    }
                )

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("RedditCollector falhou: %s", exc)
            return []


class YouTubeCollector(BaseCollector):
    source_name = "youtube"
    source_tier = "A"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            html = await self._request(
                "GET",
                "https://www.youtube.com/results",
                params={"search_query": term},
            )
            if not isinstance(html, str) or not html.strip():
                return []

            initial_data = _extract_json_object_after_token(html, "var ytInitialData = ")
            if not isinstance(initial_data, dict):
                initial_data = _extract_json_object_after_token(html, "ytInitialData = ")
            if not isinstance(initial_data, dict):
                return []

            scoped_path = (
                ((initial_data.get("contents") or {}).get("twoColumnSearchResultsRenderer") or {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )

            raw_items: list[dict[str, Any]] = []
            if isinstance(scoped_path, list) and scoped_path:
                first_section = (scoped_path[0] or {}).get("itemSectionRenderer", {}).get("contents", [])
                video_nodes = [item.get("videoRenderer") for item in first_section if isinstance(item, dict)]
            else:
                video_nodes = []

            if not video_nodes:
                video_nodes = list(_iter_video_renderers(initial_data))

            for renderer in video_nodes:
                if not isinstance(renderer, dict):
                    continue

                title = _extract_runs_text((renderer or {}).get("title"))
                details = ((renderer or {}).get("detailedMetadataSnippets") or [{}])
                snippet = _extract_runs_text((details[0] or {}).get("snippetText"))
                if not snippet:
                    snippet = _extract_runs_text((renderer or {}).get("descriptionSnippet"))
                author = _extract_runs_text((renderer or {}).get("ownerText"))
                video_id = _clean_text((renderer or {}).get("videoId"))
                if not video_id:
                    continue

                raw_items.append(
                    {
                        "title": title,
                        "text": _clean_text(f"{title} {snippet}"),
                        "snippet": snippet,
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "author": author,
                        "created_at": utcnow(),
                        "metadata": {
                            "video_id": video_id,
                            "owner": author,
                        },
                    }
                )

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("YouTubeCollector falhou: %s", exc)
            return []


class AppStoreCollector(BaseCollector):
    source_name = "appstore"
    source_tier = "A"

    @staticmethod
    def _itunes_label(value: Any) -> str:
        if isinstance(value, dict):
            return _clean_text(value.get("label") or value.get("#text") or value.get("value"))
        return _clean_text(value)

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        term = _clean_text(query)
        if not term:
            return []

        try:
            payload = await self._request(
                "GET",
                "https://itunes.apple.com/search",
                params={
                    "term": term,
                    "entity": "software",
                    "country": "br",
                    "limit": max(1, int(limit)),
                },
                expect_json=True,
            )
            if not isinstance(payload, dict):
                return []

            apps = payload.get("results") or []
            if not isinstance(apps, list):
                return []

            raw_items: list[dict[str, Any]] = []
            for app in apps[: max(1, int(limit))]:
                app_id = _clean_text((app or {}).get("trackId"))
                app_name = _clean_text((app or {}).get("trackName") or term)
                if not app_id:
                    continue

                reviews_payload = await self._request(
                    "GET",
                    f"https://itunes.apple.com/br/rss/customerreviews/id={app_id}/sortBy=mostRecent/json",
                    expect_json=True,
                )
                if not isinstance(reviews_payload, dict):
                    continue

                feed = reviews_payload.get("feed") or {}
                entries = feed.get("entry") or []
                if isinstance(entries, dict):
                    entries = [entries]
                if not isinstance(entries, list):
                    continue

                for entry in entries:
                    if not isinstance(entry, dict):
                        continue

                    content = self._itunes_label(entry.get("content"))
                    rating = self._itunes_label(entry.get("im:rating"))
                    if not content or not rating:
                        continue

                    author = entry.get("author") or {}
                    links = entry.get("link") or []
                    if isinstance(links, dict):
                        links = [links]

                    review_url = ""
                    for link in links:
                        attrs = (link or {}).get("attributes") or {}
                        href = _clean_text(attrs.get("href"))
                        if href:
                            review_url = href
                            break

                    if not review_url:
                        review_url = f"https://apps.apple.com/br/app/id{app_id}"

                    raw_items.append(
                        {
                            "title": self._itunes_label(entry.get("title")) or f"Review App Store - {app_name}",
                            "text": content,
                            "snippet": content,
                            "url": review_url,
                            "author": self._itunes_label((author or {}).get("name")),
                            "created_at": self._itunes_label(entry.get("updated")) or utcnow(),
                            "metadata": {
                                "rating": rating,
                                "version": self._itunes_label(entry.get("im:version")),
                                "app_id": app_id,
                                "app_name": app_name,
                            },
                        }
                    )

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("AppStoreCollector falhou: %s", exc)
            return []


class PlayStoreCollector(BaseCollector):
    source_name = "playstore"
    source_tier = "A"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        term = _clean_text(query)
        if not term:
            return []

        try:
            raw_items = await asyncio.to_thread(self._collect_sync, term, limit)
            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("PlayStoreCollector falhou: %s", exc)
            return []

    @staticmethod
    def _collect_sync(query: str, limit: int) -> list[dict[str, Any]]:
        try:
            from google_play_scraper import Sort, reviews, search
        except Exception:
            logger.warning("Dependencia google-play-scraper indisponivel")
            return []

        raw_items: list[dict[str, Any]] = []

        try:
            search_results = search(query, lang="pt", country="br", n_hits=max(1, int(limit)))
        except Exception as exc:
            logger.warning("Busca na Play Store falhou: %s", exc)
            return []

        for hit in search_results[: max(1, int(limit))]:
            app_id = _clean_text((hit or {}).get("appId"))
            app_title = _clean_text((hit or {}).get("title") or query)
            if not app_id:
                continue

            try:
                review_rows, _ = reviews(
                    app_id,
                    lang="pt",
                    country="br",
                    count=max(1, int(limit)),
                    sort=Sort.NEWEST,
                )
            except Exception as exc:
                logger.warning("Coleta de reviews Play Store falhou (%s): %s", app_id, exc)
                continue

            for review in review_rows or []:
                content = _clean_text((review or {}).get("content"))
                if not content:
                    continue

                review_id = _clean_text((review or {}).get("reviewId"))
                review_url = f"https://play.google.com/store/apps/details?id={app_id}"
                if review_id:
                    review_url = f"{review_url}&reviewId={review_id}"

                raw_items.append(
                    {
                        "title": _clean_text(f"Review Google Play - {app_title}"),
                        "text": content,
                        "snippet": content,
                        "url": review_url,
                        "author": _clean_text((review or {}).get("userName")),
                        "created_at": (review or {}).get("at") or utcnow(),
                        "metadata": {
                            "score": (review or {}).get("score"),
                            "thumbs_up": (review or {}).get("thumbsUpCount"),
                            "app_id": app_id,
                            "app_name": app_title,
                        },
                    }
                )

        return raw_items


class GlassdoorCollector(BaseCollector):
    source_name = "glassdoor"
    source_tier = "B"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            html = await self._request(
                "GET",
                "https://html.duckduckgo.com/html/",
                params={"q": f"site:glassdoor.com.br {term} avaliacoes"},
            )
            if not isinstance(html, str) or not html.strip():
                return []

            soup = BeautifulSoup(html, "html.parser")
            raw_items: list[dict[str, Any]] = []

            for card in soup.select(".result"):
                snippet_node = card.select_one(".result__snippet")
                title_node = card.select_one(".result__a")
                snippet = _clean_text(snippet_node.get_text(" ", strip=True) if snippet_node else "")
                title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
                href = _clean_text(title_node.get("href") if title_node else "")

                if not snippet and not title:
                    continue

                raw_items.append(
                    {
                        "title": title or "Glassdoor",
                        "text": _clean_text(f"{title} {snippet}"),
                        "snippet": snippet,
                        "url": _extract_search_target_url(href),
                        "author": "duckduckgo",
                        "created_at": utcnow(),
                        "metadata": {},
                    }
                )

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("GlassdoorCollector falhou: %s", exc)
            return []


class TrustpilotCollector(BaseCollector):
    source_name = "trustpilot"
    source_tier = "B"

    @staticmethod
    def _extract_ld_json_items(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[dict[str, Any]] = []

        for script in soup.select("script[type='application/ld+json']"):
            raw_json = script.get_text(strip=True)
            if not raw_json:
                continue

            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                continue

            queue: list[Any] = [payload]
            while queue:
                node = queue.pop(0)
                if isinstance(node, list):
                    queue.extend(node)
                    continue
                if not isinstance(node, dict):
                    continue

                item_list = node.get("itemListElement")
                if isinstance(item_list, list):
                    for item in item_list:
                        inner = item.get("item") if isinstance(item, dict) else None
                        if isinstance(inner, dict):
                            name = _clean_text(inner.get("name"))
                            url = _clean_text(inner.get("url"))
                            description = _clean_text(inner.get("description"))
                            if name or description:
                                items.append(
                                    {
                                        "title": name or "Trustpilot",
                                        "text": _clean_text(f"{name} {description}"),
                                        "snippet": description,
                                        "url": url,
                                        "author": "trustpilot",
                                        "created_at": utcnow(),
                                        "metadata": inner,
                                    }
                                )

                for value in node.values():
                    if isinstance(value, (dict, list)):
                        queue.append(value)

        return items

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            html = await self._request(
                "GET",
                "https://www.trustpilot.com/search",
                params={"query": term},
            )
            raw_items = self._extract_ld_json_items(html) if isinstance(html, str) else []

            if not raw_items:
                ddg_html = await self._request(
                    "GET",
                    "https://html.duckduckgo.com/html/",
                    params={"q": f"site:trustpilot.com {term}"},
                )
                if isinstance(ddg_html, str):
                    soup = BeautifulSoup(ddg_html, "html.parser")
                    for card in soup.select(".result"):
                        title_node = card.select_one(".result__a")
                        snippet_node = card.select_one(".result__snippet")
                        title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
                        snippet = _clean_text(snippet_node.get_text(" ", strip=True) if snippet_node else "")
                        href = _clean_text(title_node.get("href") if title_node else "")
                        if not title and not snippet:
                            continue
                        raw_items.append(
                            {
                                "title": title or "Trustpilot",
                                "text": _clean_text(f"{title} {snippet}"),
                                "snippet": snippet,
                                "url": _extract_search_target_url(href),
                                "author": "duckduckgo",
                                "created_at": utcnow(),
                                "metadata": {},
                            }
                        )

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("TrustpilotCollector falhou: %s", exc)
            return []


class WebSearchCollector(BaseCollector):
    source_name = "web"
    source_tier = "web"

    @staticmethod
    def _parse_duckduckgo(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for body in soup.select(".result__body"):
            title_node = body.select_one(".result__a")
            snippet_node = body.select_one(".result__snippet")
            if title_node is None:
                continue

            title = _clean_text(title_node.get_text(" ", strip=True))
            snippet = _clean_text(snippet_node.get_text(" ", strip=True) if snippet_node else "")
            href = _clean_text(title_node.get("href"))

            raw_items.append(
                {
                    "title": title,
                    "text": _clean_text(f"{title} {snippet}"),
                    "snippet": snippet,
                    "url": _extract_search_target_url(href),
                    "author": "duckduckgo",
                    "created_at": utcnow(),
                    "metadata": {},
                }
            )

        return raw_items

    @staticmethod
    def _parse_bing(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for card in soup.select("li.b_algo"):
            title_node = card.select_one("h2 a")
            snippet_node = card.select_one(".b_caption p")
            if title_node is None:
                continue

            title = _clean_text(title_node.get_text(" ", strip=True))
            snippet = _clean_text(snippet_node.get_text(" ", strip=True) if snippet_node else "")
            raw_items.append(
                {
                    "title": title,
                    "text": _clean_text(f"{title} {snippet}"),
                    "snippet": snippet,
                    "url": _clean_text(title_node.get("href")),
                    "author": "bing",
                    "created_at": utcnow(),
                    "metadata": {},
                }
            )

        return raw_items

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            ddg_html = await self._request(
                "GET",
                "https://html.duckduckgo.com/html/",
                params={"q": term},
            )
            raw_items = self._parse_duckduckgo(ddg_html) if isinstance(ddg_html, str) else []

            should_fallback = not raw_items
            if self.last_failure and str(self.last_failure.get("reason") or "") == "rate_limited":
                should_fallback = True

            if should_fallback:
                bing_html = await self._request(
                    "GET",
                    "https://www.bing.com/search",
                    params={"q": term, "count": max(1, int(limit))},
                )
                if isinstance(bing_html, str):
                    raw_items.extend(self._parse_bing(bing_html))

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("WebSearchCollector falhou: %s", exc)
            return []


class MastodonCollector(BaseCollector):
    source_name = "mastodon"
    source_tier = "web"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            payload = await self._request(
                "GET",
                "https://mastodon.social/api/v2/search",
                params={"q": term, "type": "statuses", "limit": max(1, int(limit))},
                expect_json=True,
            )
            if not isinstance(payload, dict):
                return []

            raw_items: list[dict[str, Any]] = []
            for status in payload.get("statuses") or []:
                content_html = _clean_text((status or {}).get("content"))
                text = _clean_text(BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True))
                if not text:
                    continue

                account = (status or {}).get("account") or {}
                author = _clean_text(account.get("display_name") or account.get("username"))

                raw_items.append(
                    {
                        "title": text[:120],
                        "text": text,
                        "snippet": text,
                        "url": _clean_text((status or {}).get("url") or (status or {}).get("uri")),
                        "author": author,
                        "created_at": (status or {}).get("created_at") or utcnow(),
                        "metadata": {
                            "replies": (status or {}).get("replies_count"),
                            "reblogs": (status or {}).get("reblogs_count"),
                            "favourites": (status or {}).get("favourites_count"),
                        },
                    }
                )

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("MastodonCollector falhou: %s", exc)
            return []


__all__ = [
    "AppStoreCollector",
    "BaseCollector",
    "GlassdoorCollector",
    "MastodonCollector",
    "PlayStoreCollector",
    "ReclameAquiCollector",
    "RedditCollector",
    "TrustpilotCollector",
    "WebSearchCollector",
    "YouTubeCollector",
]
