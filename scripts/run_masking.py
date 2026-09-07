"""Part 5: mask the CLEANED data. Never mask raw - you would be masking junk."""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import load_config, DataLoadError
from src.pii.masker import Masker, render_sample
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_masking")


def main() -> int:
    cfg = load_config(ROOT / "config" / "schema.yaml")
    src = ROOT / "data" / "processed" / "customers_cleaned.csv"
    if not src.exists():
        log.error("cleaned file not found - run scripts/run_cleaning.py first")
        return 1

    df = pd.read_csv(src, dtype=str, keep_default_na=False, na_values=[])
    masked, meta = Masker(cfg).mask(df)

    # internal bookkeeping columns do not belong in an external extract
    masked = masked.drop(columns=meta["flag_columns"], errors="ignore")
    masked.to_csv(ROOT / "data" / "processed" / "customers_masked.csv", index=False)

    (ROOT / "reports" / "masked_sample.txt").write_text(
        render_sample(meta, len(df), len(masked)), encoding="utf-8")
    log.info("wrote customers_masked.csv (%d rows) and reports/masked_sample.txt",
             len(masked))
    r = meta["residual"]
    if r and r["k_min"] == 1:
        log.warning("residual risk: %d row(s) still unique on quasi-identifiers "
                    "(k=1) - extract is pseudonymized, NOT anonymized",
                    r["unique_rows"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
