import asyncio
import logging
import signal
import sys
from pathlib import Path

# Allow importing the official backend package from repository root.
ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.database import MongoDB, get_db
from app.services.insight_service import InsightService
from app.services.processing_service import ProcessingService


logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)
_is_running = True


def _stop_signal_handler(signum, frame):
    del signum, frame
    global _is_running
    _is_running = False


async def run_worker() -> None:
    poll_interval = 5
    batch_size = max(1, int(settings.batch_size))

    logger.info("Worker iniciado. poll_interval=%ss batch_size=%s", poll_interval, batch_size)

    await MongoDB.connect_db()
    try:
        while _is_running:
            try:
                db = get_db()
                if db is None:
                    await asyncio.sleep(poll_interval)
                    continue

                cycle_result = await ProcessingService.process_pending_mentions(limit=batch_size)
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
