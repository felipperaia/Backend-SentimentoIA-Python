import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock

from app.models import SearchRequest
from app.services.collector_orchestrator import CollectorOrchestrator
from app.services.scraper import (
    AppStoreCollector,
    GlassdoorCollector,
    PlayStoreCollector,
    ReclameAquiCollector,
    RedditCollector,
    TrustpilotCollector,
    YouTubeCollector,
)
from app.services.scraper_service import ScraperService


def _sample_mention(source: str) -> dict:
    return {
        "text": f"Texto {source}",
        "source": source,
        "url": f"https://example.com/{source}",
        "created_at": "2026-05-18T12:00:00+00:00",
        "score": 0.0,
        "sentiment": "neutral",
        "source_tier": "A",
    }


def test_orchestrator_continua_quando_uma_fonte_falha(monkeypatch) -> None:
    monkeypatch.setattr("app.services.collector_orchestrator.random.uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("app.services.collector_orchestrator.asyncio.sleep", AsyncMock(return_value=None))

    monkeypatch.setattr(RedditCollector, "collect", AsyncMock(return_value=[_sample_mention("reddit")]))
    monkeypatch.setattr(YouTubeCollector, "collect", AsyncMock(side_effect=RuntimeError("youtube-down")))
    monkeypatch.setattr(AppStoreCollector, "collect", AsyncMock(return_value=[_sample_mention("appstore")]))
    monkeypatch.setattr(PlayStoreCollector, "collect", AsyncMock(return_value=[]))
    monkeypatch.setattr(GlassdoorCollector, "collect", AsyncMock(return_value=[]))
    monkeypatch.setattr(TrustpilotCollector, "collect", AsyncMock(return_value=[]))
    monkeypatch.setattr(ReclameAquiCollector, "collect", AsyncMock(return_value=[]))

    orchestrator = CollectorOrchestrator(
        active_sources=["reddit", "youtube", "appstore", "playstore", "glassdoor", "trustpilot", "reclameaqui"]
    )
    grouped = asyncio.run(
        orchestrator.gather_all(
            query="SentimentoIA",
            limit=5,
            sources=["reddit", "youtube", "appstore", "playstore", "glassdoor", "trustpilot", "reclameaqui"],
        )
    )

    assert len(grouped["reddit"]) == 1
    assert len(grouped["appstore"]) == 1
    assert grouped["youtube"] == []
    assert any(error["source"] == "youtube" for error in orchestrator.last_errors)


def test_youtube_collector_sem_html_retorna_vazio(monkeypatch) -> None:
    collector = YouTubeCollector()
    monkeypatch.setattr(collector, "_request", AsyncMock(return_value=None))

    mentions = asyncio.run(collector.collect(query="SentimentoIA", limit=5))
    assert mentions == []


def test_reddit_collector_usa_json_publico(monkeypatch) -> None:
    collector = RedditCollector()
    monkeypatch.setattr(
        collector,
        "_request",
        AsyncMock(
            return_value=(
                {
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "title": "SentimentoIA reclama",
                                    "selftext": "SentimentoIA com instabilidade",
                                    "permalink": "/r/test/comments/abc/sentimentoia/",
                                    "author": "user1",
                                    "created_utc": 1716200000,
                                    "score": 10,
                                }
                            }
                        ]
                    }
                },
                {"X-Ratelimit-Remaining": "59"},
            )
        ),
    )

    mentions = asyncio.run(collector.collect(query="SentimentoIA", limit=5))
    assert len(mentions) == 1
    assert mentions[0]["source"] == "reddit"


def test_contrato_post_scrape_permanece_estavel(client, monkeypatch) -> None:
    monkeypatch.setattr(
        ScraperService,
        "scrape_async",
        AsyncMock(
            return_value={
                "query": "SentimentoIA",
                "sources": ["reddit", "youtube"],
                "limit_per_source": 3,
                "total": 2,
                "results": {
                    "reddit": [{"source": "reddit", "title": "Post", "snippet": "Trecho", "url": "https://reddit.com/x"}],
                    "youtube": [{"source": "youtube", "title": "Video", "snippet": "Comentario", "url": "https://youtube.com/x"}],
                },
                "errors": [],
                "metadata": {"max_total_items": 50},
            }
        ),
    )

    email = f"scrape-{uuid4().hex[:10]}@example.com"
    register_response = client.post(
        "/api/auth/register",
        json={
            "name": "Usuario Scrape",
            "email": email,
            "phone": "+55 11 97777-0000",
            "password": "SenhaSegura123!",
        },
    )
    assert register_response.status_code == 201, register_response.text

    token = register_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/scrape",
        headers=headers,
        json={
            "query": "SentimentoIA",
            "sources": ["reddit", "youtube"],
            "limit_per_source": 3,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) >= {"query", "sources", "limit_per_source", "total", "results", "errors", "metadata"}
    assert payload["total"] == 2
    assert isinstance(payload["results"], dict)


def test_search_request_aceita_query_como_alias() -> None:
    payload = SearchRequest.model_validate({"query": "SentimentoIA", "limit": 3})
    assert payload.brand_name == "SentimentoIA"
    assert payload.limit == 3
