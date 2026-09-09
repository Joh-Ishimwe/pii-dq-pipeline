"""
Two design decisions worth defending in a review:

1. RULES LIVE IN config/schema.yaml, NOT IN THIS FILE.
   This module is a rule ENGINE. Adding a fourth account_status is a
   one-line YAML edit by an analyst, not a code change and deploy.

2. EVERY FAILURE CARRIES A SEVERITY, AND SEVERITY DECIDES BEHAVIOUR.
   warn       -> log, row continues
   quarantine -> row is diverted, the other 200 rows still get processed
   fail       -> stop the run, wake someone up
   "Is this data valid?" is the easy question. "What do I DO about it?"
   is the one that separates a script from a pipeline.

This same module is called TWICE by the pipeline: once on raw data (to
measure how bad it is) and once on cleaned data (to prove the cleaning
worked). One implementation, two call sites.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.monitoring.logging import get_logger

log = get_logger("transformations.validation")

EMAIL_STRICT = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
PHONE_STRICT = re.compile(r"^\d{3}-\d{3}-\d{4}$")   # the normalized target format
DATE_STRICT = "%Y-%m-%d"


@dataclass
class Failure:
    """One rule, broken, in one place. The atom of a validation result."""
    check: str            # e.g. "range_invalid"
    column: str | None
    severity: str         # warn | quarantine | fail
    message: str
    row_index: int | None = None
    # NOTE: we store the row INDEX, never the offending VALUE, for PII columns.
    value_shape: str | None = None


@dataclass
class ValidationReport:
    stage: str                                  # "pre-clean" or "post-clean"
    row_count: int = 0
    failures: list[Failure] = field(default_factory=list)
    quarantined_rows: set[int] = field(default_factory=set)
    fatal: bool = False
    fatal_reason: str | None = None

    @property
    def passed(self) -> bool:
        return not self.fatal and not self.failures

    def add(self, f: Failure) -> None:
        self.failures.append(f)
        if f.severity == "quarantine" and f.row_index is not None:
            self.quarantined_rows.add(f.row_index)
        if f.severity == "fail":
            self.fatal = True
            self.fatal_reason = self.fatal_reason or f.message

    def by_check(self) -> dict[str, int]:
        return dict(Counter(f.check for f in self.failures))

    def by_column(self) -> dict[str, int]:
        return dict(Counter(f.column or "<dataset>" for f in self.failures))


# --------------------------------------------------------------------------
# parsing helpers - a check needs to know if a value CAN become its type
# --------------------------------------------------------------------------
def _clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _to_number(v: str) -> float | None:
    try:
        return float(v.replace(",", "").replace("$", ""))
    except (ValueError, AttributeError):
        return None


def _to_date(v: str, fmt: str = DATE_STRICT) -> date | None:
    try:
        return datetime.strptime(v, fmt).date()
    except (ValueError, TypeError):
        return None


def _shape(v: str) -> str:
    return re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "x", v))[:24]


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------
# Checks that CLEANING IS DESIGNED TO FIX.
# At the pre-clean stage these are measurements, not verdicts: quarantining a
# row for having a phone as (123) 456-7890 is absurd when the very next step
# normalises it. So pre-clean we downgrade them to `warn` and only enforce
# post-clean, where a remaining failure means cleaning genuinely FAILED.
#
# This is the "two contracts" idea:
#   INGEST contract    - what may ARRIVE (loose: structure + keys + absurdities)
#   WAREHOUSE contract - what may be LOADED (strict: normalised formats)
# Same rule engine, different enforcement, chosen by stage.
FIXABLE_BY_CLEANING = {
    "type_invalid",
    "format_invalid",
    "category_invalid",
    "length_invalid",
    "alphabetic_invalid",
    "duplicate_row",
}


class Validator:
    def __init__(self, config: dict):
        self.cfg = config
        self.columns: dict[str, dict] = config["columns"]
        self.null_like = set(config.get("null_like_values", [""]))
        self.severity: dict[str, str] = config.get("severity", {})
        self.max_quarantine_pct: float = config.get("max_quarantine_rate_pct", 100)
        self.stage: str = "pre-clean"

    def _sev(self, check: str, col: str | None = None) -> str:
        if self.stage == "pre-clean" and check in FIXABLE_BY_CLEANING:
            return "warn"          # measure now, enforce after cleaning
        if col and "on_fail" in self.columns.get(col, {}):
            return self.columns[col]["on_fail"]
        return self.severity.get(check, "warn")

    def _is_missing(self, v: str) -> bool:
        return v.strip() in self.null_like

    # ---------------- dataset-level ------------------------------------
    def _validate_structure(self, df: pd.DataFrame, rep: ValidationReport) -> None:
        if df.empty:
            rep.add(Failure("empty_dataset", None, self._sev("empty_dataset"),
                            "dataset contains zero rows"))
            return

        for col in self.cfg["expected_columns"]:
            if col not in df.columns:
                rep.add(Failure("missing_column", col, self._sev("missing_column"),
                                f"required column '{col}' is absent from the dataset"))

        dupe_rows = df.duplicated(keep="first")
        n = int(dupe_rows.sum())
        if n:
            rep.add(Failure("duplicate_row", None, self._sev("duplicate_row"),
                            f"{n} exact duplicate row(s) present"))

    # ---------------- column-level -------------------------------------
    def _validate_column(self, df: pd.DataFrame, col: str, rules: dict,
                         rep: ValidationReport) -> None:
        if col not in df.columns:
            return
        series = df[col].astype(str)

        # uniqueness is a WHOLE-COLUMN property, not a per-row one
        if rules.get("unique"):
            present = [_clean(v) for v in series if not self._is_missing(_clean(v))]
            dupes = {k for k, c in Counter(present).items() if c > 1}
            if dupes:
                rows = [i for i, v in zip(df.index, series) if _clean(v) in dupes]
                rep.add(Failure(
                    "duplicate_key", col, self._sev("duplicate_key", col),
                    f"{len(rows)} row(s) share {len(dupes)} duplicated "
                    f"'{col}' value(s) in a column declared UNIQUE",
                ))

        for idx, raw in zip(df.index, series):
            v = _clean(raw)

            if self._is_missing(v):
                if rules.get("required"):
                    # A gap that cleaning DELIBERATELY recorded (via a
                    # `<col>_missing` flag) is a KNOWN, accepted absence -
                    # not the same thing as data going missing unexpectedly.
                    # The contract's `required` means "present, or explicitly
                    # accounted for". Without this distinction the warehouse
                    # contract and the cleaning policy contradict each other.
                    flag_col = f"{col}_missing"
                    recorded = (
                        flag_col in df.columns
                        and bool(df.at[idx, flag_col])
                    )
                    if recorded:
                        rep.add(Failure("required_missing_recorded", col, "warn",
                                        "value missing, but the gap is recorded in "
                                        f"{flag_col} - accepted", idx))
                    else:
                        rep.add(Failure("required_missing", col,
                                        self._sev("required_missing", col),
                                        "required value is missing and NOT recorded",
                                        idx))
                continue  # every other check is meaningless on a missing value

            self._check_value(col, rules, v, idx, rep)

    def _check_value(self, col: str, rules: dict, v: str, idx: int,
                     rep: ValidationReport) -> None:
        t = rules.get("type")

        if t in ("integer", "numeric"):
            num = _to_number(v)
            if num is None:
                rep.add(Failure("type_invalid", col, self._sev("type_invalid", col),
                                f"value is not a valid {t}", idx, _shape(v)))
                return
            if t == "integer" and num != int(num):
                rep.add(Failure("type_invalid", col, self._sev("type_invalid", col),
                                "value is not a whole number", idx, _shape(v)))
            if "min" in rules and num < rules["min"]:
                rep.add(Failure("range_invalid", col, self._sev("range_invalid", col),
                                f"value below minimum {rules['min']}", idx))
            if "max" in rules and num > rules["max"]:
                rep.add(Failure("range_invalid", col, self._sev("range_invalid", col),
                                f"value above maximum {rules['max']:,}", idx))

        elif t == "date":
            d = _to_date(v, rules.get("date_format", DATE_STRICT))
            if d is None:
                rep.add(Failure("type_invalid", col, self._sev("type_invalid", col),
                                f"not a valid date in "
                                f"{rules.get('date_format', DATE_STRICT)} format",
                                idx, _shape(v)))
                return
            if d > date.today():
                rep.add(Failure("range_invalid", col, self._sev("range_invalid", col),
                                "date is in the future", idx))
            if "max_age_years" in rules:
                age = (date.today() - d).days / 365.25
                if age > rules["max_age_years"]:
                    rep.add(Failure("range_invalid", col,
                                    self._sev("range_invalid", col),
                                    f"implied age {age:.0f} exceeds "
                                    f"{rules['max_age_years']}", idx))
                if "min_age_years" in rules and age < rules["min_age_years"]:
                    rep.add(Failure("range_invalid", col,
                                    self._sev("range_invalid", col),
                                    f"implied age {age:.0f} is under the minimum "
                                    f"{rules['min_age_years']} - COMPLIANCE REVIEW",
                                    idx))

        elif t == "category":
            allowed = rules.get("allowed", [])
            if v not in allowed:
                near = " (differs only by case/whitespace)" if v.lower() in allowed else ""
                rep.add(Failure("category_invalid", col,
                                self._sev("category_invalid", col),
                                f"'{v}' is not one of {allowed}{near}", idx))

        else:  # string
            fmt = rules.get("format")
            if fmt == "email" and not EMAIL_STRICT.match(v):
                rep.add(Failure("format_invalid", col, self._sev("format_invalid", col),
                                "not a valid email address", idx, _shape(v)))
            if fmt == "phone" and not PHONE_STRICT.match(v):
                rep.add(Failure("format_invalid", col, self._sev("format_invalid", col),
                                "phone is not in NNN-NNN-NNNN format", idx, _shape(v)))
            lo, hi = rules.get("min_length"), rules.get("max_length")
            if lo is not None and len(v) < lo:
                rep.add(Failure("length_invalid", col, self._sev("length_invalid", col),
                                f"length {len(v)} is below minimum {lo}", idx))
            if hi is not None and len(v) > hi:
                rep.add(Failure("length_invalid", col, self._sev("length_invalid", col),
                                f"length {len(v)} exceeds maximum {hi}", idx))
            if rules.get("alphabetic") and not v.replace(" ", "").replace("-", "").isalpha():
                rep.add(Failure("alphabetic_invalid", col,
                                self._sev("alphabetic_invalid", col),
                                "contains non-alphabetic characters", idx, _shape(v)))

    # ---------------- cross-field --------------------------------------
    def _validate_cross_field(self, df: pd.DataFrame, rep: ValidationReport) -> None:
        if not {"date_of_birth", "created_date"}.issubset(df.columns):
            return
        for idx, dob_raw, cre_raw in zip(df.index, df["date_of_birth"], df["created_date"]):
            dob = _to_date(_clean(dob_raw))
            cre = _to_date(_clean(cre_raw))
            if dob and cre and dob > cre:
                rep.add(Failure("cross_field_invalid", "date_of_birth",
                                self._sev("cross_field_invalid"),
                                "date_of_birth is after created_date "
                                "(customer born after signing up)", idx))

    # ---------------- entry point --------------------------------------
    def validate(self, df: pd.DataFrame, stage: str = "pre-clean") -> ValidationReport:
        self.stage = stage
        rep = ValidationReport(stage=stage, row_count=len(df))
        log.info("[%s] validating %d rows", stage, len(df))

        self._validate_structure(df, rep)
        if rep.fatal:
            log.error("[%s] FATAL structural failure: %s", stage, rep.fatal_reason)
            return rep

        for col, rules in self.columns.items():
            self._validate_column(df, col, rules, rep)
        self._validate_cross_field(df, rep)

        # Circuit breaker: too much quarantine means the SOURCE is broken, not
        # the rows, and cleaning would only launder bad data into the warehouse.
        #
        # It is deliberately POST-CLEAN ONLY. Pre-clean is a measurement of a
        # file we already know is messy - stopping there would mean the pipeline
        # can never run at all. Post-clean, a high rate means cleaning FAILED,
        # which is a genuine emergency.
        rate = 100 * len(rep.quarantined_rows) / max(len(df), 1)
        if stage != "pre-clean" and rate > self.max_quarantine_pct:
            rep.fatal = True
            rep.fatal_reason = (
                f"{rate:.1f}% of rows would be quarantined, above the "
                f"{self.max_quarantine_pct}% threshold - the source feed is "
                "likely broken. Investigate upstream before reprocessing."
            )
            log.error("[%s] circuit breaker tripped: %s", stage, rep.fatal_reason)

        log.info("[%s] %d failures across %d rows; %d row(s) quarantined (%.1f%%)",
                 stage, len(rep.failures), len(df), len(rep.quarantined_rows), rate)
        return rep


# --------------------------------------------------------------------------
def render_report(pre: ValidationReport, post: ValidationReport | None = None) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78

    add(bar)
    add("VALIDATION RESULTS - customers dataset")
    add(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add(bar)
    add("")
    add("EXECUTIVE SUMMARY")
    add("-" * 78)
    add(f"  Stage evaluated ............... {pre.stage}")
    add(f"  Rows validated ................ {pre.row_count:,}")
    add(f"  Total rule failures ........... {len(pre.failures):,}")
    add(f"  Rows flagged for quarantine ... {len(pre.quarantined_rows):,} "
        f"({100*len(pre.quarantined_rows)/max(pre.row_count,1):.1f}%)")
    add(f"  Run-stopping failures ......... {'YES - ' + (pre.fatal_reason or '') if pre.fatal else 'none'}")
    if post:
        add("")
        add(f"  AFTER CLEANING: {len(post.failures):,} failures remain "
            f"({len(pre.failures) - len(post.failures):,} resolved, "
            f"{100*(len(pre.failures)-len(post.failures))/max(len(pre.failures),1):.1f}% fixed)")
    add("")

    add(bar)
    add("1. FAILURES BY CHECK TYPE")
    add(bar)
    add(f"  {'check':<24}{'count':>8}   severity   action taken")
    add("  " + "-" * 74)
    sev_action = {
        "warn": "logged, row kept",
        "quarantine": "row diverted, run continues",
        "fail": "RUN STOPPED",
    }
    seen_sev: dict[str, str] = {}
    for f in pre.failures:
        seen_sev.setdefault(f.check, f.severity)
    for check, count in sorted(pre.by_check().items(), key=lambda kv: -kv[1]):
        s = seen_sev[check]
        add(f"  {check:<24}{count:>8}   {s:<11}{sev_action[s]}")
    add("")

    add(bar)
    add("2. FAILURES BY COLUMN")
    add(bar)
    for col, count in sorted(pre.by_column().items(), key=lambda kv: -kv[1]):
        add(f"  {col:<24}{count:>8}")
    add("")

    add(bar)
    add("3. ROW-LEVEL DETAIL  (first 40; row index is the 0-based CSV row)")
    add(bar)
    add("  NOTE: offending values are shown as shapes (x=letter, 9=digit),")
    add("        never as raw values, because these columns hold PII.")
    add("")
    add(f"  {'row':>6}  {'column':<16}{'check':<22}detail")
    add("  " + "-" * 74)
    shown = 0
    for f in pre.failures:
        if f.row_index is None:
            continue
        shape = f"  [{f.value_shape}]" if f.value_shape else ""
        add(f"  {f.row_index:>6}  {f.column or '-':<16}{f.check:<22}{f.message}{shape}")
        shown += 1
        if shown >= 40:
            remaining = sum(1 for x in pre.failures if x.row_index is not None) - shown
            add(f"  ... and {remaining:,} more row-level failures "
                "(full list in logs/pipeline.log)")
            break
    add("")

    add(bar)
    add("4. WHAT HAPPENS NEXT")
    add(bar)
    add("  quarantine -> rows go to data/quarantine/ for manual review. They are")
    add("                NOT silently dropped and NOT silently fixed. Someone owns them.")
    add("  warn       -> cleaning will normalise these automatically.")
    add("  fail       -> pipeline halts; the source system must be fixed first.")
    add("")
    if post and post.failures:
        add("  REMAINING AFTER CLEANING (these could not be auto-fixed):")
        for check, count in sorted(post.by_check().items(), key=lambda kv: -kv[1]):
            add(f"    - {check}: {count}")
    add("")
    add(bar)
    add("END OF REPORT")
    add(bar)
    return "\n".join(L)
