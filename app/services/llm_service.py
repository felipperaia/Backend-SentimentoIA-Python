import json
import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """Servico de IA com chamada HTTP para Ollama remoto."""

    REDACTED = "[redacted]"
    SENSITIVE_KEY_PATTERN = re.compile(
        r"password|senha|hash|token|api_key|secret|mfa|phone|cpf|cnpj|email",
        re.IGNORECASE,
    )

    @staticmethod
    def _base_url() -> str:
        base_url = str(settings.ollama_base_url or "").strip().rstrip("/")
        if not base_url:
            return ""
        if base_url.lower().endswith("/api"):
            return base_url
        return f"{base_url}/api"

    @staticmethod
    def _model() -> str:
        return str(settings.ollama_model or "").strip()

    @staticmethod
    def _timeout_seconds() -> int:
        try:
            return max(1, int(settings.ollama_timeout_seconds or 120))
        except (TypeError, ValueError):
            return 120

    @staticmethod
    def _headers() -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = str(settings.ollama_api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def ollama_configured() -> bool:
        return bool(LLMService._base_url() and LLMService._model())

    @staticmethod
    def gateway_configured() -> bool:
        # Compatibilidade com contratos antigos.
        return LLMService.ollama_configured()

    @staticmethod
    def _extract_chat_text(payload: dict[str, Any]) -> str:
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()

        response = payload.get("response")
        if isinstance(response, str) and response.strip():
            return response.strip()

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                nested_message = first.get("message")
                if isinstance(nested_message, dict):
                    content = nested_message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

        return ""

    @staticmethod
    async def _post_json(endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        base_url = LLMService._base_url()
        if not base_url:
            logger.warning("OLLAMA_BASE_URL nao configurada; resposta fallback sera usada")
            return None

        model = payload.get("model")
        if not model:
            logger.warning("OLLAMA_MODEL nao configurado; resposta fallback sera usada")
            return None

        url = f"{base_url}/{endpoint.lstrip('/')}"

        try:
            async with httpx.AsyncClient(timeout=LLMService._timeout_seconds()) as client:
                response = await client.post(url, headers=LLMService._headers(), json=payload)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning("Falha ao chamar Ollama em %s: %s", url, exc)
            return None

        if response.status_code >= 400:
            logger.warning("Ollama retornou HTTP %s em %s", response.status_code, url)
            return None

        try:
            data = response.json()
        except ValueError:
            logger.warning("Ollama retornou payload nao-JSON em %s", url)
            return None

        if not isinstance(data, dict):
            logger.warning("Ollama retornou payload invalido em %s", url)
            return None

        return data

    @staticmethod
    async def _get_json(endpoint: str) -> dict[str, Any] | None:
        base_url = LLMService._base_url()
        if not base_url:
            logger.warning("OLLAMA_BASE_URL nao configurada; verificacao de saude indisponivel")
            return None

        url = f"{base_url}/{endpoint.lstrip('/')}"

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(url, headers=LLMService._headers())
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning("Falha ao chamar Ollama em %s: %s", url, exc)
            return None

        if response.status_code >= 400:
            logger.warning("Ollama retornou HTTP %s em %s", response.status_code, url)
            return None

        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except ValueError:
            logger.warning("Ollama retornou payload nao-JSON em %s", url)
            return None

    @staticmethod
    def parse_json(response: str) -> dict[str, Any]:
        content = str(response or "").strip()
        if not content:
            return {}

        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                parsed = json.loads(content[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _parse_json(response: str) -> dict[str, Any]:
        return LLMService.parse_json(response)

    @staticmethod
    def normalize_analysis(raw: dict[str, Any]) -> dict[str, Any]:
        data = raw if isinstance(raw, dict) else {}
        return {
            "executive_summary": str(data.get("executive_summary") or "Analise indisponivel no momento."),
            "sentiment_overview": str(data.get("sentiment_overview") or "Resumo de sentimento indisponivel."),
            "priority": str(data.get("priority") or "medium").lower(),
            "risks": data.get("risks") if isinstance(data.get("risks"), list) else [],
            "opportunities": data.get("opportunities") if isinstance(data.get("opportunities"), list) else [],
            "recommended_actions": data.get("recommended_actions") if isinstance(data.get("recommended_actions"), list) else [],
            "decision_guidance": str(data.get("decision_guidance") or "Direcionamento indisponivel."),
            "trend": str(data.get("trend") or "stable").lower(),
            "source_references": data.get("source_references") if isinstance(data.get("source_references"), list) else [],
            "resolution": str(data.get("resolution") or "pending").lower(),
        }

    @staticmethod
    def _normalize_analysis(raw: dict[str, Any], reason_if_empty: str = "Analise indisponivel.") -> dict[str, Any]:
        normalized = LLMService.normalize_analysis(raw)
        if not normalized.get("executive_summary"):
            normalized["executive_summary"] = reason_if_empty
        return normalized

    @staticmethod
    def empty_analysis(reason: str = "Analise indisponivel no momento.", llm_unavailable: bool = False) -> dict[str, Any]:
        payload = LLMService.normalize_analysis(
            {
                "executive_summary": reason,
                "sentiment_overview": "indefinido",
                "decision_guidance": reason,
            }
        )
        payload["llm_unavailable"] = llm_unavailable
        return payload

    @staticmethod
    def snapshot_prompt(snapshot: dict[str, Any]) -> str:
        schema = {
            "executive_summary": "string",
            "sentiment_overview": "string",
            "priority": "high|medium|low",
            "risks": ["string"],
            "opportunities": ["string"],
            "recommended_actions": ["string"],
            "decision_guidance": "string",
            "trend": "improving|stable|worsening",
            "source_references": ["string"],
            "resolution": "pending|in_progress|resolved",
        }

        return (
            "Responda SOMENTE em JSON valido. "
            "Nao invente dados fora do snapshot e nao retorne markdown.\n\n"
            f"Formato obrigatorio:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"Snapshot:\n{json.dumps(snapshot, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _snapshot_prompt(snapshot: dict[str, Any]) -> str:
        return LLMService.snapshot_prompt(snapshot)

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        return bool(LLMService.SENSITIVE_KEY_PATTERN.search(str(key or "")))

    @staticmethod
    def sanitize_context(context: dict[str, Any], _depth: int = 0) -> dict[str, Any]:
        if not isinstance(context, dict):
            return {}
        if _depth > 5:
            return {}

        sanitized: dict[str, Any] = {}
        for key, value in context.items():
            if LLMService._is_sensitive_key(str(key)):
                continue

            if isinstance(value, dict):
                sanitized[str(key)] = LLMService.sanitize_context(value, _depth + 1)
            elif isinstance(value, list):
                new_items: list[Any] = []
                for item in value[:50]:
                    if isinstance(item, dict):
                        new_items.append(LLMService.sanitize_context(item, _depth + 1))
                    else:
                        new_items.append(item)
                sanitized[str(key)] = new_items
            else:
                sanitized[str(key)] = value

        return sanitized

    @staticmethod
    def _sanitize_context_for_llm(value: Any, parent_key: str = "") -> Any:
        del parent_key
        if isinstance(value, dict):
            return LLMService.sanitize_context(value)
        return value

    @staticmethod
    def _redact_text(value: Any) -> str:
        text = str(value or "")
        text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", LLMService.REDACTED, text)
        text = re.sub(r"\+?\d[\d\s().-]{7,}\d", LLMService.REDACTED, text)
        text = re.sub(r"eyJ[\w.-]{20,}", LLMService.REDACTED, text)
        return text

    @staticmethod
    def _redact_for_prompt(value: Any, parent_key: str = "", _depth: int = 0) -> Any:
        if _depth > 5:
            return LLMService.REDACTED
        if LLMService._is_sensitive_key(parent_key):
            return LLMService.REDACTED
        if isinstance(value, dict):
            return {
                str(key): LLMService._redact_for_prompt(item, str(key), _depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [LLMService._redact_for_prompt(item, parent_key, _depth + 1) for item in value[:50]]
        if isinstance(value, str):
            return LLMService._redact_text(value)
        return value

    @staticmethod
    async def call_ollama(prompt: str, format_json: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": LLMService._model(),
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        if format_json:
            payload["format"] = "json"

        response_payload = await LLMService._post_json("generate", payload)
        return response_payload or {}

    @staticmethod
    async def _call_ollama(prompt: str, model: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": str(model or LLMService._model()).strip(),
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }

        response_payload = await LLMService._post_json("generate", payload)
        if not response_payload:
            return {}

        return LLMService.parse_json(str(response_payload.get("response") or "{}"))

    @staticmethod
    async def call_ollama_chat(messages: list[dict[str, Any]]) -> str:
        payload: dict[str, Any] = {
            "model": LLMService._model(),
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.15},
        }
        response_payload = await LLMService._post_json("chat", payload)
        if not response_payload:
            return ""
        return LLMService._extract_chat_text(response_payload)

    @staticmethod
    async def _call_ollama_text(prompt: str, model: str | None = None) -> str:
        payload: dict[str, Any] = {
            "model": str(model or LLMService._model()).strip(),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.15},
        }

        response_payload = await LLMService._post_json("chat", payload)
        if not response_payload:
            return ""

        return LLMService._extract_chat_text(response_payload)

    @staticmethod
    async def analyze_snapshot(snapshot: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
        del user_id

        total_mentions = int(snapshot.get("total_mentions", snapshot.get("total_comments", 0)) or 0)
        if not snapshot or total_mentions <= 0:
            return LLMService.empty_analysis("Sem comentarios processados para gerar insight.")

        if not LLMService.ollama_configured():
            logger.warning("LLM nao configurada; retorno de insight vazio")
            return LLMService.empty_analysis(
                "Analise indisponivel no momento.",
                llm_unavailable=True,
            )

        safe_snapshot = LLMService.sanitize_context(snapshot)
        prompt = LLMService.snapshot_prompt(safe_snapshot)
        try:
            response_text = await LLMService._call_ollama_text(prompt, model=LLMService._model())
        except Exception as exc:
            logger.warning("Falha na chamada LLM durante analise de snapshot: %s", exc)
            return LLMService.empty_analysis(
                "Falha temporaria na LLM. Insight nao disponivel no momento.",
                llm_unavailable=True,
            )

        if not response_text:
            logger.warning("LLM indisponivel durante analise de snapshot; retorno fallback")
            return LLMService.empty_analysis(
                "Falha temporaria na LLM. Insight nao disponivel no momento.",
                llm_unavailable=True,
            )

        parsed = LLMService.parse_json(response_text)
        normalized = LLMService.normalize_analysis(parsed)
        normalized["llm_unavailable"] = False
        return normalized

    @staticmethod
    async def analyze_mentions(brand: str, mentions: list[dict[str, Any]]) -> dict[str, Any]:
        if not mentions:
            return LLMService.empty_analysis("Sem mencoes coletadas para analise.")

        sentiments: dict[str, int] = {}
        for mention in mentions:
            sentiment = str(mention.get("sentiment") or "neutral").lower()
            sentiments[sentiment] = sentiments.get(sentiment, 0) + 1

        max_samples = max(1, int(settings.llm_max_sample_mentions or 20))
        snapshot = {
            "brand": brand or "indefinida",
            "query": brand or "indefinida",
            "total_mentions": len(mentions),
            "total_comments": len(mentions),
            "sentiment_distribution": sentiments,
            "sample_mentions": [
                {
                    "source": mention.get("source"),
                    "text": str(mention.get("text") or "")[:500],
                    "criticality": mention.get("criticality"),
                }
                for mention in mentions[:max_samples]
            ],
        }
        return await LLMService.analyze_snapshot(snapshot=snapshot)

    @staticmethod
    async def answer_domain_chat(
        messages: list | None = None,
        authorized_context: dict | None = None,
        fail_on_unavailable: bool = False,
        **legacy_kwargs: Any,
    ) -> str:
        del fail_on_unavailable

        fallback = "Desculpe, o assistente esta temporariamente indisponivel."

        if not LLMService.ollama_configured():
            logger.warning("Chat LLM indisponivel por configuracao ausente")
            return fallback

        try:
            if legacy_kwargs:
                safe_context = LLMService._redact_for_prompt(authorized_context or {})
                safe_history = LLMService._redact_for_prompt(legacy_kwargs.get("history") or [])
                user_message = LLMService._redact_text(legacy_kwargs.get("user_message") or "")
                rendered_prompt = "\n\n".join(
                    [
                        str(legacy_kwargs.get("system_prompt") or "Sistema SentimentoIA"),
                        f"Locale: {legacy_kwargs.get('locale') or 'pt-BR'}",
                        f"Base de conhecimento:\n{LLMService._redact_text(legacy_kwargs.get('knowledge_base') or '')}",
                        "Intencoes permitidas: get_user_dashboard_summary, list_mentions, generate_insight, explain_dashboard",
                        "Contexto autorizado:\n"
                        f"{json.dumps(safe_context, ensure_ascii=False, default=str)}",
                        "Historico:\n"
                        f"{json.dumps(safe_history, ensure_ascii=False, default=str)}",
                        f"Mensagem do usuario:\n{user_message}",
                    ]
                )
                response = await LLMService._call_ollama_text(
                    rendered_prompt,
                    model=legacy_kwargs.get("model") or LLMService._model(),
                )
                return response if response else fallback

            safe_context = LLMService.sanitize_context(authorized_context or {})
            safe_messages: list[dict[str, str]] = []

            if safe_context:
                safe_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "DADOS AUTORIZADOS DO USUARIO (FILTRADOS):\n"
                            f"{json.dumps(safe_context, ensure_ascii=False, default=str)}"
                        ),
                    }
                )

            for message in messages or []:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "user").strip().lower()
                if role not in {"system", "user", "assistant"}:
                    role = "user"
                content = str(message.get("content") or "").strip()
                if not content:
                    continue
                safe_messages.append({"role": role, "content": content[:6000]})

            if not safe_messages:
                return fallback

            prompt_parts = [f"{item.get('role', 'user').upper()}:\n{item.get('content', '')}" for item in safe_messages]
            rendered_prompt = "\n\n".join(prompt_parts)
            response = await LLMService._call_ollama_text(
                rendered_prompt,
                model=LLMService._model(),
            )
            if not response:
                logger.warning("Chat LLM retornou vazio; fallback aplicado")
                return fallback
            return response
        except Exception as exc:
            logger.warning("Falha no chat Ollama; fallback aplicado: %s", exc)
            return fallback

    @staticmethod
    async def health_check() -> dict[str, Any]:
        configured = LLMService.ollama_configured()
        if not configured:
            return {
                "status": "error",
                "provider": "ollama",
                "configured": False,
                "available": False,
                "gateway_configured": False,
                "gateway_ok": False,
            }

        tags_payload = await LLMService._get_json("tags")
        available = isinstance(tags_payload, dict)

        return {
            "status": "ok" if available else "error",
            "provider": "ollama",
            "configured": True,
            "available": available,
            "gateway_configured": True,
            "gateway_ok": available,
        }

    @staticmethod
    async def healthcheck() -> dict[str, Any]:
        return await LLMService.health_check()

    @staticmethod
    async def validate_connection() -> None:
        health = await LLMService.health_check()
        if not health.get("configured"):
            logger.warning("OLLAMA_BASE_URL ou OLLAMA_MODEL nao configurados")
            return

        if not health.get("available"):
            logger.warning("Ollama indisponivel no endpoint configurado")
            return

        logger.info("Conexao com Ollama validada com sucesso")
