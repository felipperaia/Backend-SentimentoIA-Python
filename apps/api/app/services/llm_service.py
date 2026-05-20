import json
import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """Servico de IA com chamada direta ao Ollama."""

    REDACTED = "[redacted]"
    SENSITIVE_KEY_PATTERN = re.compile(
        r"password|senha|hash|token|api_key|secret|mfa|phone|cpf|cnpj|email",
        re.IGNORECASE,
    )

    @staticmethod
    def _settings_str(name: str) -> str:
        value = getattr(settings, name, "")
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _normalize_api_base_url(value: str) -> str:
        base_url = str(value or "").strip().rstrip("/")
        if not base_url:
            return ""
        if base_url.lower().endswith("/api"):
            return base_url
        return f"{base_url}/api"

    @staticmethod
    def _effective_base_url() -> str:
        direct_url = (
            LLMService._settings_str("OLLAMA_EFFECTIVE_URL")
            or LLMService._settings_str("OLLAMA_BASE_URL")
        )
        return LLMService._normalize_api_base_url(direct_url)

    @staticmethod
    def _effective_api_key() -> str:
        return (
            LLMService._settings_str("OLLAMA_API_KEY")
            or LLMService._settings_str("LLM_GATEWAY_EFFECTIVE_API_KEY")
        )

    @staticmethod
    def _effective_model() -> str:
        return (
            LLMService._settings_str("LLM_MODEL_EFFECTIVE")
            or LLMService._settings_str("OLLAMA_MODEL")
        )

    @staticmethod
    def _gateway_base_url() -> str:
        # Compatibilidade com chamadas antigas; gateway foi removido.
        return ""

    @staticmethod
    def _gateway_mode() -> bool:
        # Compatibilidade com chamadas antigas; gateway foi removido.
        return False

    @staticmethod
    def ollama_configured() -> bool:
        return bool(LLMService._effective_base_url() and LLMService._effective_model())

    @staticmethod
    def gateway_configured() -> bool:
        # Compatibilidade com chamadas antigas.
        return LLMService.ollama_configured()

    @staticmethod
    def _ollama_headers() -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = LLMService._effective_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _gateway_headers() -> dict[str, str]:
        # Compatibilidade com chamadas antigas.
        return LLMService._ollama_headers()

    @staticmethod
    def _llm_timeout_seconds() -> int:
        value = getattr(settings, "OLLAMA_TIMEOUT_SECONDS", 60)
        try:
            return max(1, int(value or 60))
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _build_ollama_url(endpoint: str) -> str:
        base_url = LLMService._effective_base_url()
        if not base_url:
            raise RuntimeError("OLLAMA_BASE_URL nao configurada")
        return f"{base_url}/{endpoint.lstrip('/')}"

    @staticmethod
    def _build_gateway_url(endpoint: str) -> str:
        # Compatibilidade com chamadas antigas.
        return LLMService._build_ollama_url(endpoint)

    @staticmethod
    def _handle_ollama_error(resp: httpx.Response) -> None:
        status = resp.status_code
        if status < 400:
            return

        body_preview = resp.text[:300] if resp.text else "(sem corpo)"

        if status == 401:
            logger.error("Ollama 401 Unauthorized. Verifique OLLAMA_API_KEY.")
            raise RuntimeError("Ollama: autenticacao falhou (401). Verifique OLLAMA_API_KEY.")
        if status == 404:
            logger.error("Ollama 404 Not Found. URL: %s", resp.url)
            raise RuntimeError(f"Ollama: endpoint nao encontrado (404). URL: {resp.url}")
        if status == 429:
            logger.warning("Ollama 429 Rate Limited. Aguarde antes de tentar novamente.")
            raise RuntimeError("Ollama: limite de requisicoes atingido (429). Tente novamente em instantes.")
        if status >= 500:
            logger.error("Ollama %d Server Error. Body: %s", status, body_preview)
            raise RuntimeError(f"Ollama: erro no upstream ({status}). Servico pode estar instavel.")

        logger.error("Ollama HTTP %d. Body: %s", status, body_preview)
        raise RuntimeError(f"Ollama: erro HTTP {status}.")

    @staticmethod
    def _handle_gateway_error(resp: httpx.Response) -> None:
        # Compatibilidade com chamadas antigas.
        LLMService._handle_ollama_error(resp)

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError("Ollama retornou payload nao-JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Ollama retornou payload invalido")
        return payload

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
                maybe_message = first.get("message")
                if isinstance(maybe_message, dict):
                    content = maybe_message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

        return ""

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
            raise ValueError("LLM nao retornou JSON valido")

    @staticmethod
    def _parse_json(response: str) -> dict[str, Any]:
        # Compatibilidade com chamadas antigas.
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
        # Compatibilidade com chamadas antigas.
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
                for item in value:
                    if isinstance(item, dict):
                        new_items.append(LLMService.sanitize_context(item, _depth + 1))
                    elif isinstance(item, list):
                        new_items.append(item[:50])
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
        model = LLMService._effective_model()
        if not model:
            raise RuntimeError("OLLAMA_MODEL nao configurado")

        url = LLMService._build_ollama_url("generate")
        timeout = LLMService._llm_timeout_seconds()

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        if format_json:
            payload["format"] = "json"

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=LLMService._ollama_headers(), json=payload)
            LLMService._handle_ollama_error(resp)
            return LLMService._safe_json(resp)

    @staticmethod
    async def _call_ollama(prompt: str, model: str | None = None) -> dict[str, Any]:
        selected_model = str(model or LLMService._effective_model()).strip()
        if not selected_model:
            raise RuntimeError("OLLAMA_MODEL nao configurado")

        url = LLMService._build_ollama_url("generate")
        timeout = LLMService._llm_timeout_seconds()

        payload: dict[str, Any] = {
            "model": selected_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=LLMService._ollama_headers(), json=payload)
            LLMService._handle_ollama_error(resp)
            response_payload = LLMService._safe_json(resp)
            return LLMService.parse_json(str(response_payload.get("response") or "{}"))

    @staticmethod
    async def call_ollama_chat(messages: list[dict[str, Any]]) -> str:
        model = LLMService._effective_model()
        if not model:
            raise RuntimeError("OLLAMA_MODEL nao configurado")

        url = LLMService._build_ollama_url("chat")
        timeout = LLMService._llm_timeout_seconds()

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.15},
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=LLMService._ollama_headers(), json=payload)
            LLMService._handle_ollama_error(resp)
            response_payload = LLMService._safe_json(resp)
            return LLMService._extract_chat_text(response_payload)

    @staticmethod
    async def _call_ollama_text(prompt: str, model: str | None = None) -> str:
        selected_model = str(model or LLMService._effective_model()).strip()
        if not selected_model:
            raise RuntimeError("OLLAMA_MODEL nao configurado")

        url = LLMService._build_ollama_url("chat")
        timeout = LLMService._llm_timeout_seconds()

        payload = {
            "model": selected_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.15},
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=LLMService._ollama_headers(), json=payload)
            LLMService._handle_ollama_error(resp)
            return LLMService._extract_chat_text(LLMService._safe_json(resp))

    @staticmethod
    async def analyze_snapshot(snapshot: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
        del user_id

        total_mentions = int(snapshot.get("total_mentions", snapshot.get("total_comments", 0)) or 0)
        if not snapshot or total_mentions <= 0:
            return LLMService.empty_analysis("Sem comentarios processados para gerar insight.")

        if not LLMService.ollama_configured():
            return LLMService.empty_analysis(
                "Analise indisponivel no momento.",
                llm_unavailable=True,
            )

        safe_snapshot = LLMService.sanitize_context(snapshot)
        prompt = LLMService.snapshot_prompt(safe_snapshot)

        try:
            parsed = await LLMService._call_ollama(prompt, model=LLMService._effective_model())
            if not isinstance(parsed, dict):
                parsed = {}

            normalized = LLMService.normalize_analysis(parsed)
            normalized["llm_unavailable"] = False
            return normalized
        except (httpx.HTTPError, httpx.TimeoutException, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            logger.exception("Falha ao gerar analise de snapshot: %s", exc)
            return LLMService.empty_analysis(
                "Falha temporaria na LLM. Insight nao disponivel no momento.",
                llm_unavailable=True,
            )

    @staticmethod
    async def analyze_mentions(brand: str, mentions: list[dict[str, Any]]) -> dict[str, Any]:
        if not mentions:
            return LLMService.empty_analysis("Sem mencoes coletadas para analise.")

        sentiments: dict[str, int] = {}
        for mention in mentions:
            sentiment = str(mention.get("sentiment") or "neutral").lower()
            sentiments[sentiment] = sentiments.get(sentiment, 0) + 1

        max_samples = max(1, int(getattr(settings, "LLM_MAX_SAMPLE_MENTIONS", 40) or 40))
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
        **legacy_kwargs: Any,
    ) -> str:
        fallback = "Desculpe, o assistente esta temporariamente indisponivel."

        if not LLMService.ollama_configured():
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
                    model=legacy_kwargs.get("model") or LLMService._effective_model(),
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
                model=LLMService._effective_model(),
            )
            return response if response else fallback
        except (httpx.HTTPError, httpx.TimeoutException, RuntimeError, ValueError) as exc:
            logger.exception("Falha no chat Ollama: %s", exc)
            return fallback

    @staticmethod
    async def health_check() -> dict[str, Any]:
        base_url = LLMService._effective_base_url()
        configured = bool(base_url and LLMService._effective_model())

        if not configured:
            return {
                "status": "error",
                "provider": "ollama",
                "configured": False,
                "available": False,
                "gateway_configured": False,
                "gateway_ok": False,
            }

        tags_url = f"{base_url}/tags"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(tags_url, headers=LLMService._ollama_headers())
                if resp.status_code < 400:
                    return {
                        "status": "ok",
                        "provider": "ollama",
                        "configured": True,
                        "available": True,
                        "gateway_configured": True,
                        "gateway_ok": True,
                    }
                return {
                    "status": "error",
                    "provider": "ollama",
                    "configured": True,
                    "available": False,
                    "gateway_configured": True,
                    "gateway_ok": False,
                }
        except (httpx.HTTPError, httpx.TimeoutException, RuntimeError) as exc:
            del exc
            return {
                "status": "error",
                "provider": "ollama",
                "configured": True,
                "available": False,
                "gateway_configured": True,
                "gateway_ok": False,
            }

    @staticmethod
    async def healthcheck() -> dict[str, Any]:
        # Compatibilidade com chamadas antigas.
        return await LLMService.health_check()

    @staticmethod
    async def validate_connection() -> None:
        base_url = LLMService._effective_base_url()
        if not base_url:
            logger.warning("OLLAMA_BASE_URL nao configurada.")
            return

        tags_url = f"{base_url}/tags"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(tags_url, headers=LLMService._ollama_headers())
                if resp.status_code >= 400:
                    logger.warning(
                        "Ollama nao acessivel em %s. Verifique OLLAMA_BASE_URL no .env. HTTP %s",
                        tags_url,
                        resp.status_code,
                    )
                else:
                    logger.info("Conexao com Ollama validada com sucesso")
        except (httpx.HTTPError, httpx.TimeoutException, RuntimeError) as exc:
            logger.warning(
                "Ollama nao acessivel em %s. Verifique OLLAMA_BASE_URL no .env. Detalhes: %s",
                tags_url,
                exc,
            )
