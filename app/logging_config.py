"""Logging configuration using Loguru."""

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure Loguru with console + rotating file sinks."""
    logger.remove()

    # Console — human-readable
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # File — structured JSON for production
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger.add(
        log_path / "tradesystem_{time:YYYY-MM-DD}.log",
        level=log_level,
        rotation="00:00",    # rotate at midnight
        retention="14 days",
        compression="gz",
        serialize=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} — {message}",
    )

    logger.info("Logging initialised — level={}", log_level)
