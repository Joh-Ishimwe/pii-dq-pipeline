"""
Ingestion: get bytes off disk into a DataFrame WITHOUT interpreting them.

The single most important decision in this module is dtype=str.
Pandas' type inference silently destroys evidence:
  "00123"       -> 123        (leading zeros gone)
  "0788123456"  -> 788123456  (phone mangled into an int)
  "N/A"         -> NaN        ("someone typed N/A" becomes indistinguishable
                               from "field was left blank")
We convert types LATER, deliberately, in cleaning.py, and log every failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from src.monitoring.logging import get_logger

log = get_logger("ingestion.files")


class DataLoadError(Exception):
    """Raised when the source file cannot be used at all. Permanent - do not retry."""


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise DataLoadError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    log.info("loaded config: %s (%d columns defined)", path.name, len(cfg["columns"]))
    return cfg


def load_raw_csv(path: str | Path, expected_columns: list[str]) -> pd.DataFrame:
    """
    Load the raw CSV as pure text and fail loudly on structural problems.

    Structural problems (missing file, missing columns, zero rows) are
    PERMANENT failures: retrying will not help, and continuing would
    silently produce a wrong report. So we stop the run.
    Row-level problems are handled later, per row, without stopping.
    """
    path = Path(path)
    if not path.exists():
        raise DataLoadError(f"source file not found: {path}")
    if path.stat().st_size == 0:
        raise DataLoadError(f"source file is empty: {path}")

    try:
        df = pd.read_csv(
            path,
            dtype=str,             # everything is text until we say otherwise
            keep_default_na=False,  # do NOT auto-convert "NA"/"null" to NaN
            na_values=[],           # nothing is null on read; we decide what null means
            skipinitialspace=False,  # keep leading whitespace so we can measure it
        )
    except pd.errors.ParserError as exc:
        raise DataLoadError(f"CSV is malformed and could not be parsed: {exc}") from exc

    if df.empty:
        raise DataLoadError(f"source file has a header but zero rows: {path}")

    missing = [c for c in expected_columns if c not in df.columns]
    extra = [c for c in df.columns if c not in expected_columns]

    if missing:
        # Schema drift that breaks everything downstream -> stop the run.
        raise DataLoadError(f"expected columns are missing from source: {missing}")
    if extra:
        # Additive drift is usually safe -> warn, keep going, tell a human.
        log.warning("source has unexpected extra columns: %s", extra)

    log.info("loaded %s: %d rows x %d columns", path.name, len(df), len(df.columns))
    return df
