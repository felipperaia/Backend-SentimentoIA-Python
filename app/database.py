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
    secondary_client: Optional[MongoClient] = None
    secondary_db = None

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

            cls.db.privacyconsents.create_index([("user_id", 1), ("updated_at", -1)])
            cls.db.privacyconsents.create_index([("session_id", 1), ("updated_at", -1)])

            logger.info("✓ Índices criados com sucesso")

        except Exception as exc:
            logger.error("✗ Erro ao criar índices: %s", exc)

    @classmethod
    async def create_secondary_indexes(cls):
        """Índices de staging de ingestão JSON no banco secundário."""
        if cls.secondary_db is None:
            return

        try:
            staging_mentions = cls.secondary_db.ingestion_staging_mentions
            staging_mentions.create_index([("user_id", 1), ("company_slug", 1), ("source", 1), ("published_at", -1)])
            staging_mentions.create_index([("user_id", 1), ("batch_id", 1), ("published_at", -1)])
            staging_mentions.create_index([("user_id", 1), ("company_slug", 1), ("published_at", -1)])
            # Em ambientes existentes, o indice antigo deve ser removido manualmente:
            # db.ingestion_staging_mentions.dropIndex("user_id_1_staging_hash_1")
            staging_mentions.create_index([("staging_hash", 1)], unique=True)

            staging_batches = cls.secondary_db.ingestion_staging_batches
            staging_batches.create_index("batch_id", unique=True)
            staging_batches.create_index([("user_id", 1), ("created_at", -1)])
            logger.info("✓ Índices do MongoDB secundário criados com sucesso")
        except Exception as exc:
            logger.error("✗ Erro ao criar índices do MongoDB secundário: %s", exc)


def get_db():
    """Retorna instância ativa do MongoDB."""
    return MongoDB.db


def get_secondary_db():
    """Retorna instância ativa do MongoDB secundário para staging de ingestão."""
    if MongoDB.secondary_db is None:
        raise RuntimeError(
            "Secondary MongoDB is not configured. Configure SECONDARY_MONGODB_URI and SECONDARY_DATABASE_NAME."
        )
    return MongoDB.secondary_db


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

        secondary_uri = str(settings.secondary_mongodb_uri or "").strip()
        secondary_db_name = str(settings.secondary_database_name or "").strip()
        if secondary_uri and secondary_db_name:
            same_uri = secondary_uri == mongodb_uri
            same_db_name = secondary_db_name == str(settings.database_name or "").strip()
            if same_uri and same_db_name:
                error = (
                    "Configuracao invalida: banco secundario aponta para o mesmo URI e DATABASE_NAME do banco primario. "
                    "Use SECONDARY_MONGODB_URI/SECONDARY_DATABASE_NAME distintos."
                )
                logger.error(error)
                raise RuntimeError(error)
            if same_uri and not same_db_name:
                logger.warning(
                    "MongoDB secundario usa o mesmo cluster do primario, mas com database diferente: %s",
                    secondary_db_name,
                )
            try:
                MongoDB.secondary_client = MongoClient(
                    secondary_uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=10000,
                    retryWrites=True,
                    w="majority",
                )
                MongoDB.secondary_client.admin.command("ping")
                MongoDB.secondary_db = MongoDB.secondary_client[secondary_db_name]
                logger.info("Conectado ao MongoDB secundário com sucesso")
                await MongoDB.create_secondary_indexes()
            except (ConnectionFailure, ServerSelectionTimeoutError, Exception) as secondary_exc:
                logger.warning("MongoDB secundário indisponível: %s", secondary_exc)
                MongoDB.secondary_db = None
                if MongoDB.secondary_client:
                    MongoDB.secondary_client.close()
                    MongoDB.secondary_client = None
        elif secondary_uri or secondary_db_name:
            logger.warning(
                "MongoDB secundário parcialmente configurado. Defina SECONDARY_MONGODB_URI e SECONDARY_DATABASE_NAME."
            )
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

    if MongoDB.secondary_client:
        MongoDB.secondary_client.close()
        MongoDB.secondary_client = None
        MongoDB.secondary_db = None
        logger.info("Conexao com MongoDB secundário fechada")
