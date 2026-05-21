from uuid import uuid4


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_generate_insight_threshold_not_met_returns_structured_response(client):
    email = f"insight-threshold-{uuid4().hex[:10]}@example.com"

    register_response = client.post(
        "/api/auth/register",
        json={
            "name": "Usuario Insight",
            "email": email,
            "phone": "+55 11 95555-0000",
            "password": "SenhaSegura123!",
        },
    )
    assert register_response.status_code == 201, register_response.text

    token = register_response.json()["access_token"]
    headers = _auth_headers(token)

    ingestion_response = client.post(
        "/api/ingestion/comments",
        headers=headers,
        json={
            "batch_name": "batch-threshold",
            "source": "manual_import",
            "channel": "app",
            "brand": "SentimentoIA",
            "locale": "pt-BR",
            "comments": [
                {
                    "external_id": "msg-threshold-001",
                    "author_name": "Ana",
                    "author_email": "",
                    "author_phone": "",
                    "text": "Demora no atendimento, aguardando resposta.",
                    "rating": 2,
                    "created_at": "2026-05-10T10:30:00Z",
                    "tags": ["atendimento"],
                    "metadata": {"city": "Recife"},
                }
            ],
        },
    )
    assert ingestion_response.status_code == 202, ingestion_response.text

    batch_id = ingestion_response.json()["batch_id"]

    insight_response = client.post(
        "/api/insights/generate",
        headers=headers,
        json={"batch_id": batch_id, "force": False},
    )

    assert insight_response.status_code == 400, insight_response.text
    payload = insight_response.json()

    assert payload.get("ok") is False
    assert payload.get("code") == "threshold_not_met"
    assert payload.get("expected_state") is True
    assert isinstance(payload.get("meta"), dict)
    assert int(payload["meta"].get("threshold", 0)) >= 1
    assert int(payload["meta"].get("processed_count", -1)) >= 0
    assert isinstance(payload["meta"].get("actionable_message"), str)
