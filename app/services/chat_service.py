import json
from pathlib import Path
from typing import Any
from uuid import uuid4
import re

from app.database import get_db
from app.services.controlled_context_service import build_authorized_context
from app.services.llm_service import LLMService
from app.services.normalization_service import utcnow
from app.services.search_service import SearchService

PROMPTS_DIR = Path(__file__).resolve().parents[4] / "packages" / "prompts"
DB_UNAVAILABLE_ERROR = "Banco de dados indisponivel"
DEFAULT_THREAD_TITLE = "Nova conversa"
OUT_OF_SCOPE_KEYWORDS = [
    "futebol",
    "copa",
    "esporte",
    "receita",
    "culinaria",
    "culinária",
    "politica",
    "política",
    "presidente",
    "noticia",
    "notícia",
    "clima",
    "tempo",
    "bitcoin",
    "crypto",
    "como programar",
    "codigo",
    "código",
    "python tutorial",
]
IN_SCOPE_KEYWORDS = [
    "sentimento",
    "sentiment",
    "reputacao",
    "reputação",
    "marca",
    "dashboard",
    "insight",
    "insights",
    "mencoes",
    "menções",
    "alerta",
    "alertas",
    "relatorio",
    "relatorio",
    "nps",
    "busca",
    "comentarios",
    "comentários",
    "critico",
    "crítico",
    "criticidade",
    "acao",
    "ação",
    "prioridade",
    "risco",
    "riscos",
    "oportunidade",
    "oportunidades",
    "resolucao",
    "resolução",
    "filtro",
    "filtros",
    "fonte",
    "fontes",
    "pesquisa",
    "relatorios",
    "relatórios",
]



class ChatUnavailableError(RuntimeError):
    """Erro controlado quando a resposta de IA nao pode ser gerada no momento."""


class ChatService:
    @staticmethod
    def _sanitize_user_content(content: str) -> str:
        # Remove control chars and collapse excessive whitespace.
        text = (content or "").replace("\x00", "")
        text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
        text = re.sub(r"[\t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip()[:3000]

    @staticmethod
    def _load_system_prompt() -> str:
        try:
            system_prompt = (PROMPTS_DIR / "domain-closed-system-prompt.md").read_text(encoding="utf-8")
            knowledge = (PROMPTS_DIR / "domain-knowledge-base.md").read_text(encoding="utf-8")
            return f"{system_prompt}\n\n---\n\n## BASE DE CONHECIMENTO DO SISTEMA\n{knowledge}"
        except Exception:
            return "Você é o assistente do SentimentoIA. Responda apenas sobre análise de sentimentos."

    @staticmethod
    def _scope_verdict(message: str) -> tuple[bool, str]:
        msg_lower = str(message or "").lower().strip()
        if not msg_lower:
            return False, "empty_message"

        if any(keyword in msg_lower for keyword in OUT_OF_SCOPE_KEYWORDS):
            return False, "blocked_keyword"

        if any(keyword in msg_lower for keyword in IN_SCOPE_KEYWORDS):
            return True, "domain_keyword"

        # Dominio fechado: sem sinal claro de SentimentoIA, a mensagem e recusada.
        return False, "missing_domain_signal"

    @staticmethod
    def _is_in_scope(message: str) -> bool:
        verdict, _reason = ChatService._scope_verdict(message)
        return verdict

    @staticmethod
    def _refusal_message(locale: str) -> str:
        del locale
        return "Posso ajudar apenas com análises de sentimento e reputação da sua marca no SentimentoIA."

    @staticmethod
    def _temporary_unavailable_message(locale: str) -> str:
        del locale
        return "Assistente temporariamente indisponivel. Tente novamente em instantes."

    @staticmethod
    def _serialize_thread(item: dict[str, Any]) -> dict[str, Any]:
        serialized = SearchService.serialize(item)
        serialized["thread_id"] = serialized.get("thread_id") or serialized.get("id")
        return serialized

    @staticmethod
    def _serialize_message(item: dict[str, Any]) -> dict[str, Any]:
        serialized = SearchService.serialize(item)
        serialized["message_id"] = serialized.get("message_id") or serialized.get("id")
        return serialized

    @staticmethod
    def _ensure_thread(user_id: str, thread_id: str) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        thread = db.chat_threads.find_one({"_id": thread_id, "user_id": user_id})
        if not thread:
            raise ValueError("Thread nao encontrada")
        return thread

    @staticmethod
    def list_threads(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        items = list(
            db.chat_threads.find({"user_id": user_id, "archived": {"$ne": True}})
            .sort("last_message_at", -1)
            .limit(max(1, min(limit, 200)))
        )
        return [ChatService._serialize_thread(item) for item in items]

    @staticmethod
    def create_thread(user_id: str, title: str | None = None, locale: str = "pt-BR") -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        now = utcnow()
        thread_id = f"thread_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        safe_title = (title or DEFAULT_THREAD_TITLE).strip()[:120] or DEFAULT_THREAD_TITLE

        doc = {
            "_id": thread_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "title": safe_title,
            "locale": locale,
            "archived": False,
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
        }
        db.chat_threads.insert_one(doc)
        return ChatService._serialize_thread(doc)

    @staticmethod
    def list_messages(user_id: str, thread_id: str, limit: int = 100) -> list[dict[str, Any]]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        ChatService._ensure_thread(user_id=user_id, thread_id=thread_id)

        items = list(
            db.chat_messages.find({"user_id": user_id, "thread_id": thread_id})
            .sort("created_at", 1)
            .limit(max(1, min(limit, 500)))
        )
        return [ChatService._serialize_message(item) for item in items]

    @staticmethod
    def delete_thread(user_id: str, thread_id: str) -> bool:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        thread = db.chat_threads.find_one(
            {
                "user_id": user_id,
                "$or": [{"_id": thread_id}, {"thread_id": thread_id}],
            }
        )
        if not thread:
            raise ValueError("Thread nao encontrada")

        resolved_thread_id = str(thread.get("thread_id") or thread.get("_id"))
        db.chat_messages.delete_many({"user_id": user_id, "thread_id": resolved_thread_id})
        result = db.chat_threads.delete_one({"_id": thread.get("_id"), "user_id": user_id})
        return result.deleted_count > 0

    @staticmethod
    def delete_message(user_id: str, thread_id: str, message_id: str) -> bool:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        thread = db.chat_threads.find_one(
            {
                "user_id": user_id,
                "$or": [{"_id": thread_id}, {"thread_id": thread_id}],
            }
        )
        if not thread:
            raise ValueError("Thread nao encontrada")

        resolved_thread_id = str(thread.get("thread_id") or thread.get("_id"))
        result = db.chat_messages.delete_one(
            {
                "user_id": user_id,
                "thread_id": resolved_thread_id,
                "$or": [{"_id": message_id}, {"message_id": message_id}],
            }
        )
        return result.deleted_count > 0

    @staticmethod
    def delete_all_threads(user_id: str) -> dict[str, int]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        deleted_threads = db.chat_threads.delete_many({"user_id": user_id}).deleted_count
        deleted_messages = db.chat_messages.delete_many({"user_id": user_id}).deleted_count
        return {
            "deleted_threads": int(deleted_threads),
            "deleted_messages": int(deleted_messages),
        }

    @staticmethod
    def _authorized_context(user_id: str) -> dict[str, Any]:
        return build_authorized_context(user_id=user_id)

    @staticmethod
    async def send_message(
        user_id: str,
        thread_id: str,
        content: str,
        locale: str,
    ) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise RuntimeError(DB_UNAVAILABLE_ERROR)

        thread = ChatService._ensure_thread(user_id=user_id, thread_id=thread_id)
        clean_content = ChatService._sanitize_user_content(content)
        if not clean_content:
            raise ValueError("Mensagem vazia")

        now = utcnow()
        user_message_id = f"cmsg_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        user_message = {
            "_id": user_message_id,
            "message_id": user_message_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "role": "user",
            "content": clean_content[:3000],
            "created_at": now,
        }
        db.chat_messages.insert_one(user_message)

        in_scope, refusal_reason = ChatService._scope_verdict(clean_content)
        assistant_metadata: dict[str, Any] = {"out_of_scope": False}
        if not in_scope:
            assistant_content = ChatService._refusal_message(locale=locale)
            assistant_metadata = {
                "out_of_scope": True,
                "refusal_reason": refusal_reason,
            }
        else:
            history_docs = list(
                db.chat_messages.find(
                    {"user_id": user_id, "thread_id": thread_id, "role": {"$in": ["user", "assistant"]}}
                )
                .sort("created_at", -1)
                .limit(12)
            )
            history_docs.reverse()
            history_messages = [
                {
                    "role": str(item.get("role") or "user"),
                    "content": str(item.get("content") or ""),
                }
                for item in history_docs
            ]

            authorized_context = ChatService._authorized_context(user_id=user_id)
            messages = [
                {"role": "system", "content": ChatService._load_system_prompt()},
                {
                    "role": "system",
                    "content": (
                        "DADOS AUTORIZADOS DO USUÁRIO:\n"
                        f"{json.dumps(authorized_context, ensure_ascii=False, default=str)}"
                    ),
                },
                *history_messages,
                {"role": "user", "content": clean_content},
            ]

            try:
                assistant_content = await LLMService.answer_domain_chat(
                    messages=messages,
                    authorized_context=authorized_context,
                    fail_on_unavailable=True,
                )
            except RuntimeError as exc:
                db.chat_threads.update_one(
                    {"_id": thread_id, "user_id": user_id},
                    {
                        "$set": {
                            "locale": locale,
                            "updated_at": now,
                            "last_message_at": now,
                        }
                    },
                )
                raise ChatUnavailableError(
                    "Assistente IA indisponivel no momento. Verifique a conectividade com o Ollama e tente novamente."
                ) from exc

            if not isinstance(assistant_content, str) or not assistant_content.strip():
                db.chat_threads.update_one(
                    {"_id": thread_id, "user_id": user_id},
                    {
                        "$set": {
                            "locale": locale,
                            "updated_at": now,
                            "last_message_at": now,
                        }
                    },
                )
                raise ChatUnavailableError(
                    "Assistente IA indisponivel no momento. Verifique a conectividade com o Ollama e tente novamente."
                )

        now_response = utcnow()
        assistant_id = f"cmsg_{now_response.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        assistant_message = {
            "_id": assistant_id,
            "message_id": assistant_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "role": "assistant",
            "content": assistant_content[:3500],
            "metadata": assistant_metadata,
            "created_at": now_response,
        }
        db.chat_messages.insert_one(assistant_message)

        title = str(thread.get("title") or "").strip()
        if title in {"", DEFAULT_THREAD_TITLE}:
            title = clean_content[:72]

        db.chat_threads.update_one(
            {"_id": thread_id, "user_id": user_id},
            {
                "$set": {
                    "locale": locale,
                    "title": title,
                    "updated_at": now_response,
                    "last_message_at": now_response,
                }
            },
        )

        return {
            "thread": ChatService._serialize_thread(
                db.chat_threads.find_one({"_id": thread_id, "user_id": user_id})
                or {"_id": thread_id, "thread_id": thread_id, "title": title}
            ),
            "user_message": ChatService._serialize_message(user_message),
            "assistant_message": ChatService._serialize_message(assistant_message),
        }
