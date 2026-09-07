"""Part 4: clean, then re-validate to PROVE the cleaning worked."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import load_config, load_raw_csv, DataLoadError
from src.transformations.cleaning import Cleaner, render_log
from src.transformations.validation import Validator, render_report
from src.monitoring.logging import get_logger

log = get_logger("scripts.run_cleaning")


def main() -> int:
    try:
        cfg = load_config(ROOT / "config" / "schema.yaml")
        df = load_raw_csv(ROOT / "data" / "raw" / "customers_raw.csv", cfg["expected_columns"])
    except DataLoadError as exc:
        log.error("permanent load failure: %s", exc)
        return 1

    validator = Validator(cfg)
    pre = validator.validate(df, stage="pre-clean")

    cleaned, res = Cleaner(cfg).clean(df)

    post = validator.validate(cleaned, stage="post-clean")

    (ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)
    cleaned.to_csv(ROOT / "data" / "processed" / "customers_cleaned.csv", index=False)
    if res.quarantined is not None and len(res.quarantined):
        res.quarantined.to_csv(ROOT / "data" / "quarantine" / "duplicate_keys.csv", index=False)

    (ROOT / "reports" / "cleaning_log.txt").write_text(
        render_log(res, cfg.get("ambiguous_date_policy", "day_first")), encoding="utf-8")
    (ROOT / "reports" / "validation_results.txt").write_text(
        render_report(pre, post), encoding="utf-8")

    log.info("pre-clean failures=%d  post-clean failures=%d  (%.1f%% resolved)",
             len(pre.failures), len(post.failures),
             100 * (len(pre.failures) - len(post.failures)) / max(len(pre.failures), 1))
    return 2 if post.fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
