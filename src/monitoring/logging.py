"""
Central logging setup.

RULE: log identifiers and counts, never field VALUES.
`logger.warning("row 42: bad email")` is fine.
`logger.warning(f"bad email: {row['email']}")` leaks PII into a file
that gets shipped, retained and read by people who shouldn't see it.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:          # already configured; don't double-attach
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # rotate at 5 MB, keep 3 old files -> logs can never fill the disk
    fileh = RotatingFileHandler(
        LOG_DIR / "pipeline.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    logger.addHandler(fileh)

    logger.propagate = False
    return logger
