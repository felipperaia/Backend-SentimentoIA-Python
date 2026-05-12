from uuid import uuid4

from app.services.scraper_service import ScraperService


def test_scrape_endpoint_returns_grouped_results(client, monkeypatch) -> None:
    def fake_scrape(query: str, sources: list[str], limit_per_source: int | None = None):
        del query
        return {
            "query": "SentimentoIA",
            "sources": ["reclameaqui", "reddit", "mastodon"],
            "limit_per_source": limit_per_source or 5,
            "total": 2,
            "results": {
                "reclameaqui": [
                    {
                        "source": "reclameaqui",
                        "title": "Reclamação 1",
                        "url": "https://www.reclameaqui.com.br/reclamacao/123",
                        "snippet": "Trecho 1",
                        "author": None,
                        "published_at": None,
                    }
                ],
                "reddit": [
                    {
                        "source": "reddit",
                        "title": "Post 1",
                        "url": "https://old.reddit.com/r/test/comments/1",
                        "snippet": "Trecho 2",
                        "author": "user1",
                        "published_at": None,
                    }
                ],
                "mastodon": [],
            },
            "errors": [
                {
                    "source": "mastodon",
                    "error": "Fonte indisponivel no momento",
                }
            ],
        }

    monkeypatch.setattr(ScraperService, "scrape", staticmethod(fake_scrape))

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
            "sources": ["reclameaqui", "reddit", "mastodon"],
            "limit_per_source": 3,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2
    assert len(payload["results"]["reclameaqui"]) == 1
    assert len(payload["results"]["reddit"]) == 1
    assert payload["errors"][0]["source"] == "mastodon"
