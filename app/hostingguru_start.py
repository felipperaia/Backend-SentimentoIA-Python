from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import Callable

import uvicorn


logger = logging.getLogger(__name__)


def _terminate_worker(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _safe_port(value: str | None, default: int = 3000) -> int:
    try:
        parsed = int(str(value or "").strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _signal_handler_factory(worker_process: subprocess.Popen[bytes]) -> Callable[[int, object], None]:
    def _handler(signum: int, frame: object) -> None:
        del frame
        logger.info("Sinal %s recebido. Encerrando worker e API.", signum)
        _terminate_worker(worker_process)
        raise SystemExit(0)

    return _handler


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    host = os.getenv("HOST", "0.0.0.0")
    port = _safe_port(os.getenv("PORT"), default=3000)

    worker_cmd = [sys.executable, "-m", "apps.worker.app.worker"]
    worker_process = subprocess.Popen(worker_cmd)
    logger.info("Worker iniciado no processo pid=%s", worker_process.pid)

    signal_handler = _signal_handler_factory(worker_process)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        uvicorn.run("app.main:app", host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())
    finally:
        _terminate_worker(worker_process)
        logger.info("Worker finalizado")


if __name__ == "__main__":
    main()
