"""
End-to-end orchestration.

A pipeline is not "the five scripts, called in order". The difference is in
what surrounds each step:

  RUN ID          every run is identifiable. Without it, "the pipeline failed"
                  is unanswerable - which run, on what input, producing what?
  STEP ISOLATION  each step is timed, logged, and its failure is CLASSIFIED,
                  so the run can stop cleanly instead of half-writing outputs.
  QUALITY GATES   between steps. The run can be stopped by BAD DATA, not only
                  by an exception. A pipeline that only fails on crashes will
                  cheerfully publish garbage.
  ATOMIC WRITES   write to a temp file, then rename. A crash mid-write must
                  never leave a half-written CSV that looks complete.
  MANIFEST        a record of what this run read, produced, and decided.
                  This is lineage, and it is the only thing that lets you
                  answer questions about a run after it has finished.

EXIT CODES (an orchestrator reads these, not your prose):
  0 success
  1 configuration or input failure - a human must fix something
  2 data quality gate failed       - upstream data is bad
  3 unexpected error               - a bug in this code
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.ingestion.files import DataLoadError, load_config, load_raw_csv
from src.monitoring.logging import get_logger
from src.pii.detector import detect_pii, render_report as render_pii
from src.pii.masker import Masker, render_sample
from src.profiling.profiler import profile_dataset, render_report as render_profile
from src.transformations.cleaning import Cleaner, render_log
from src.transformations.validation import Validator, render_report as render_validation
from src.utils.retry import PermanentError, TransientError, retry

log = get_logger("pipelines.dq_pipeline")

EXIT_OK, EXIT_INPUT, EXIT_QUALITY, EXIT_BUG = 0, 1, 2, 3


@dataclass
class StepResult:
    name: str
    status: str = "pending"      # ok | failed | skipped
    seconds: float = 0.0
    rows_in: int | None = None
    rows_out: int | None = None
    detail: str = ""
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    started_at: str
    steps: list[StepResult] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK
    halted_at: str | None = None
    halt_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return sum(s.seconds for s in self.steps)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write to a sibling temp file, then rename. Rename is atomic on POSIX,
    so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


@retry(attempts=3, base_delay=0.5)
def _load_with_retry(path: Path, expected: list[str]) -> pd.DataFrame:
    """
    Reading a local file rarely needs retries - but this same function will
    one day point at S3 or an SFTP server, where it absolutely does. The
    classification below is what makes that swap safe:
    a missing file is PERMANENT; a locked or briefly unreadable one is TRANSIENT.
    """
    try:
        return load_raw_csv(path, expected)
    except DataLoadError as exc:
        raise PermanentError(str(exc)) from exc
    except OSError as exc:
        raise TransientError(f"transient I/O error reading {path}: {exc}") from exc


class DataQualityPipeline:
    def __init__(self, root: Path):
        self.root = root
        self.run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.result = RunResult(
            run_id=self.run_id,
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ------------------------------------------------------------------
    def _step(self, name: str):
        """Context helper: time a step, catch its failure, record the outcome."""
        class _Ctx:
            def __init__(ctx, outer, name):
                ctx.outer, ctx.res = outer, StepResult(name=name)
            def __enter__(ctx):
                ctx.t0 = time.perf_counter()
                log.info("[%s] step START: %s", outer_id := ctx.outer.run_id, name)
                return ctx.res
            def __exit__(ctx, exc_type, exc, tb):
                ctx.res.seconds = round(time.perf_counter() - ctx.t0, 3)
                if exc is None:
                    ctx.res.status = ctx.res.status if ctx.res.status != "pending" else "ok"
                    log.info("[%s] step OK: %s (%.3fs)", ctx.outer.run_id, name,
                             ctx.res.seconds)
                else:
                    ctx.res.status = "failed"
                    ctx.res.error = f"{type(exc).__name__}: {exc}"
                    log.error("[%s] step FAILED: %s - %s", ctx.outer.run_id, name,
                              ctx.res.error)
                ctx.outer.result.steps.append(ctx.res)
                return False   # never swallow: the caller decides what to do
        return _Ctx(self, name)

    def _halt(self, step: str, reason: str, code: int) -> RunResult:
        self.result.halted_at = step
        self.result.halt_reason = reason
        self.result.exit_code = code
        log.error("[%s] RUN HALTED at '%s': %s", self.run_id, step, reason)
        return self.result

    # ------------------------------------------------------------------
    def run(self) -> RunResult:
        R, reports = self.root, self.root / "reports"
        log.info("[%s] pipeline run starting", self.run_id)

        # ---- 1. config + load ----------------------------------------
        try:
            with self._step("load_config") as s:
                cfg = load_config(R / "config" / "schema.yaml")
                s.detail = f"{len(cfg['columns'])} columns under contract"
            with self._step("load_raw") as s:
                raw = _load_with_retry(R / "data" / "raw" / "customers_raw.csv",
                                       cfg["expected_columns"])
                s.rows_in = s.rows_out = len(raw)
                s.detail = f"{len(raw.columns)} columns read as text (dtype=str)"
        except (PermanentError, TransientError, DataLoadError) as exc:
            return self._halt("load_raw", str(exc), EXIT_INPUT)
        except Exception as exc:                       # a bug in our own code
            log.error("unexpected error during load:\n%s", traceback.format_exc())
            return self._halt("load_raw", f"unexpected: {exc}", EXIT_BUG)

        # ---- 2. profile ----------------------------------------------
        # Discovery steps are NON-BLOCKING. A failure here loses insight,
        # not correctness, so it must not stop data from being processed.
        with self._step("profile") as s:
            try:
                prof = profile_dataset(raw, cfg)
                _atomic_write_text(reports / "data_quality_report.txt", render_profile(prof))
                self.result.outputs.append("reports/data_quality_report.txt")
                s.rows_in = s.rows_out = len(raw)
                s.detail = (f"{prof['overall_completeness_pct']}% complete, "
                            f"{prof['exact_duplicate_rows']} duplicate rows")
                self.result.metrics["completeness_pct"] = prof["overall_completeness_pct"]
            except Exception as exc:
                s.status, s.error = "failed", str(exc)
                log.warning("profiling failed but is non-blocking; continuing")

        # ---- 3. detect PII -------------------------------------------
        with self._step("detect_pii") as s:
            try:
                pii = detect_pii(raw, cfg)
                _atomic_write_text(reports / "pii_detection_report.txt", render_pii(pii))
                self.result.outputs.append("reports/pii_detection_report.txt")
                s.detail = (f"risk={pii['risk_level']}, {pii['pii_column_count']} PII "
                            f"columns, {pii['affected_individuals']} individuals")
                self.result.metrics["pii_risk"] = pii["risk_level"]
                self.result.metrics["pii_columns"] = pii["pii_column_count"]
            except Exception as exc:
                s.status, s.error = "failed", str(exc)
                log.warning("PII detection failed but is non-blocking; continuing")

        validator = Validator(cfg)

        # ---- 4. validate (measure) -----------------------------------
        with self._step("validate_pre") as s:
            pre = validator.validate(raw, stage="pre-clean")
            s.rows_in = s.rows_out = len(raw)
            s.detail = f"{len(pre.failures)} failures, {len(pre.quarantined_rows)} rows flagged"
            self.result.metrics["failures_pre"] = len(pre.failures)
        if pre.fatal:
            return self._halt("validate_pre", pre.fatal_reason or "structural failure",
                              EXIT_QUALITY)

        # ---- 5. clean -------------------------------------------------
        try:
            with self._step("clean") as s:
                cleaned, clean_res = Cleaner(cfg).clean(raw)
                _atomic_write_csv(R / "data" / "processed" / "customers_cleaned.csv", cleaned)
                if clean_res.quarantined is not None and len(clean_res.quarantined):
                    _atomic_write_csv(R / "data" / "quarantine" / "duplicate_keys.csv",
                                      clean_res.quarantined)
                _atomic_write_text(reports / "cleaning_log.txt",
                                   render_log(clean_res, cfg.get("ambiguous_date_policy")))
                self.result.outputs += ["data/processed/customers_cleaned.csv",
                                        "reports/cleaning_log.txt"]
                s.rows_in, s.rows_out = clean_res.rows_in, clean_res.rows_out
                s.detail = (f"{len(clean_res.changes)} changes, "
                            f"{clean_res.ambiguous_dates} ambiguous dates assumed")
                self.result.metrics["retention_pct"] = round(
                    100 * clean_res.rows_out / max(clean_res.rows_in, 1), 1)
                self.result.metrics["ambiguous_dates"] = clean_res.ambiguous_dates
        except Exception as exc:
            log.error("cleaning failed:\n%s", traceback.format_exc())
            return self._halt("clean", str(exc), EXIT_BUG)

        # ---- 6. validate (prove) + QUALITY GATE ----------------------
        with self._step("validate_post") as s:
            post = validator.validate(cleaned, stage="post-clean")
            _atomic_write_text(reports / "validation_results.txt",
                               render_validation(pre, post))
            self.result.outputs.append("reports/validation_results.txt")
            s.rows_in = s.rows_out = len(cleaned)
            s.detail = f"{len(post.failures)} failures remain"
            self.result.metrics["failures_post"] = len(post.failures)
            self.result.metrics["resolved_pct"] = round(
                100 * (len(pre.failures) - len(post.failures)) / max(len(pre.failures), 1), 1)

        # THE GATE: bad data stops the run even though nothing crashed.
        if post.fatal:
            return self._halt("validate_post", post.fatal_reason or "quality gate failed",
                              EXIT_QUALITY)

        # ---- 7. mask --------------------------------------------------
        try:
            with self._step("mask") as s:
                masker = Masker(cfg)
                masked, meta = masker.mask(cleaned)
                masked = masked.drop(columns=meta["flag_columns"], errors="ignore")
                _atomic_write_csv(R / "data" / "processed" / "customers_masked.csv", masked)
                _atomic_write_text(reports / "masked_sample.txt",
                                   render_sample(meta, len(cleaned), len(masked)))
                self.result.outputs += ["data/processed/customers_masked.csv",
                                        "reports/masked_sample.txt"]
                s.rows_in, s.rows_out = len(cleaned), len(masked)
                r = meta["residual"]
                s.detail = (f"{len(meta['rules'])} columns masked; residual k={r.get('k_min')}, "
                            f"{r.get('unique_pct')}% rows unique")
                self.result.metrics["residual_k_min"] = r.get("k_min")
                self.result.metrics["residual_unique_pct"] = r.get("unique_pct")
        except Exception as exc:
            # Masking failing is a SECURITY event, not just a bug: an unmasked
            # cleaned file already exists on disk. Halt loudly.
            log.error("MASKING FAILED - unmasked output exists on disk:\n%s",
                      traceback.format_exc())
            return self._halt("mask", f"masking failed: {exc}", EXIT_BUG)

        # ---- 8. manifest (lineage) -----------------------------------
        with self._step("write_manifest") as s:
            manifest = {
                "run_id": self.run_id,
                "started_at": self.result.started_at,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "data/raw/customers_raw.csv",
                "source_rows": len(raw),
                "output_rows": len(masked),
                "outputs": self.result.outputs,
                "metrics": self.result.metrics,
                "assumptions": {
                    "ambiguous_date_policy": cfg.get("ambiguous_date_policy"),
                    "ambiguous_dates_affected": clean_res.ambiguous_dates,
                },
                "steps": [{"name": st.name, "status": st.status,
                           "seconds": st.seconds} for st in self.result.steps],
            }
            _atomic_write_text(R / "logs" / f"manifest-{self.run_id}.json",
                               json.dumps(manifest, indent=2))
            self.result.outputs.append(f"logs/manifest-{self.run_id}.json")
            s.detail = "lineage manifest written"

        log.info("[%s] pipeline COMPLETE in %.2fs", self.run_id, self.result.duration)
        return self.result


# --------------------------------------------------------------------------
def render_execution_report(r: RunResult) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78
    ok = r.exit_code == EXIT_OK

    add(bar)
    add("PIPELINE EXECUTION REPORT")
    add(f"Run ID    : {r.run_id}")
    add(f"Started   : {r.started_at}")
    add(f"Duration  : {r.duration:.2f}s")
    add(f"Outcome   : {'SUCCESS' if ok else 'FAILED'}  (exit code {r.exit_code})")
    add(bar)
    add("")
    if not ok:
        add(f"  HALTED AT : {r.halted_at}")
        add(f"  REASON    : {r.halt_reason}")
        add("")
        add("  exit 1 = input/config problem, a human must fix it")
        add("  exit 2 = data quality gate failed, upstream data is bad")
        add("  exit 3 = unexpected error, a bug in the pipeline code")
        add("")

    add(bar)
    add("1. STEP TIMELINE")
    add(bar)
    add(f"  {'#':>3} {'step':<18}{'status':<10}{'secs':>8}{'rows in':>10}{'rows out':>10}  detail")
    add("  " + "-" * 74)
    for i, s in enumerate(r.steps, 1):
        add(f"  {i:>3} {s.name:<18}{s.status:<10}{s.seconds:>8.3f}"
            f"{str(s.rows_in or '-'):>10}{str(s.rows_out or '-'):>10}  {s.detail}")
        if s.error:
            add(f"      ERROR: {s.error}")
    add("")

    add(bar)
    add("2. RUN METRICS")
    add(bar)
    labels = {
        "completeness_pct": "Raw completeness (%)",
        "pii_risk": "PII risk level",
        "pii_columns": "Columns holding PII",
        "failures_pre": "Rule failures before cleaning",
        "failures_post": "Rule failures after cleaning",
        "resolved_pct": "Failures resolved by cleaning (%)",
        "retention_pct": "Rows retained through cleaning (%)",
        "ambiguous_dates": "Ambiguous dates resolved by assumption",
        "residual_k_min": "Residual k-anonymity (min group size)",
        "residual_unique_pct": "Masked rows still unique (%)",
    }
    for key, label in labels.items():
        if key in r.metrics:
            add(f"  {label:<42} {r.metrics[key]}")
    add("")
    add("  These are the numbers to TREND across runs. A single run tells you")
    add("  little; the same metric moving is what signals an upstream change.")
    add("  Example alert: retention drops from 94% to 60% -> the source feed")
    add("  changed and nobody told you.")
    add("")

    add(bar)
    add("3. OUTPUTS PRODUCED")
    add(bar)
    for o in r.outputs:
        add(f"  {o}")
    add("")

    add(bar)
    add("4. OPERATIONAL NOTES")
    add(bar)
    add("  Idempotency : re-running with the same input overwrites the same")
    add("                outputs and produces identical results. Safe to retry.")
    add("  Atomicity   : every file is written to .tmp and renamed, so a crash")
    add("                cannot leave a half-written CSV that looks complete.")
    add("  Isolation   : profiling and PII detection are non-blocking - losing")
    add("                insight must not stop data from being processed.")
    add("                Cleaning and masking are blocking - a masking failure")
    add("                is a SECURITY event, because unmasked output already")
    add("                exists on disk at that point.")
    add("  Lineage     : logs/manifest-<run_id>.json records the source, the")
    add("                outputs, the metrics and the ASSUMPTIONS for this run.")
    add("")
    add(bar)
    add("END OF REPORT")
    add(bar)
    return "\n".join(L)
