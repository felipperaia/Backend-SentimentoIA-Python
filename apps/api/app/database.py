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
        """Conecta no MongoDB Atlas/local e cria índices."""
        try:
            cls.client = MongoClient(
                settings.MONGODB_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                retryWrites=True,
                w="majority",
            )
            cls.client.admin.command("ping")
            cls.db = cls.client[settings.DATABASE_NAME]

            logger.info("✓ Conectado ao MongoDB com sucesso")
            await cls.create_indexes()

        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            logger.error("✗ Erro ao conectar ao MongoDB: %s", exc)
            # Se estiver em ambiente de desenvolvimento, tenta usar mongomock como
            # fallback para permitir que a aplicação rode sem um servidor MongoDB
            # local durante o desenvolvimento.
            if getattr(settings, "ENV", "").lower() == "development":
                try:
                    import mongomock  # type: ignore

                    logger.warning("! Usando mongomock como fallback (ENV=development)")
                    cls.client = mongomock.MongoClient()
                    cls.db = cls.client[settings.DATABASE_NAME]
                    logger.info("✓ Conectado ao mongomock (fallback)")
                    await cls.create_indexes()
                    return
                except Exception as mexc:
                    logger.error("✗ Falha ao ativar mongomock fallback: %s", mexc)
            raise

    @classmethod
    async def close_db(cls):
        """Fecha conexão MongoDB."""
        if cls.client:
            cls.client.close()
            logger.info("✓ Conexão com MongoDB fechada")

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
