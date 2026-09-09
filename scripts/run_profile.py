"""
Part 1 (EDA / data quality profiling) - two parts, one file:

  PART 1 - ydata-profiling companion. Generic, dataset-agnostic exploration
           (distributions, correlations). Doesn't know our rules. Optional,
           import-guarded so its absence can't block Part 2.
           -> reports/standard_profile.html  (UNMASKED PII - never commit)

  PART 2 - custom profiling (src/profiling/profiler.py). Checks data against
           OUR contract (config/schema.yaml, PII classes, cross-column rules).
           Required deliverable, always runs.
           -> reports/data_quality_report.txt  (safe to share)

Usage: python scripts/run_profile.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import DataLoadError, load_config, load_raw_csv
from src.monitoring.logging import get_logger
from src.profiling.profiler import profile_dataset, render_report

log = get_logger("scripts.run_profile")


# --------------------------------------------------------------------------
# PART 1 - ydata-profiling companion (optional)
# --------------------------------------------------------------------------
def _coerce_for_eda(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Coerce numeric/date columns to real dtypes, for exploration only.

    Everything loads as text by default (src/ingestion/files.py) to avoid
    mangling values like customer_id. ydata-profiling needs real dtypes to
    plot histograms/date ranges, so we coerce here - read-only, never
    written back. Bad values become NaN (errors="coerce"); this is
    exploration, not validation (that's src/transformations/validation.py).
    """
    out = df.copy()
    for col, rules in config["columns"].items():
        if col not in out.columns:
            continue
        if rules.get("type") in ("integer", "numeric"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        elif rules.get("type") == "date":
            out[col] = pd.to_datetime(out[col], errors="coerce")
    return out


def _run_ydata_profile(df: pd.DataFrame, cfg: dict) -> None:
    """PART 1 - optional companion. Its absence must never block Part 2."""
    try:
        from ydata_profiling import ProfileReport
    except ImportError:
        log.warning(
            "ydata-profiling not installed - skipping standard_profile.html "
            "(pip install -r requirements.txt to enable it)"
        )
        return

    coerced = _coerce_for_eda(df, cfg)
    report = ProfileReport(
        coerced,
        title="customers_raw.csv - standard EDA profile",
        explorative=True,
    )
    out = ROOT / "reports" / "standard_profile.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    report.to_file(out)
    log.warning(
        "%s contains UNMASKED sample data - restricted, do not commit or share",
        out.relative_to(ROOT),
    )


# --------------------------------------------------------------------------
# PART 2 - custom profiling (required deliverable)
# --------------------------------------------------------------------------
def _run_custom_profile(df: pd.DataFrame, cfg: dict) -> None:
    profile = profile_dataset(df, cfg)
    out = ROOT / "reports" / "data_quality_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(profile), encoding="utf-8")
    log.info("wrote %s", out.relative_to(ROOT))
    log.info("summary: %d rows, %.2f%% complete, %d exact duplicate rows",
              profile["row_count"], profile["overall_completeness_pct"],
              profile["exact_duplicate_rows"])


def main() -> int:
    try:
        cfg = load_config(ROOT / "config" / "schema.yaml")
        df = load_raw_csv(ROOT / "data" / "raw" / "customers_raw.csv", cfg["expected_columns"])
    except DataLoadError as exc:
        log.error("cannot profile - permanent load failure: %s", exc)
        return 1

    _run_ydata_profile(df, cfg)    # optional - failures are logged, not fatal
    _run_custom_profile(df, cfg)   # required - a failure here should be loud
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
