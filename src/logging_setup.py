import logging
import os
import sys
from datetime import datetime

from src.config import LOG_DIR

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, run_id: str = "") -> str:
    """Configure console + rotating-per-run file logging. Returns the log path."""
    global _CONFIGURED

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"pipeline_{stamp}.log")

    if _CONFIGURED:
        return log_path

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
    ))

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(message)s"))

    root.handlers = [file_handler, console]

    # Third-party libraries are noisy at DEBUG; keep them at WARNING.
    for noisy in ("urllib3", "google", "google_genai", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return log_path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
