from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def utcnow() -> datetime:
    """Retorna datetime UTC padronizado para todos os registros."""
    return datetime.now(timezone.utc)


def make_search_id() -> str:
    """Cria um ID único para cada busca.

    Esse search_id é a chave para evitar misturar dados antigos com dados novos.
    Dashboard, histórico, alertas, CSV e PDF devem sempre filtrar por search_id.
    """
    return str(uuid4())


def normalize_mention(
    *,
    query: str,
    source: str,
    text: Optional[str],
    author: Optional[str] = None,
    published_at: Optional[Any] = None,
    url: Optional[str] = None,
    rating: Optional[float] = None,
    raw: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Normaliza qualquer dado de Google, Reddit ou X em um schema único.

    Retorna None quando o texto estiver vazio, pois comentários vazios não devem
    entrar no pipeline de IA nem nos relatórios.
    """
    clean_text = (text or "").strip()
    if not clean_text:
        return None

    if isinstance(published_at, datetime):
        dt = published_at
    else:
        dt = utcnow()

    return {
        "external_id": str((raw or {}).get("id") or (raw or {}).get("url") or url or uuid4()),
        "query": query,
        "source": source,
        "text": clean_text[:5000],
        "author": author or "desconhecido",
        "published_at": dt,
        "url": url,
        "rating": rating,
        "raw": raw or {},
        "created_at": utcnow(),
    }
