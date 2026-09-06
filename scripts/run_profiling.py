"""Entry point for Part 1. Run from the project root: python scripts/run_profiling.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import load_config, load_raw_csv, DataLoadError
from src.profiling.profiler import profile_dataset, render_report
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_profiling")


def main() -> int:
    try:
        cfg = load_config(ROOT / "config" / "schema.yaml")
        df = load_raw_csv(ROOT / "data" / "raw" / "customers_raw.csv", cfg["expected_columns"])
    except DataLoadError as exc:
        log.error("cannot profile - permanent load failure: %s", exc)
        return 1

    profile = profile_dataset(df, cfg)
    out = ROOT / "reports" / "data_quality_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(profile), encoding="utf-8")
    log.info("wrote %s", out.relative_to(ROOT))
    log.info("summary: %d rows, %.2f%% complete, %d exact duplicate rows",
             profile["row_count"], profile["overall_completeness_pct"],
             profile["exact_duplicate_rows"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
