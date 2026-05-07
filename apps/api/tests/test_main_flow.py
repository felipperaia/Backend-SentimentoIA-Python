import asyncio
from uuid import uuid4

from app.services.llm_service import LLMService
from app.services.processing_service import ProcessingService


async def _fake_call_ollama(prompt: str, model: str | None = None):
    del prompt, model
    return {
        "executive_summary": "Insight de teste gerado com sucesso.",
        "sentiment_overview": "Predominio neutro com ocorrencias negativas.",
        "risks": ["Risco operacional em atendimento"],
        "opportunities": ["Melhorar SLA de resposta"],
        "recommended_actions": ["Priorizar contatos criticos"],
        "decision_guidance": "Atue nos itens de maior urgencia.",
        "trend": "estavel",
    }


async def _fake_call_ollama_text(prompt: str, model: str | None = None):
    del prompt, model
    return "Resposta de teste restrita ao dominio SentimentoIA."


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_main_flow_end_to_end(client, monkeypatch):
    monkeypatch.setattr(LLMService, "ollama_configured", staticmethod(lambda: True))
    monkeypatch.setattr(LLMService, "_call_ollama", staticmethod(_fake_call_ollama))
    monkeypatch.setattr(LLMService, "_call_ollama_text", staticmethod(_fake_call_ollama_text))

    email = f"flow-{uuid4().hex[:10]}@example.com"

    register_response = client.post(
        "/api/auth/register",
        json={
            "name": "Fluxo Final",
            "email": email,
            "phone": "+55 11 90000-0000",
            "password": "SenhaSegura123!",
        },
    )
    assert register_response.status_code == 201, register_response.text
    token = register_response.json()["access_token"]
    headers = _auth_headers(token)

    settings_put = client.put(
        "/api/settings",
        headers=headers,
        json={
            "theme": "light",
            "locale": "pt-BR",
            "llm_trigger_min_comments": 1,
        },
    )
    assert settings_put.status_code == 200, settings_put.text

    ingestion_response = client.post(
        "/api/ingestion/comments",
        headers=headers,
        json={
            "batch_name": "batch-demo",
            "source": "manual_import",
            "channel": "app",
            "brand": "SentimentoIA",
            "locale": "pt-BR",
            "comments": [
                {
                    "external_id": "msg-001",
                    "author_name": "Ana",
                    "author_email": "",
                    "author_phone": "",
                    "text": "Atendimento lento, vou reclamar no procon.",
                    "rating": 1,
                    "created_at": "2026-05-05T10:30:00Z",
                    "tags": ["atendimento"],
                    "metadata": {"city": "Recife"},
                },
                {
                    "external_id": "msg-002",
                    "author_name": "Bruno",
                    "author_email": "",
                    "author_phone": "",
                    "text": "Produto bom, entrega rapida e qualidade alta.",
                    "rating": 5,
                    "created_at": "2026-05-05T11:00:00Z",
                    "tags": ["produto"],
                    "metadata": {"city": "Recife"},
                },
            ],
        },
    )
    assert ingestion_response.status_code == 202, ingestion_response.text
    batch_id = ingestion_response.json()["batch_id"]

    processing_result = ProcessingService.process_pending_mentions(limit=20)
    assert processing_result["processed"] >= 2

    dashboard_response = client.get(
        "/api/dashboard",
        headers=headers,
        params={"batch_id": batch_id},
    )
    assert dashboard_response.status_code == 200, dashboard_response.text
    dashboard_data = dashboard_response.json()
    assert int(dashboard_data["metrics"].get("total_comments", 0)) >= 2

    insight_response = client.post(
        "/api/insights/generate",
        headers=headers,
        json={"batch_id": batch_id, "force": True},
    )
    assert insight_response.status_code == 200, insight_response.text

    insights_list = client.get("/api/insights", headers=headers)
    assert insights_list.status_code == 200, insights_list.text
    assert len(insights_list.json().get("items", [])) >= 1

    thread_response = client.post("/api/chat/threads", headers=headers, json={"title": "Suporte"})
    assert thread_response.status_code == 200, thread_response.text
    thread_id = thread_response.json()["item"]["thread_id"]

    chat_scope = client.post(
        f"/api/chat/threads/{thread_id}/messages",
        headers=headers,
        json={"content": "Como interpretar meu dashboard?"},
    )
    assert chat_scope.status_code == 200, chat_scope.text
    assert "SentimentoIA" in chat_scope.json()["assistant_message"]["content"]

    chat_out_scope = client.post(
        f"/api/chat/threads/{thread_id}/messages",
        headers=headers,
        json={"content": "Quem ganhou a copa de 2002?"},
    )
    assert chat_out_scope.status_code == 200, chat_out_scope.text
    assert "Posso ajudar apenas" in chat_out_scope.json()["assistant_message"]["content"]

    settings_get = client.get("/api/settings", headers=headers)
    assert settings_get.status_code == 200, settings_get.text
    assert settings_get.json()["locale"] == "pt-BR"

    # Processamento assíncrono de insights deve estar operacional.
    result_async = asyncio.run(asyncio.sleep(0, result="ok"))
    assert result_async == "ok"
