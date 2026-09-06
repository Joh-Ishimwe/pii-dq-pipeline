"""
THE entry point. Everything else is a module.

Usage:
    python scripts/run_pipeline.py

Exit codes are what an orchestrator (cron, Airflow, GitHub Actions) reads:
    0 ok | 1 input problem | 2 quality gate failed | 3 bug
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pipelines.dq_pipeline import DataQualityPipeline, render_execution_report
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_pipeline")


def main() -> int:
    pipeline = DataQualityPipeline(ROOT)
    result = pipeline.run()

    out = ROOT / "reports" / "pipeline_execution_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_execution_report(result), encoding="utf-8")
    log.info("execution report: %s", out.relative_to(ROOT))

    if result.exit_code != 0:
        log.error("PIPELINE FAILED at '%s': %s", result.halted_at, result.halt_reason)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
