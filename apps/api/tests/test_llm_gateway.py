import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services.llm_service import LLMService


class _FakeResponse:
    def __init__(self, *, url: str, status_code: int = 200, payload: dict | None = None, text: str | None = None):
        self.url = url
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def _configure_gateway(
    monkeypatch,
    *,
    base_url: str = "https://gateway.example.com",
    api_key: str = "gateway-key",
) -> None:
    monkeypatch.setattr(settings, "LLM_GATEWAY_BASE_URL", base_url, raising=False)
    monkeypatch.setattr(settings, "LLM_GATEWAY_API_KEY", api_key, raising=False)
    monkeypatch.setattr(settings, "LLM_GATEWAY_TIMEOUT_SECONDS", 20, raising=False)


def _install_fake_async_client(monkeypatch, *, post_handler=None, get_handler=None):
    calls: list[dict] = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
            if post_handler:
                return post_handler(url, headers, json)
            return _FakeResponse(url=url, payload={"response": "{}"})

        async def get(self, url, headers=None):
            calls.append({"method": "GET", "url": url, "headers": headers})
            if get_handler:
                return get_handler(url, headers)
            return _FakeResponse(url=url, payload={"models": []})

    monkeypatch.setattr("app.services.llm_service.httpx.AsyncClient", _FakeAsyncClient)
    return calls


def test_analyze_snapshot_uses_gateway_generate(monkeypatch):
    _configure_gateway(monkeypatch)

    def _post_handler(url, headers, payload):
        del headers
        assert url.endswith("/api/generate")
        assert "model" not in payload
        return _FakeResponse(
            url=url,
            payload={
                "response": json.dumps(
                    {
                        "executive_summary": "Insight gerado via gateway",
                        "trend": "estavel",
                    }
                )
            },
        )

    calls = _install_fake_async_client(monkeypatch, post_handler=_post_handler)

    result = asyncio.run(
        LLMService.analyze_snapshot(
            {
                "total_comments": 3,
                "sample_mentions": [{"text": "comentario de teste"}],
            }
        )
    )

    assert calls
    assert calls[0]["url"].endswith("/api/generate")
    assert result["executive_summary"] == "Insight gerado via gateway"


def test_answer_domain_chat_uses_gateway_chat_and_redacts_sensitive_data(monkeypatch):
    _configure_gateway(monkeypatch)

    calls = _install_fake_async_client(
        monkeypatch,
        post_handler=lambda url, _headers, _payload: _FakeResponse(
            url=url,
            payload={"message": {"content": "Resposta segura do gateway"}},
        ),
    )

    response = asyncio.run(
        LLMService.answer_domain_chat(
            locale="pt-BR",
            system_prompt="Sistema SentimentoIA",
            knowledge_base="Base de conhecimento interna",
            authorized_context={
                "email": "usuario@example.com",
                "phone": "+55 11 98888-7777",
                "auth_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.zzzzzzzzzzzzzzzz.zzzzzzzzzzzzzzzz",
                "dashboard": {"total_mentions": 10},
            },
            history=[{"role": "user", "content": "meu email e usuario@example.com"}],
            user_message="Meu telefone e +55 11 98888-7777",
        )
    )

    assert response == "Resposta segura do gateway"
    assert calls
    assert calls[0]["url"].endswith("/api/chat")

    sent_prompt = calls[0]["json"]["messages"][0]["content"]
    assert "usuario@example.com" not in sent_prompt
    assert "+55 11 98888-7777" not in sent_prompt
    assert "eyJhbGciOiJIUzI1Ni" not in sent_prompt
    assert "[redacted" in sent_prompt
    assert "get_user_dashboard_summary" in sent_prompt
    assert "Intencoes permitidas" in sent_prompt


def test_healthcheck_uses_gateway_tags(monkeypatch):
    _configure_gateway(monkeypatch)
    calls = _install_fake_async_client(
        monkeypatch,
        get_handler=lambda url, _headers: _FakeResponse(url=url, status_code=200, payload={"models": []}),
    )

    payload = asyncio.run(LLMService.healthcheck())

    assert payload["gateway_configured"] is True
    assert payload["gateway_ok"] is True
    assert any(call["method"] == "GET" and call["url"].endswith("/api/tags") for call in calls)


def test_gateway_auth_error_401():
    response = _FakeResponse(url="https://gateway.example.com/api/chat", status_code=401, text="Unauthorized")
    with pytest.raises(RuntimeError, match="autenticacao falhou"):
        LLMService._handle_gateway_error(response)


def test_gateway_rate_limit_429():
    response = _FakeResponse(url="https://gateway.example.com/api/generate", status_code=429, text="Rate limited")
    with pytest.raises(RuntimeError, match="limite de requisicoes"):
        LLMService._handle_gateway_error(response)


def test_gateway_upstream_5xx():
    response = _FakeResponse(url="https://gateway.example.com/api/generate", status_code=503, text="Service unavailable")
    with pytest.raises(RuntimeError, match="erro no upstream"):
        LLMService._handle_gateway_error(response)


def test_analyze_snapshot_timeout_returns_safe_fallback(monkeypatch):
    _configure_gateway(monkeypatch)

    async def _raise_timeout(prompt: str, model: str | None = None):
        del prompt, model
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(LLMService, "_call_ollama", staticmethod(_raise_timeout))

    result = asyncio.run(LLMService.analyze_snapshot({"total_comments": 2, "sample_mentions": []}))

    assert "Falha temporaria na LLM" in result["executive_summary"]


def test_analyze_snapshot_auth_error_returns_safe_fallback(monkeypatch):
    _configure_gateway(monkeypatch)

    async def _raise_auth(prompt: str, model: str | None = None):
        del prompt, model
        raise RuntimeError("Gateway LLM: autenticacao falhou (401)")

    monkeypatch.setattr(LLMService, "_call_ollama", staticmethod(_raise_auth))

    result = asyncio.run(LLMService.analyze_snapshot({"total_comments": 1}))

    assert "Falha temporaria na LLM" in result["executive_summary"]
