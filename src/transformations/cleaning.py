"""
Cleaning: turn raw values into contract-compliant values.

THE RULE THAT GOVERNS THIS ENTIRE MODULE:
    every change is recorded, and nothing is ever silently altered or dropped.

A cleaning step that quietly fixes 300 values is indistinguishable from a
cleaning step that quietly corrupts 300 values. The audit trail is what makes
the difference, and it is also what lets you answer the question you WILL be
asked six months from now: "why does this customer's DOB say 1990-05-06?"

Three kinds of outcome, and they are not the same thing:
    FIXED       - value was repaired deterministically (format normalisation)
    FLAGGED     - value could not be repaired; the gap is recorded, row kept
    QUARANTINED - row cannot be made contract-compliant; diverted for a human
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.monitoring.logging import get_logger

log = get_logger("transformations.cleaning")

# Formats we will attempt, in priority order. The ambiguous pair is resolved
# by config, never by guessing.
UNAMBIGUOUS_FORMATS = ["%Y-%m-%d", "%d-%b-%Y", "%Y/%m/%d"]
AMBIGUOUS_FORMATS = {"day_first": "%d/%m/%Y", "month_first": "%m/%d/%Y"}

PLACEHOLDER = "UNKNOWN"


@dataclass
class Change:
    row_index: int | None
    column: str | None
    action: str          # normalised | placeholder | flagged | dropped | quarantined
    reason: str
    before_shape: str | None = None
    after_shape: str | None = None


@dataclass
class CleaningResult:
    rows_in: int = 0
    rows_out: int = 0
    changes: list[Change] = field(default_factory=list)
    quarantined: pd.DataFrame | None = None
    dropped_rows: int = 0
    ambiguous_dates: int = 0
    notes: list[str] = field(default_factory=list)

    def add(self, c: Change) -> None:
        self.changes.append(c)

    def counts(self) -> dict[str, int]:
        return dict(Counter(c.action for c in self.changes))

    def by_column(self) -> dict[str, Counter]:
        out: dict[str, Counter] = {}
        for c in self.changes:
            out.setdefault(c.column or "<row>", Counter())[c.action] += 1
        return out


def _shape(v: str) -> str:
    return re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "x", str(v)))[:24]


class Cleaner:
    def __init__(self, config: dict):
        self.cfg = config
        self.columns: dict[str, dict] = config["columns"]
        self.null_like = set(config.get("null_like_values", [""]))
        self.strategy: dict[str, str] = config.get("missing_strategy", {})
        self.date_policy: str = config.get("ambiguous_date_policy", "day_first")
        self.synonyms: dict[str, dict] = config.get("category_synonyms", {})

    # ------------------------------------------------------------------
    # value-level normalisers
    # ------------------------------------------------------------------
    def _norm_phone(self, v: str) -> str | None:
        """Strip everything that isn't a digit, then rebuild as NNN-NNN-NNNN."""
        digits = re.sub(r"\D", "", v)
        # drop a leading country code so 1-866-443-7557 and 866-443-7557 agree
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return None          # 555-CALL-NOW, extensions, truncated numbers
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

    def _norm_date(self, v: str) -> tuple[str | None, bool]:
        """Return (ISO date, was_ambiguous)."""
        v = v.strip()
        for fmt in UNAMBIGUOUS_FORMATS:
            try:
                return datetime.strptime(v, fmt).date().isoformat(), False
            except ValueError:
                continue

        # Slash-separated numeric dates: could be DD/MM or MM/DD.
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
        if m:
            a, b, _ = int(m.group(1)), int(m.group(2)), m.group(3)
            # If one part is > 12 the order is FORCED - not ambiguous at all.
            if a > 12:
                forced = "%d/%m/%Y"
            elif b > 12:
                forced = "%m/%d/%Y"
            else:
                forced = AMBIGUOUS_FORMATS[self.date_policy]
                try:
                    return datetime.strptime(v, forced).date().isoformat(), True
                except ValueError:
                    return None, False
            try:
                return datetime.strptime(v, forced).date().isoformat(), False
            except ValueError:
                return None, False
        return None, False

    def _norm_number(self, v: str, integer: bool = False) -> str | None:
        cleaned = v.replace(",", "").replace("$", "").strip()
        try:
            num = float(cleaned)
        except ValueError:
            return None
        if integer:
            # An id must stay an id. float() would turn 1029 into "1029.0",
            # which silently breaks every join and lookup downstream.
            if num != int(num):
                return None
            return str(int(num))
        return str(round(num, 2))

    def _norm_name(self, v: str) -> str:
        # Title Case, but keep O'Brien and Jean-Luc intact
        return re.sub(r"[A-Za-z]+('[A-Za-z]+)?",
                      lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(),
                      v.strip())

    def _norm_category(self, col: str, v: str) -> str:
        v = v.strip().lower()
        return self.synonyms.get(col, {}).get(v, v)

    # ------------------------------------------------------------------
    def clean(self, df: pd.DataFrame) -> tuple[pd.DataFrame, CleaningResult]:
        res = CleaningResult(rows_in=len(df))
        df = df.copy()
        df.index = range(len(df))          # stable row ids for the audit trail

        # ---- 1. whitespace + null-like normalisation ------------------
        for col in df.columns:
            for idx, raw in df[col].items():
                v = str(raw)
                stripped = v.strip()
                if stripped in self.null_like and stripped != "":
                    df.at[idx, col] = ""
                    res.add(Change(idx, col, "normalised",
                                   f"null-in-disguise '{stripped}' converted to empty"))
                elif v != stripped:
                    df.at[idx, col] = stripped
                    res.add(Change(idx, col, "normalised", "surrounding whitespace removed"))

        # ---- 2. per-column type normalisation -------------------------
        flag_columns: dict[str, list[bool]] = {}

        for col, rules in self.columns.items():
            if col not in df.columns:
                continue
            t = rules.get("type")
            fmt = rules.get("format")
            missing_flags = []

            for idx, raw in df[col].items():
                v = str(raw).strip()
                if v == "":
                    missing_flags.append(True)
                    continue
                missing_flags.append(False)

                new: str | None = v

                if fmt == "phone":
                    new = self._norm_phone(v)
                elif fmt == "email":
                    new = v.lower()
                elif t == "date":
                    new, ambiguous = self._norm_date(v)
                    if ambiguous:
                        res.ambiguous_dates += 1
                elif t in ("integer", "numeric"):
                    new = self._norm_number(v, integer=(t == "integer"))
                elif t == "category":
                    new = self._norm_category(col, v)
                elif rules.get("alphabetic"):
                    new = self._norm_name(v)

                if new is None:
                    # Unrepairable. Blank it and let the missing-value
                    # strategy decide the row's fate - do NOT guess a value.
                    res.add(Change(idx, col, "flagged",
                                   "value could not be normalised to the required "
                                   "format; cleared and flagged", _shape(v), None))
                    df.at[idx, col] = ""
                    missing_flags[-1] = True
                elif new != v:
                    res.add(Change(idx, col, "normalised",
                                   f"normalised to {t}/{fmt or 'standard'} form",
                                   _shape(v), _shape(new)))
                    df.at[idx, col] = new

            if self.strategy.get(col) == "flag":
                flag_columns[f"{col}_missing"] = missing_flags

        # ---- 3. missing-value strategy --------------------------------
        drop_mask = pd.Series(False, index=df.index)

        for col, strat in self.strategy.items():
            if col not in df.columns:
                continue
            empty = df[col].astype(str).str.strip() == ""
            n = int(empty.sum())
            if n == 0:
                continue

            if strat == "drop_row":
                drop_mask |= empty
                for idx in df.index[empty]:
                    res.add(Change(idx, col, "dropped",
                                   f"'{col}' is missing and the record is unusable "
                                   "without it"))
            elif strat == "placeholder":
                df.loc[empty, col] = PLACEHOLDER
                for idx in df.index[empty]:
                    res.add(Change(idx, col, "placeholder",
                                   f"substituted '{PLACEHOLDER}' - visibly not real, "
                                   "so it can never be mistaken for data"))
            elif strat == "flag":
                for idx in df.index[empty]:
                    res.add(Change(idx, col, "flagged",
                                   f"'{col}' left empty; absence recorded in "
                                   f"{col}_missing"))

        for name, values in flag_columns.items():
            df[name] = values

        if drop_mask.any():
            res.dropped_rows = int(drop_mask.sum())
            df = df[~drop_mask]
            res.notes.append(
                f"{res.dropped_rows} row(s) dropped for missing drop_row columns")

        # ---- 4. deduplication -----------------------------------------
        # 4a. exact duplicate rows: same load, same data - safe to remove
        before = len(df)
        dupes = df.duplicated(keep="first")
        for idx in df.index[dupes]:
            res.add(Change(idx, None, "dropped",
                           "exact duplicate of an earlier row"))
        df = df[~dupes]
        res.notes.append(f"{before - len(df)} exact duplicate row(s) removed")

        # 4b. duplicate keys: same customer_id, DIFFERENT data. This is NOT
        # safe to auto-resolve - you would be choosing which version of a
        # customer is true. Quarantine it for a human.
        key_dupes = df["customer_id"].duplicated(keep=False)
        quarantine = df[key_dupes].copy()
        for idx in df.index[key_dupes]:
            res.add(Change(idx, "customer_id", "quarantined",
                           "duplicate customer_id with differing data - a human "
                           "must decide which record is authoritative"))
        df = df[~key_dupes]
        res.quarantined = quarantine
        res.notes.append(f"{len(quarantine)} row(s) quarantined on duplicate keys")

        res.rows_out = len(df)
        log.info("cleaning: %d rows in -> %d rows out (%d dropped, %d quarantined)",
                 res.rows_in, res.rows_out, res.dropped_rows, len(quarantine))
        if res.ambiguous_dates:
            log.warning("%d ambiguous date(s) resolved using policy '%s' - "
                        "this is an assumption, not a fact", res.ambiguous_dates,
                        self.date_policy)
        return df.reset_index(drop=True), res


# --------------------------------------------------------------------------
def render_log(res: CleaningResult, policy: str) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78
    counts = res.counts()

    add(bar)
    add("CLEANING LOG - customers dataset")
    add(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add(bar)
    add("")
    add("EXECUTIVE SUMMARY")
    add("-" * 78)
    add(f"  Rows in ....................... {res.rows_in:,}")
    add(f"  Rows out ...................... {res.rows_out:,}")
    add(f"  Rows dropped .................. {res.dropped_rows:,}")
    add(f"  Rows quarantined .............. {len(res.quarantined) if res.quarantined is not None else 0:,}")
    add(f"  Retention rate ................ {100*res.rows_out/max(res.rows_in,1):.1f}%")
    add("")
    add("  Individual changes made:")
    for action, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        add(f"    {action:<14} {n:>7}")
    add("")

    if res.ambiguous_dates:
        add("  !! ASSUMPTION APPLIED")
        add(f"     {res.ambiguous_dates} date(s) were ambiguous (e.g. 05/06/1990 could")
        add(f"     be 5 June or 6 May). Resolved using policy '{policy}'.")
        add("     If the source system is actually month-first, these dates are")
        add("     WRONG and the error is invisible - the values still look valid.")
        add("     This assumption must be confirmed with the data owner.")
        add("")

    add(bar)
    add("1. CHANGES BY COLUMN AND ACTION")
    add(bar)
    actions = ["normalised", "placeholder", "flagged", "dropped", "quarantined"]
    add(f"  {'column':<20}" + "".join(f"{a:>13}" for a in actions))
    add("  " + "-" * 74)
    for col, c in sorted(res.by_column().items(), key=lambda kv: -sum(kv[1].values())):
        add(f"  {col:<20}" + "".join(f"{c.get(a, 0):>13}" for a in actions))
    add("")
    add("  normalised  = value repaired deterministically (format only)")
    add("  placeholder = substituted an obviously-fake value")
    add("  flagged     = could not repair; gap recorded, row kept")
    add("  dropped     = row removed (unusable, or an exact duplicate)")
    add("  quarantined = row diverted for human decision")
    add("")

    add(bar)
    add("2. ROW-LEVEL AUDIT TRAIL  (first 45 of "
        f"{len(res.changes):,}; full trail in logs/pipeline.log)")
    add(bar)
    add(f"  {'row':>6}  {'column':<18}{'action':<13}{'before':<14}{'after':<14}reason")
    add("  " + "-" * 74)
    for c in res.changes[:45]:
        add(f"  {str(c.row_index):>6}  {str(c.column or '-'):<18}{c.action:<13}"
            f"{str(c.before_shape or '-'):<14}{str(c.after_shape or '-'):<14}{c.reason}")
    add("")

    add(bar)
    add("3. NOTES")
    add(bar)
    for n in res.notes:
        add(f"  - {n}")
    add("")
    add("  NOT DONE AUTOMATICALLY, ON PURPOSE:")
    add("  - No missing date_of_birth or income was imputed. A fabricated DOB")
    add("    drives age and eligibility logic; a fabricated income drives credit")
    add("    decisions. Both would be invisible errors with real consequences.")
    add("  - Duplicate customer_ids were not merged. Choosing which version of a")
    add("    customer is 'true' is a business decision, not a code decision.")
    add("")
    add(bar)
    add("END OF LOG")
    add(bar)
    return "\n".join(L)
