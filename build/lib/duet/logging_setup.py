from __future__ import annotations

import logging
import os
from pathlib import Path

LOGGER_NAME = "duet"

_configured = False


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(level: str | None = None, log_file: str | Path | None = None) -> logging.Logger:
    """Configure the duet logger once. Level resolves from the argument, then
    the DUET_LOG env var, then defaults to WARNING so normal runs stay quiet."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    resolved = (level or os.environ.get("DUET_LOG") or "WARNING").upper()
    logger.setLevel(getattr(logging, resolved, logging.WARNING))

    if _configured:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s duet: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    file_target = log_file or os.environ.get("DUET_LOG_FILE")
    if file_target:
        try:
            handler = logging.FileHandler(Path(file_target).expanduser(), encoding="utf-8")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except OSError as exc:  # a broken log path must never abort the run
            logger.warning("could not open log file %s: %s", file_target, exc)

    logger.propagate = False
    _configured = True
    return logger
