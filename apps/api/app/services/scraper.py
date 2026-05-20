from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/136.0.0.0 Safari/537.36",
]


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_for_match(value: str) -> str:
    lowered = str(value or "").lower()
    folded = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("utf-8")
    return re.sub(r"\s+", " ", folded).strip()


def _slugify(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("utf-8")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", folded.lower()).strip("-")
    return slug or "empresa"


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


def _extract_reclameaqui_records(node: Any, output: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _extract_reclameaqui_records(item, output)
        return

    if not isinstance(node, dict):
        return

    title = _clean_text(
        node.get("title")
        or node.get("subject")
        or node.get("problema")
        or node.get("problem")
        or node.get("company")
    )
    text = _clean_text(
        node.get("description")
        or node.get("descricao")
        or node.get("content")
        or node.get("problemDescription")
        or node.get("problem")
        or node.get("texto")
        or title
    )

    if title or text:
        output.append(
            {
                "title": title,
                "text": text,
                "snippet": text,
                "url": str(node.get("url") or node.get("link") or "").strip(),
                "author": _clean_text(node.get("author") or node.get("consumer") or ""),
                "created_at": node.get("created_at") or node.get("createdAt") or node.get("date") or utcnow(),
                "metadata": node,
            }
        )

    for value in node.values():
        _extract_reclameaqui_records(value, output)


class BaseCollector:
    source_name = "unknown"
    source_tier = "B"

    @property
    def source(self) -> str:
        return self.source_name

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _random_headers(self) -> dict[str, str]:
        user_agent = random.choice(USER_AGENTS)
        return {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _score(self, raw: dict[str, Any], query: str) -> float:
        query_norm = _normalize_for_match(query)
        title_norm = _normalize_for_match(raw.get("title") or "")
        body_norm = _normalize_for_match(raw.get("text") or raw.get("snippet") or "")

        metadata = raw.get("metadata") or {}
        metadata_norm = _normalize_for_match(json.dumps(metadata, ensure_ascii=False, default=str))

        if query_norm and query_norm in title_norm:
            return 1.0
        if query_norm and query_norm in body_norm:
            return 0.7
        if query_norm and query_norm in metadata_norm:
            return 0.3
        return 0.0

    def _normalize(self, raw: dict[str, Any], query: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        query_norm = _normalize_for_match(query)
        title = _clean_text(raw.get("title"))
        body = _clean_text(raw.get("text") or raw.get("snippet"))
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

        combined = _clean_text(" ".join(part for part in [title, body, json.dumps(metadata, ensure_ascii=False)] if part))
        combined_norm = _normalize_for_match(combined)
        if not query_norm or query_norm not in combined_norm:
            return None

        min_text_length = max(1, int(getattr(settings, "SCRAPER_MIN_TEXT_LENGTH", 20) or 20))
        effective_text = body or combined
        if len(effective_text) < min_text_length:
            return None

        score = self._score(raw, query)
        if score <= 0:
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
            "score": float(score),
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

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = False,
    ) -> Any:
        timeout = max(5, int(getattr(settings, "SCRAPER_TIMEOUT_SECONDS", 20) or 20))

        for attempt in range(3):
            await asyncio.sleep(random.uniform(2.0, 5.0))
            request_headers = self._random_headers()
            if headers:
                request_headers.update(headers)

            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=request_headers,
                    )
            except Exception as exc:
                logger.warning("%s request falhou (%s) tentativa %s/3", self.source, exc, attempt + 1)
                if attempt < 2:
                    await asyncio.sleep((2**attempt) * random.uniform(0.5, 1.5))
                continue

            status = int(response.status_code)
            if status in {403, 429}:
                logger.warning("%s recebeu HTTP %s em %s", self.source, status, url)
                if attempt < 2:
                    await asyncio.sleep(random.uniform(15.0, 30.0))
                    continue
                return None

            if 500 <= status < 600:
                logger.warning("%s recebeu HTTP %s em %s", self.source, status, url)
                if attempt < 2:
                    await asyncio.sleep((2**attempt) * random.uniform(0.5, 1.5))
                    continue
                return None

            if status >= 400:
                logger.warning("%s recebeu HTTP %s em %s", self.source, status, url)
                return None

            if not expect_json:
                return response.text

            try:
                return response.json()
            except ValueError:
                logger.warning("%s recebeu JSON invalido em %s", self.source, url)
                return None

        return None

    @staticmethod
    def _playwright_enabled() -> bool:
        return bool(getattr(settings, "ENABLE_PLAYWRIGHT", False))

    async def _fetch_html_with_playwright(self, url: str) -> str:
        if not self._playwright_enabled():
            return ""

        try:
            from playwright.async_api import async_playwright
        except Exception:
            logger.warning("Playwright indisponivel para %s", self.source)
            return ""

        timeout_ms = max(5000, int(getattr(settings, "SCRAPER_TIMEOUT_SECONDS", 20) * 1000))

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=random.choice(USER_AGENTS), locale="pt-BR")
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(1200)
                html = await page.content()
                await context.close()
                await browser.close()
                return html
        except Exception as exc:
            logger.warning("Playwright falhou para %s: %s", self.source, exc)
            return ""


class RedditCollector(BaseCollector):
    source_name = "reddit"
    source_tier = "A"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            endpoint = f"{str(settings.SCRAPER_REDDIT_URL).rstrip('/')}/search.json"
            payload = await self._request(
                "GET",
                endpoint,
                params={"q": term, "sort": "new", "limit": 25, "raw_json": 1},
                expect_json=True,
            )
            if not isinstance(payload, dict):
                return []

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
                params={"search_query": f"{term} review avaliacao"},
            )
            if not isinstance(html, str) or not html.strip():
                return []

            initial_data = _extract_json_object_after_token(html, "var ytInitialData")
            if not isinstance(initial_data, dict):
                initial_data = _extract_json_object_after_token(html, "ytInitialData")
            if not isinstance(initial_data, dict):
                return []

            raw_items: list[dict[str, Any]] = []
            for renderer in _iter_video_renderers(initial_data):
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
                            "published": _extract_runs_text((renderer or {}).get("publishedTimeText")),
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

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        term = _clean_text(query)
        if not term:
            return []

        try:
            raw_items = await asyncio.to_thread(self._collect_sync, term, limit)
            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("AppStoreCollector falhou: %s", exc)
            return []

    def _collect_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            from app_store_scraper import AppStore
        except Exception:
            logger.warning("Dependencia app-store-scraper indisponivel")
            return []

        app_id = _clean_text(getattr(settings, "COMPANY_APP_STORE_ID", "")) or None

        try:
            if app_id:
                try:
                    app = AppStore(country="br", app_name=query, app_id=app_id)
                except TypeError:
                    app = AppStore(country="br", app_name=query)
            else:
                app = AppStore(country="br", app_name=query)

            app.review(how_many=max(5, int(limit)))
            reviews = list(getattr(app, "reviews", []) or [])
        except Exception as exc:
            logger.warning("App Store request falhou: %s", exc)
            return []

        fallback_url = f"https://apps.apple.com/br/search?term={quote_plus(query)}"
        raw_items: list[dict[str, Any]] = []

        for review in reviews:
            content = _clean_text(review.get("review") or review.get("content"))
            if not content:
                continue

            raw_items.append(
                {
                    "title": _clean_text(review.get("title") or "Review App Store"),
                    "text": content,
                    "snippet": content,
                    "url": _clean_text(review.get("reviewUrl") or review.get("url") or fallback_url),
                    "author": _clean_text(review.get("userName")),
                    "created_at": review.get("date") or utcnow(),
                    "metadata": {"rating": review.get("rating"), "id": review.get("id")},
                }
            )

        return raw_items


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

    def _collect_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            from google_play_scraper import Sort, reviews, search
        except Exception:
            logger.warning("Dependencia google-play-scraper indisponivel")
            return []

        app_id = _clean_text(getattr(settings, "COMPANY_PLAY_STORE_ID", ""))

        try:
            if not app_id:
                hits = search(query, lang="pt", country="br", n_hits=3)
                if not hits:
                    return []
                app_id = _clean_text((hits[0] or {}).get("appId"))

            if not app_id:
                return []

            review_rows, _ = reviews(
                app_id,
                lang="pt",
                country="br",
                count=max(5, int(limit)),
                sort=Sort.NEWEST,
            )
        except Exception as exc:
            logger.warning("Google Play request falhou: %s", exc)
            return []

        raw_items: list[dict[str, Any]] = []
        for review in review_rows or []:
            content = _clean_text(review.get("content"))
            if not content:
                continue

            review_id = _clean_text(review.get("reviewId"))
            review_url = f"https://play.google.com/store/apps/details?id={app_id}"
            if review_id:
                review_url = f"{review_url}&reviewId={review_id}"

            raw_items.append(
                {
                    "title": _clean_text(f"Review Google Play - {app_id}"),
                    "text": content,
                    "snippet": content,
                    "url": review_url,
                    "author": _clean_text(review.get("userName")),
                    "created_at": review.get("at") or utcnow(),
                    "metadata": {"score": review.get("score"), "thumbs_up": review.get("thumbsUpCount")},
                }
            )

        return raw_items


class TrustpilotCollector(BaseCollector):
    source_name = "trustpilot"
    source_tier = "B"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            search_html = await self._request(
                "GET",
                "https://br.trustpilot.com/search",
                params={"query": term},
            )
            target_url = self._first_review_url(search_html) if isinstance(search_html, str) else ""
            if not target_url:
                target_url = f"https://br.trustpilot.com/review/{_slugify(term)}.com"

            review_url = f"{target_url}{'&' if '?' in target_url else '?'}languages=pt"
            html = await self._request("GET", review_url)
            if not isinstance(html, str) or not html.strip():
                html = await self._fetch_html_with_playwright(review_url)
            if not isinstance(html, str) or not html.strip():
                return []

            raw_items = self._parse_reviews(html, review_url)
            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("TrustpilotCollector falhou: %s", exc)
            return []

    @staticmethod
    def _first_review_url(html: str | None) -> str:
        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.select("a[href*='/review/']"):
            href = _clean_text(anchor.get("href"))
            if not href:
                continue
            if href.startswith("http"):
                return href
            return urljoin("https://br.trustpilot.com", href)
        return ""

    @staticmethod
    def _parse_reviews(html: str, default_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for article in soup.select("article[class*='review'], article[data-service-review-id], article"):
            body_node = article.select_one("p[class*='review__body'], p[data-service-review-text-typography], p")
            text = _clean_text(body_node.get_text(" ", strip=True) if body_node else "")
            if not text:
                continue

            title_node = article.select_one("h2, h3, a")
            title = _clean_text(title_node.get_text(" ", strip=True) if title_node else text[:120])
            time_node = article.select_one("time")
            created_at = _clean_text(time_node.get("datetime") if time_node else "") or utcnow()

            link_node = article.select_one("a[href*='/reviews/'], a[href*='/review/']")
            href = _clean_text(link_node.get("href") if link_node else "")
            url = default_url
            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = urljoin("https://br.trustpilot.com", href)

            raw_items.append(
                {
                    "title": title,
                    "text": text,
                    "snippet": text,
                    "url": url,
                    "author": "trustpilot",
                    "created_at": created_at,
                    "metadata": {},
                }
            )

            if len(raw_items) >= 10:
                break

        return raw_items


class GlassdoorCollector(BaseCollector):
    source_name = "glassdoor"
    source_tier = "B"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            slug = _slugify(term)
            target_url = f"https://www.glassdoor.com.br/Avaliacoes/{slug}-avaliacoes.htm"

            html = await self._request("GET", target_url)
            raw_items = self._parse_reviews(html, target_url) if isinstance(html, str) else []

            if not raw_items:
                ddg_html = await self._request(
                    "GET",
                    "https://html.duckduckgo.com/html/",
                    params={"q": f"site:glassdoor.com.br {term}"},
                )
                if isinstance(ddg_html, str):
                    raw_items.extend(self._parse_fallback_snippets(ddg_html))

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("GlassdoorCollector falhou: %s", exc)
            return []

    @staticmethod
    def _parse_reviews(html: str, default_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for block in soup.select("div[class*='review'], li[class*='review'], article"):
            review_text = _clean_text(
                " ".join(
                    part.get_text(" ", strip=True)
                    for part in block.select(
                        "span[class*='reviewText'], span[data-test='pros'], span[data-test='cons'], p"
                    )
                )
            )
            if not review_text:
                continue

            title_node = block.select_one("h2, h3")
            title = _clean_text(title_node.get_text(" ", strip=True) if title_node else review_text[:120])
            raw_items.append(
                {
                    "title": title,
                    "text": review_text,
                    "snippet": review_text,
                    "url": default_url,
                    "author": "glassdoor",
                    "created_at": utcnow(),
                    "metadata": {},
                }
            )
            if len(raw_items) >= 10:
                break

        return raw_items

    @staticmethod
    def _parse_fallback_snippets(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for card in soup.select("div.result, article, li.result"):
            title_node = card.select_one("a.result__a, h2 a, a[href]")
            snippet_node = card.select_one(".result__snippet, .result__body")
            title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
            snippet = _clean_text(snippet_node.get_text(" ", strip=True) if snippet_node else "")
            href = _clean_text(title_node.get("href") if title_node else "")
            url = _extract_search_target_url(href)

            if not snippet and not title:
                continue

            raw_items.append(
                {
                    "title": title,
                    "text": _clean_text(f"{title} {snippet}"),
                    "snippet": snippet,
                    "url": url,
                    "author": "duckduckgo",
                    "created_at": utcnow(),
                    "metadata": {},
                }
            )

        return raw_items


class ReclameAquiCollector(BaseCollector):
    source_name = "reclameaqui"
    source_tier = "A"

    @staticmethod
    def _enabled() -> bool:
        enabled_new = bool(getattr(settings, "ENABLE_RECLAME_AQUI", False))
        enabled_legacy = bool(getattr(settings, "ENABLE_RECLAMEAQUI", False))
        return bool(enabled_new or enabled_legacy)

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            if not self._enabled():
                return []

            payload = await self._request(
                "GET",
                "https://iosearch.reclameaqui.com.br/raichu/query/companyComplains/0/10",
                params={"q": term},
                expect_json=True,
            )

            raw_items: list[dict[str, Any]] = []
            if isinstance(payload, (dict, list)):
                _extract_reclameaqui_records(payload, raw_items)

            if not raw_items:
                search_url = f"{str(settings.SCRAPER_RECLAMEAQUI_URL).rstrip('/')}/busca/"
                html = await self._request("GET", search_url, params={"q": term})
                if isinstance(html, str):
                    raw_items.extend(self._parse_html_fallback(html, str(settings.SCRAPER_RECLAMEAQUI_URL).rstrip("/")))

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("ReclameAquiCollector falhou: %s", exc)
            return []

    @staticmethod
    def _parse_html_fallback(html: str, base_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for link in soup.select("a[href*='/reclamacao/']"):
            href = _clean_text(link.get("href"))
            title = _clean_text(link.get_text(" ", strip=True))
            if not href or not title:
                continue

            full_url = href if href.startswith("http") else urljoin(base_url + "/", href)
            raw_items.append(
                {
                    "title": title,
                    "text": title,
                    "snippet": title,
                    "url": full_url,
                    "author": "reclameaqui",
                    "created_at": utcnow(),
                    "metadata": {},
                }
            )
            if len(raw_items) >= 20:
                break

        return raw_items


class WebSearchCollector(BaseCollector):
    source_name = "web"
    source_tier = "web"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            ddg_html = await self._request(
                "GET",
                "https://html.duckduckgo.com/html/",
                params={"q": f"{term} reclamacao avaliacao"},
            )
            raw_items = self._parse_duckduckgo(ddg_html) if isinstance(ddg_html, str) else []

            if len(raw_items) < 3:
                bing_html = await self._request(
                    "GET",
                    "https://www.bing.com/search",
                    params={"q": f"{term} reclamacao avaliacao"},
                )
                if isinstance(bing_html, str):
                    raw_items.extend(self._parse_bing(bing_html))

            return self._normalize_many(raw_items, term, limit)
        except Exception as exc:
            logger.warning("WebSearchCollector falhou: %s", exc)
            return []

    @staticmethod
    def _parse_duckduckgo(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        raw_items: list[dict[str, Any]] = []

        for card in soup.select("div.result, article, li.result"):
            title_node = card.select_one("a.result__a, h2 a, a[data-testid='result-title-a'], a[href]")
            snippet_node = card.select_one(".result__snippet, .result__body")
            if title_node is None:
                continue

            href = _clean_text(title_node.get("href"))
            raw_items.append(
                {
                    "title": _clean_text(title_node.get_text(" ", strip=True)),
                    "text": _clean_text(
                        f"{title_node.get_text(' ', strip=True)} {(snippet_node.get_text(' ', strip=True) if snippet_node else '')}"
                    ),
                    "snippet": _clean_text(snippet_node.get_text(" ", strip=True) if snippet_node else ""),
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


class MastodonCollector(BaseCollector):
    source_name = "mastodon"
    source_tier = "web"

    async def collect(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            term = _clean_text(query)
            if not term:
                return []

            base_url = str(getattr(settings, "SCRAPER_MASTODON_BASE_URL", "https://mastodon.social")).rstrip("/")
            path = str(getattr(settings, "SCRAPER_MASTODON_SEARCH_PATH", "/api/v2/search")).strip() or "/api/v2/search"
            if not path.startswith("/"):
                path = f"/{path}"

            headers: dict[str, str] = {}
            token = _clean_text(getattr(settings, "SCRAPER_MASTODON_ACCESS_TOKEN", ""))
            if token:
                headers["Authorization"] = f"Bearer {token}"

            payload = await self._request(
                "GET",
                f"{base_url}{path}",
                params={"q": term, "type": "statuses", "limit": 20},
                headers=headers,
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
