"""Entry point for Part 3. Run from project root: python scripts/run_validation.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import load_config, load_raw_csv, DataLoadError
from src.transformations.validation import Validator, render_report
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_validation")


def main() -> int:
    try:
        cfg = load_config(ROOT / "config" / "schema.yaml")
        df = load_raw_csv(ROOT / "data" / "raw" / "customers_raw.csv", cfg["expected_columns"])
    except DataLoadError as exc:
        log.error("cannot validate - permanent load failure: %s", exc)
        return 1

    report = Validator(cfg).validate(df, stage="pre-clean")
    out = ROOT / "reports" / "validation_results.txt"
    out.write_text(render_report(report), encoding="utf-8")
    log.info("wrote %s", out.relative_to(ROOT))
    return 2 if report.fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
