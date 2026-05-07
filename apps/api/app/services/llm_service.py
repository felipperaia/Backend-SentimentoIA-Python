import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """Servico de IA com foco oficial em Ollama (local/cloud)."""

    @staticmethod
    def ollama_configured() -> bool:
        return bool(settings.OLLAMA_EFFECTIVE_URL and settings.OLLAMA_MODEL)

    @staticmethod
    def _ollama_headers() -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.OLLAMA_EFFECTIVE_MODE == "cloud" and settings.OLLAMA_API_KEY.strip():
            headers["Authorization"] = f"Bearer {settings.OLLAMA_API_KEY.strip()}"
        return headers

    @staticmethod
    async def _call_ollama(prompt: str, model: str | None = None) -> dict[str, Any]:
        base_url = settings.OLLAMA_EFFECTIVE_URL.rstrip("/")
        if not base_url:
            raise RuntimeError("OLLAMA_EFFECTIVE_URL nao configurada")

        payload = {
            "model": model or settings.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2,
            },
        }

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{base_url}/api/generate",
                headers=LLMService._ollama_headers(),
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:500]}")
            content = resp.json().get("response", "")
            return LLMService._parse_json(content)

    @staticmethod
    async def _call_ollama_text(prompt: str, model: str | None = None) -> str:
        base_url = settings.OLLAMA_EFFECTIVE_URL.rstrip("/")
        if not base_url:
            raise RuntimeError("OLLAMA_EFFECTIVE_URL nao configurada")

        payload = {
            "model": model or settings.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.15,
            },
        }

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{base_url}/api/generate",
                headers=LLMService._ollama_headers(),
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:500]}")
            return str(resp.json().get("response", "")).strip()

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Extrai JSON mesmo se o modelo devolver texto ao redor."""
        content = (content or "").strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start : end + 1])
            raise ValueError("LLM nao retornou JSON valido") from exc

    @staticmethod
    def empty_analysis(reason: str) -> dict[str, Any]:
        return {
            "executive_summary": reason,
            "sentiment_overview": "indefinido",
            "risks": [],
            "opportunities": [],
            "recommended_actions": [],
            "decision_guidance": reason,
            "trend": "indefinido",
        }

    @staticmethod
    def _normalize_analysis(data: dict[str, Any], reason_if_empty: str = "Analise vazia") -> dict[str, Any]:
        base = LLMService.empty_analysis(reason_if_empty)
        base.update({
            "executive_summary": str(data.get("executive_summary") or base["executive_summary"]),
            "sentiment_overview": str(data.get("sentiment_overview") or base["sentiment_overview"]),
            "decision_guidance": str(data.get("decision_guidance") or base["decision_guidance"]),
            "trend": str(data.get("trend") or base["trend"]),
            "risks": data.get("risks") if isinstance(data.get("risks"), list) else [],
            "opportunities": data.get("opportunities") if isinstance(data.get("opportunities"), list) else [],
            "recommended_actions": data.get("recommended_actions")
            if isinstance(data.get("recommended_actions"), list)
            else [],
        })
        return base

    @staticmethod
    def _snapshot_prompt(snapshot: dict[str, Any]) -> str:
        return (
            "Use somente os dados fornecidos. Nao invente fatos. "
            "Seu foco e orientar tomada de decisao executiva. "
            "Retorne SOMENTE JSON valido com as chaves: "
            "executive_summary, sentiment_overview, risks, opportunities, "
            "recommended_actions, decision_guidance, trend.\n\n"
            f"Contexto:\n{json.dumps(snapshot, ensure_ascii=False)}"
        )

    @staticmethod
    async def analyze_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        if not snapshot or int(snapshot.get("total_comments", 0)) <= 0:
            return LLMService.empty_analysis("Sem comentarios processados para gerar insight.")

        if not LLMService.ollama_configured():
            return LLMService.empty_analysis("Ollama nao configurado. Verifique OLLAMA_MODE e URLs.")

        try:
            raw = await LLMService._call_ollama(LLMService._snapshot_prompt(snapshot))
            return LLMService._normalize_analysis(raw, reason_if_empty="Resposta LLM incompleta")
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.error("Falha no Ollama (modo=%s): %s", settings.OLLAMA_EFFECTIVE_MODE, exc)
            return LLMService.empty_analysis("Falha temporaria na LLM. Insight nao disponivel no momento.")

    @staticmethod
    async def analyze_mentions(brand: str, mentions: list[dict[str, Any]]) -> dict[str, Any]:
        """Compatibilidade com o fluxo legado de busca."""
        if not mentions:
            return LLMService.empty_analysis("Sem mencoes coletadas para analise.")

        sample = []
        sentiments: dict[str, int] = {}
        for mention in mentions[: max(1, int(settings.LLM_MAX_SAMPLE_MENTIONS))]:
            sentiment = str(mention.get("sentiment") or "neutro")
            sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
            sample.append(
                {
                    "source": mention.get("source"),
                    "rating": mention.get("rating"),
                    "text": (mention.get("text") or "")[:500],
                    "criticality": mention.get("criticality"),
                }
            )

        snapshot = {
            "batch_id": None,
            "brand": brand or "indefinida",
            "period": "legado",
            "total_comments": len(mentions),
            "sentiment_distribution": sentiments,
            "critical_mentions": len([m for m in mentions if m.get("criticality") == "alta"]),
            "average_urgency": 0,
            "top_aspects": [],
            "top_critical_terms": [],
            "sample_mentions": sample,
        }
        return await LLMService.analyze_snapshot(snapshot)

    @staticmethod
    async def answer_domain_chat(
        *,
        locale: str,
        system_prompt: str,
        knowledge_base: str,
        authorized_context: dict[str, Any],
        history: list[dict[str, str]],
        user_message: str,
    ) -> str:
        fallback = (
            "No momento nao consegui gerar a resposta. Tente novamente em instantes."
            if locale == "pt-BR"
            else "I could not generate a response right now. Please try again shortly."
        )

        if not LLMService.ollama_configured():
            return fallback

        history_lines = []
        for item in history[-10:]:
            role = str(item.get("role") or "user").upper()
            content = str(item.get("content") or "")[:800]
            history_lines.append(f"{role}: {content}")

        prompt = (
            f"{system_prompt}\n\n"
            "# Base de conhecimento\n"
            f"{knowledge_base}\n\n"
            "# Contexto autorizado do usuario (JSON)\n"
            f"{json.dumps(authorized_context, ensure_ascii=False)}\n\n"
            "# Historico recente\n"
            f"{chr(10).join(history_lines) if history_lines else '(sem historico)'}\n\n"
            f"# Idioma alvo\n{locale}\n\n"
            f"# Pergunta do usuario\n{user_message}\n\n"
            "Responda de forma objetiva, pratica e estritamente dentro do dominio SentimentoIA."
        )

        try:
            response = await LLMService._call_ollama_text(prompt)
            return response[:2500] if response else fallback
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.error("Falha no chat Ollama (modo=%s): %s", settings.OLLAMA_EFFECTIVE_MODE, exc)
            return fallback

    @staticmethod
    async def healthcheck() -> dict[str, Any]:
        """Diagnostico simples para /api/status/integrations."""
        base_url = settings.OLLAMA_EFFECTIVE_URL.rstrip("/")
        result = {
            "provider": settings.LLM_PROVIDER,
            "ollama_mode": settings.OLLAMA_EFFECTIVE_MODE,
            "ollama_configured": LLMService.ollama_configured(),
            "ollama_model": settings.OLLAMA_MODEL,
            "ollama_url": base_url,
        }

        if LLMService.ollama_configured():
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(f"{base_url}/api/tags", headers=LLMService._ollama_headers())
                    result["ollama_ok"] = resp.status_code < 400
            except (httpx.HTTPError, RuntimeError) as exc:
                result["ollama_ok"] = False
                result["ollama_error"] = str(exc)

        return result
