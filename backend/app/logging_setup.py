"""App-wide logging.

Critical rule: every pipeline operation runs inside try/except, and every
failure gets logged here rather than crashing the request. Nothing fails
silently.
"""

import logging
from logging.handlers import RotatingFileHandler

from app.paths import LOGS_DIR

_LOGGER_NAME = "loopsite"
_configured = False


def get_logger() -> logging.Logger:
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)

    if not _configured:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logger.setLevel(logging.INFO)

        file_handler = RotatingFileHandler(
            LOGS_DIR / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        console_handler = logging.StreamHandler()

        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )
        file_handler.setFormatter(fmt)
        console_handler.setFormatter(fmt)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        _configured = True

    return logger
