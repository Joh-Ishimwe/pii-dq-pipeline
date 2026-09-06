"""Entry point for Part 2. Run from project root: python scripts/run_pii_detection.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import load_config, load_raw_csv, DataLoadError
from src.pii.detector import detect_pii, render_report
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_pii_detection")


def main() -> int:
    try:
        cfg = load_config(ROOT / "config" / "schema.yaml")
        df = load_raw_csv(ROOT / "data" / "raw" / "customers_raw.csv", cfg["expected_columns"])
    except DataLoadError as exc:
        log.error("cannot scan - permanent load failure: %s", exc)
        return 1

    result = detect_pii(df, cfg)
    out = ROOT / "reports" / "pii_detection_report.txt"
    out.write_text(render_report(result), encoding="utf-8")
    log.info("wrote %s", out.relative_to(ROOT))
    # log counts and column NAMES only - never values
    log.warning("PII scan: risk=%s, %d PII columns, %d individuals affected",
                result["risk_level"], result["pii_column_count"],
                result["affected_individuals"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
