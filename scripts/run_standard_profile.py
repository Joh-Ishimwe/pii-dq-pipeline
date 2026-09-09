"""
Companion EDA report, built on ydata-profiling (the industry-standard Python
profiler - the actively maintained successor to pandas-profiling).

This is NOT a replacement for src/profiling/profiler.py. The two do
different jobs:

  src/profiling/profiler.py   Checks THIS data against OUR contract:
                               config/schema.yaml rules, our null-in-disguise
                               list, our PII classes, our fintech-specific
                               cross-column logic (age < 18, born-after-signup,
                               same person under two ids, ...). No generic
                               library knows any of that - it has to be code.

  ydata-profiling (this file)  Generic, dataset-agnostic exploration: value
                               distributions, correlations, interaction plots.
                               Excellent at "what does this column look like",
                               useless at "is this column allowed to look
                               like that" - it has no idea what a valid
                               account_status is.

Use both. Neither makes the other redundant.

DTYPE NOTE: the rest of this pipeline loads every column as text on purpose
(see src/ingestion/files.py) to avoid silently mangling values like a
customer_id key. That protection doesn't matter here - this script only
ever READS the data and never writes it back anywhere - so we coerce
numeric/date columns to real dtypes below. Skipping that step would leave
ydata-profiling unable to compute a histogram or a date range at all; it
would just see text.

*** OUTPUT CONTAINS REAL, UNMASKED PII ***
ydata-profiling's HTML report embeds sample rows and per-value distributions
from the RAW file. Treat reports/standard_profile.html exactly like
data/raw/ and reports/masked_sample.txt: never commit it, never attach it to
a ticket, restrict it to the data team. It is already covered by the
`reports/` entry in .gitignore - keep it that way.

Usage: python scripts/run_standard_profile.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import DataLoadError, load_config, load_raw_csv
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_standard_profile")


def _coerce_for_eda(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Best-effort dtype coercion, for exploration only.

    Unparseable values become NaN (errors="coerce") rather than raising -
    that's fine here: this script reports on distributions, it doesn't
    validate. src/transformations/validation.py is what enforces correctness;
    duplicating that logic here would just be a second, weaker copy of it.
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


def main() -> int:
    try:
        from ydata_profiling import ProfileReport
    except ImportError:
        log.error(
            "ydata-profiling is not installed. Run: pip install -r requirements.txt"
        )
        return 1

    try:
        cfg = load_config(ROOT / "config" / "schema.yaml")
        df = load_raw_csv(ROOT / "data" / "raw" / "customers_raw.csv", cfg["expected_columns"])
    except DataLoadError as exc:
        log.error("cannot profile - permanent load failure: %s", exc)
        return 1

    df = _coerce_for_eda(df, cfg)

    report = ProfileReport(
        df,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
