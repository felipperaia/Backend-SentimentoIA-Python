import logging
from typing import Optional

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.config import settings

logger = logging.getLogger(__name__)


class MongoDB:
    """Gerenciador de conexão MongoDB.

    Usa pymongo síncrono por simplicidade. Como as operações são pequenas no MVP,
    isso é suficiente. Para alta escala, migrar para Motor async.
    """

    client: Optional[MongoClient] = None
    db = None

    @classmethod
    async def connect_db(cls):
        await connect_db()

    @classmethod
    async def close_db(cls):
        await disconnect_db()

    @classmethod
    async def create_indexes(cls):
        """Índices para performance e consistência de busca e ingestão."""
        if cls.db is None:
            return

        try:
            cls.db.users.create_index("email", unique=True)

            cls.db.search_jobs.create_index([("user_id", 1), ("created_at", -1)])
            cls.db.search_jobs.create_index("search_id", unique=True)
            cls.db.search_jobs.create_index([("user_id", 1), ("query", 1), ("created_at", -1)])

            cls.db.mentions.create_index([("user_id", 1), ("search_id", 1)])
            cls.db.mentions.create_index([("search_id", 1), ("published_at", -1)])
            cls.db.mentions.create_index("source")
            cls.db.mentions.create_index("sentiment")
            cls.db.mentions.create_index("criticality")
            cls.db.mentions.create_index([("user_id", 1), ("batch_id", 1), ("status", 1), ("created_at", -1)])
            cls.db.mentions.create_index([("user_id", 1), ("external_id", 1), ("batch_id", 1)])
            cls.db.mentions.create_index([("user_id", 1), ("text_fingerprint", 1), ("batch_id", 1)])
            cls.db.mentions.create_index([("user_id", 1), ("content_hash", 1)])
            cls.db.mentions.create_index([("user_id", 1), ("canonical_url", 1)])
            cls.db.mentions.create_index([("user_id", 1), ("source", 1), ("published_at", -1)])
            cls.db.mentions.create_index([("user_id", 1), ("created_at", -1), ("criticality", 1)])

            cls.db.scraped_items.create_index([("source", 1), ("query_key", 1), ("created_at", -1)])
            cls.db.scraped_items.create_index([("source", 1), ("query_key", 1), ("canonical_url", 1)])
            cls.db.scraped_items.create_index([("source", 1), ("query_key", 1), ("content_hash", 1)])

            cls.db.source_checkpoints.create_index([("source", 1), ("query_key", 1)], unique=True)
            cls.db.monitor_sources.create_index("name", unique=True)
            cls.db.monitor_sources.create_index([("active", 1), ("priority", -1)])

            cls.db.comment_batches.create_index([("user_id", 1), ("created_at", -1)])
            cls.db.comment_batches.create_index("batch_id", unique=True)

            cls.db.insight_jobs.create_index([("user_id", 1), ("batch_id", 1), ("status", 1), ("created_at", -1)])
            cls.db.insights.create_index([("user_id", 1), ("batch_id", 1), ("created_at", -1)])
            cls.db.chat_threads.create_index([("user_id", 1), ("created_at", -1)])
            cls.db.chat_messages.create_index([("user_id", 1), ("thread_id", 1), ("created_at", 1)])
            cls.db.dashboard_settings.create_index("user_id", unique=True)

            cls.db.alerts.create_index([("user_id", 1), ("search_id", 1), ("created_at", -1)])
            cls.db.reports.create_index([("user_id", 1), ("search_id", 1), ("created_at", -1)])
            cls.db.audit_logs.create_index([("user_id", 1), ("created_at", -1)])

            cls.db.nps_responses.create_index([("user_id", 1), ("created_at", -1)])
            cls.db.nps_responses.create_index([("session_id", 1), ("created_at", -1)])
            cls.db.nps_responses.create_index([("module_key", 1), ("created_at", -1)])

            logger.info("✓ Índices criados com sucesso")

        except Exception as exc:
            logger.error("✗ Erro ao criar índices: %s", exc)


def get_db():
    """Retorna instância ativa do MongoDB."""
    return MongoDB.db


async def connect_db() -> None:
    """Conecta no MongoDB e cria índices; falha deve impedir startup da API."""
    mongodb_uri = str(settings.mongodb_uri or "").strip()
    if not mongodb_uri:
        error = "MONGODB_URI is not configured"
        logger.error("MongoDB connection failed: %s", error)
        raise RuntimeError(f"MongoDB connection failed: {error}")

    try:
        MongoDB.client = MongoClient(
            mongodb_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            retryWrites=True,
            w="majority",
        )
        MongoDB.client.admin.command("ping")
        MongoDB.db = MongoDB.client[settings.database_name]

        logger.info("Conectado ao MongoDB com sucesso")
        await MongoDB.create_indexes()
    except (ConnectionFailure, ServerSelectionTimeoutError, Exception) as exc:
        logger.exception("MongoDB connection failed")
        raise RuntimeError(f"MongoDB connection failed: {exc}") from exc


async def disconnect_db() -> None:
    """Fecha conexão ativa com MongoDB."""
    if MongoDB.client:
        MongoDB.client.close()
        MongoDB.client = None
        MongoDB.db = None
        logger.info("Conexao com MongoDB fechada")
