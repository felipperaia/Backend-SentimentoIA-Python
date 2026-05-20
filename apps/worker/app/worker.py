import asyncio
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load canonical API env file before importing backend settings.
ROOT_DIR = Path(__file__).resolve().parents[3]
API_ENV_FILE = ROOT_DIR / "apps" / "api" / ".env"
load_dotenv(API_ENV_FILE, override=False)

# Allow importing the official backend package (apps/api/app/*).
API_PATH = ROOT_DIR / "apps" / "api"
if str(API_PATH) not in sys.path:
    sys.path.insert(0, str(API_PATH))

from app.config import settings
from app.database import MongoDB, get_db
from app.services.collector_orchestrator import CollectorOrchestrator
from app.services.insight_service import InsightService
from app.services.processing_service import ProcessingService
from app.services.source_registry_service import SourceRegistryService


logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)
_is_running = True


def _stop_signal_handler(signum, frame):
    del signum, frame
    global _is_running
    _is_running = False


async def run_worker() -> None:
    poll_interval = max(1, int(settings.WORKER_POLL_INTERVAL_SECONDS))
    batch_size = max(1, int(settings.WORKER_BATCH_SIZE))

    logger.info("Worker iniciado. poll_interval=%ss batch_size=%s", poll_interval, batch_size)

    await MongoDB.connect_db()
    try:
        while _is_running:
            try:
                db = get_db()
                if db is not None:
                    latest_search = db.search_jobs.find_one(
                        {"status": {"$in": ["running", "completed"]}},
                        sort=[("updated_at", -1)],
                    )
                    latest_query = str((latest_search or {}).get("query") or "").strip()
                    latest_sources = (latest_search or {}).get("sources")

                    if latest_query:
                        orchestrator_sources = latest_sources if isinstance(latest_sources, list) else SourceRegistryService.default_sources()
                        orchestrator = CollectorOrchestrator(active_sources=orchestrator_sources)
                        await orchestrator.gather_all(
                            query=latest_query,
                            limit=min(batch_size, int(settings.SCRAPER_MAX_ITEMS_PER_SOURCE)),
                        )
                        if orchestrator.last_errors:
                            logger.warning("Orchestrator de coleta reportou %s falhas no ciclo atual", len(orchestrator.last_errors))

                cycle_result = ProcessingService.process_pending_mentions(limit=batch_size)
                queue_result = InsightService.enqueue_jobs_for_ready_batches(limit=100)
                insight_result = await InsightService.process_queued_jobs(limit=2)

                if cycle_result["found"] > 0:
                    logger.info(
                        "Ciclo worker: found=%s processed=%s errors=%s queued_insights=%s generated=%s failed=%s",
                        cycle_result["found"],
                        cycle_result["processed"],
                        cycle_result["errors"],
                        queue_result.get("queued", 0),
                        insight_result.get("completed", 0),
                        insight_result.get("failed", 0),
                    )
            except Exception as exc:
                logger.exception("Falha no loop principal do worker. Nova tentativa em %ss: %s", poll_interval, exc)
            await asyncio.sleep(poll_interval)
    finally:
        await MongoDB.close_db()
        logger.info("Worker finalizado")


def main() -> None:
    signal.signal(signal.SIGINT, _stop_signal_handler)
    signal.signal(signal.SIGTERM, _stop_signal_handler)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
