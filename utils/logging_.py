"""Structured logging setup for ContextEngine."""

import logging
import sys
from pathlib import Path

from config import LOGS_DIR


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure structured logging."""
    logger = logging.getLogger("context-engine")
    logger.setLevel(level)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console.setFormatter(console_fmt)
    logger.addHandler(console)

    # File handler
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOGS_DIR / "api.log")
    file_handler.setLevel(level)
    file_handler.setFormatter(console_fmt)
    logger.addHandler(file_handler)

    return logger


logger = setup_logging()
