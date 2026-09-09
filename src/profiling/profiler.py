"""
Profiling = describing what IS in the data (open question).
Validation = checking data against rules (closed, pass/fail).  <- comes later.

Design note: profile_dataset() returns a plain dict of FACTS.
render_report() turns facts into text.
Keeping them apart means the same facts can later feed a dashboard,
a JSON metrics store, or a Slack alert without rewriting the maths.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.monitoring.logging import get_logger

log = get_logger("profiling.profiler")

# Date formats seen in the wild. Order matters: the first that parses wins.
DATE_FORMATS = {
    "%Y-%m-%d": "ISO (YYYY-MM-DD)",
    "%d/%m/%Y": "day-first (DD/MM/YYYY)",
    "%m/%d/%Y": "month-first (MM/DD/YYYY)",
    "%d-%b-%Y": "abbreviated month (DD-Mon-YYYY)",
    "%Y/%m/%d": "slashed ISO (YYYY/MM/DD)",
}

PHONE_PATTERNS = {
    r"^\d{3}-\d{3}-\d{4}$": "NNN-NNN-NNNN (target format)",
    r"^\(\d{3}\)\s*\d{3}-\d{4}$": "(NNN) NNN-NNNN",
    r"^\d{3}\.\d{3}\.\d{4}$": "NNN.NNN.NNNN",
    r"^\+\d{10,15}$": "+E164 style",
    r"^\d{10}$": "NNNNNNNNNN (no separators)",
    r"^\d{3}-\d{3}-\d{4}\s*(ext|x)\.?\s*\d+$": "with extension",
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _is_missing(value: str, null_like: set[str]) -> bool:
    """Missing is not just empty. 'N/A', 'unknown', '-' are nulls in disguise."""
    return value is None or str(value).strip() in null_like


def _try_parse_date(value: str) -> tuple[date | None, str | None]:
    v = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date(), fmt
        except ValueError:
            continue
    return None, None


def _try_parse_number(value: str) -> float | None:
    v = str(value).strip().replace(",", "").replace("$", "")
    try:
        return float(v)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# column-level profiling
# --------------------------------------------------------------------------
def profile_column(series: pd.Series, name: str, rules: dict, null_like: set[str]) -> dict:
    values = series.tolist()
    total = len(values)

    missing_mask = [_is_missing(v, null_like) for v in values]
    n_missing = sum(missing_mask)
    present = [str(v) for v, m in zip(values, missing_mask) if not m]

    prof: dict[str, Any] = {
        "declared_type": rules.get("type", "unknown"),
        "pii_class": rules.get("pii", "none"),
        "total_rows": total,
        "missing_count": n_missing,
        "missing_pct": round(100 * n_missing / total, 2) if total else 0.0,
        "present_count": len(present),
        "distinct_count": len(set(present)),
        "distinct_pct": round(100 * len(set(present)) / len(present), 2) if present else 0.0,
        "whitespace_padded": sum(1 for v in present if v != v.strip()),
        "min_length": min((len(v) for v in present), default=0),
        "max_length": max((len(v) for v in present), default=0),
        "issues": [],
    }

    declared = rules.get("type")

    if declared in ("integer", "numeric"):
        parsed = [_try_parse_number(v) for v in present]
        bad = [v for v, p in zip(present, parsed) if p is None]
        ok = [p for p in parsed if p is not None]
        prof["non_numeric_count"] = len(bad)
        prof["non_numeric_examples"] = bad[:3]
        if ok:
            prof["min_value"] = min(ok)
            prof["max_value"] = max(ok)
            prof["mean_value"] = round(sum(ok) / len(ok), 2)
        if "min" in rules:
            below = sum(1 for p in ok if p < rules["min"])
            if below:
                prof["issues"].append(f"{below} value(s) below minimum {rules['min']}")
        if "max" in rules:
            above = sum(1 for p in ok if p > rules["max"])
            if above:
                prof["issues"].append(f"{above} value(s) above maximum {rules['max']:,}")
        if bad:
            prof["issues"].append(
                f"{len(bad)} value(s) are not numeric, e.g. {bad[:2]}"
            )

    elif declared == "date":
        fmt_counter: Counter[str] = Counter()
        unparseable = []
        parsed_dates = []
        for v in present:
            d, fmt = _try_parse_date(v)
            if d is None:
                unparseable.append(v)
            else:
                fmt_counter[DATE_FORMATS[fmt]] += 1
                parsed_dates.append(d)
        prof["format_breakdown"] = fmt_counter.most_common()
        prof["unparseable_count"] = len(unparseable)
        prof["unparseable_examples"] = list(dict.fromkeys(unparseable))[:3]
        if parsed_dates:
            prof["min_value"] = min(parsed_dates).isoformat()
            prof["max_value"] = max(parsed_dates).isoformat()
            future = sum(1 for d in parsed_dates if d > date.today())
            if future:
                prof["issues"].append(f"{future} date(s) are in the future")
        if len(fmt_counter) > 1:
            prof["issues"].append(
                f"{len(fmt_counter)} different date formats mixed in one column"
            )
        if unparseable:
            prof["issues"].append(
                f"{len(unparseable)} value(s) cannot be parsed as a date, "
                f"e.g. {prof['unparseable_examples']}"
            )

    elif declared == "category":
        allowed = set(rules.get("allowed", []))
        counts = Counter(present)
        prof["value_counts"] = counts.most_common()
        illegal = {k: c for k, c in counts.items() if k not in allowed}
        # case-only differences are a CONSISTENCY defect, cheap to fix
        case_only = {k: c for k, c in illegal.items() if k.strip().lower() in allowed}
        truly_unknown = {k: c for k, c in illegal.items() if k.strip().lower() not in allowed}
        prof["illegal_values"] = illegal
        if case_only:
            prof["issues"].append(
                f"{sum(case_only.values())} row(s) differ only by casing/whitespace: "
                f"{sorted(case_only)}"
            )
        if truly_unknown:
            prof["issues"].append(
                f"{sum(truly_unknown.values())} row(s) use undefined categories: "
                f"{sorted(truly_unknown)}"
            )

    else:  # string
        if rules.get("format") == "email":
            bad = [v for v in present if not EMAIL_RE.match(v.strip())]
            prof["invalid_format_count"] = len(bad)
            prof["invalid_format_examples"] = bad[:3]
            if bad:
                prof["issues"].append(f"{len(bad)} value(s) are not valid email addresses")
        if rules.get("format") == "phone":
            fmt_counter = Counter()
            unmatched = []
            for v in present:
                s = v.strip()
                for pat, label in PHONE_PATTERNS.items():
                    if re.match(pat, s):
                        fmt_counter[label] += 1
                        break
                else:
                    unmatched.append(s)
            prof["format_breakdown"] = fmt_counter.most_common()
            prof["unparseable_count"] = len(unmatched)
            prof["unparseable_examples"] = unmatched[:3]
            if len(fmt_counter) > 1:
                prof["issues"].append(
                    f"{len(fmt_counter)} different phone formats mixed in one column"
                )
            if unmatched:
                prof["issues"].append(
                    f"{len(unmatched)} value(s) match no known phone format, "
                    f"e.g. {unmatched[:2]}"
                )
        if rules.get("alphabetic"):
            bad = [v for v in present if not v.replace(" ", "").replace("-", "").isalpha()]
            if bad:
                prof["issues"].append(
                    f"{len(bad)} value(s) contain non-alphabetic characters, e.g. {bad[:2]}"
                )
        lo, hi = rules.get("min_length"), rules.get("max_length")
        if lo is not None:
            short = sum(1 for v in present if len(v.strip()) < lo)
            if short:
                prof["issues"].append(f"{short} value(s) shorter than {lo} characters")
        if hi is not None:
            long_ = sum(1 for v in present if len(v.strip()) > hi)
            if long_:
                prof["issues"].append(f"{long_} value(s) longer than {hi} characters")

    if rules.get("required") and n_missing:
        prof["issues"].insert(0, f"{n_missing} missing value(s) in a REQUIRED column")

    if rules.get("unique"):
        dup = len(present) - len(set(present))
        prof["duplicate_value_count"] = dup
        if dup:
            dupes = [k for k, c in Counter(present).items() if c > 1]
            prof["duplicate_examples"] = dupes[:5]
            prof["issues"].insert(0, f"{dup} duplicate value(s) in a UNIQUE column")

    return prof


# --------------------------------------------------------------------------
# dataset-level and cross-column profiling
# --------------------------------------------------------------------------
def profile_cross_column(df: pd.DataFrame, null_like: set[str]) -> dict:
    """
    Checks that involve MORE THAN ONE column. Single-column profiling
    can never catch these, and they are where real-world logic errors hide.
    """
    findings: dict[str, Any] = {}

    dob = df["date_of_birth"].tolist()
    created = df["created_date"].tolist()

    born_after_signup = 0
    impossible_age = 0
    underage = 0
    ages = []

    for d_raw, c_raw in zip(dob, created):
        d, _ = _try_parse_date(d_raw) if not _is_missing(d_raw, null_like) else (None, None)
        c, _ = _try_parse_date(c_raw) if not _is_missing(c_raw, null_like) else (None, None)
        if d:
            age = (date.today() - d).days / 365.25
            ages.append(age)
            if age > 120:
                impossible_age += 1
            elif age < 18:
                underage += 1
        if d and c and d > c:
            born_after_signup += 1

    findings["born_after_account_created"] = born_after_signup
    findings["age_over_120"] = impossible_age
    findings["age_under_18"] = underage
    findings["age_min"] = round(min(ages), 1) if ages else None
    findings["age_max"] = round(max(ages), 1) if ages else None

    # Same human, different customer_id -> a duplicate that ID checks miss.
    key = (df["first_name"].str.strip().str.lower() + "|"
           + df["last_name"].str.strip().str.lower() + "|"
           + df["date_of_birth"].str.strip())
    person_dupes = sum(c - 1 for c in Counter(key.tolist()).values() if c > 1)
    findings["same_person_different_id"] = person_dupes

    # An 'active' account with no contact route is operationally useless.
    unreachable = sum(
        1 for s, e, p in zip(df["account_status"], df["email"], df["phone"])
        if str(s).strip().lower() == "active"
        and _is_missing(e, null_like) and _is_missing(p, null_like)
    )
    findings["active_but_unreachable"] = unreachable

    return findings


def profile_dataset(df: pd.DataFrame, config: dict) -> dict:
    null_like = set(config.get("null_like_values", [""]))
    log.info("profiling %d rows across %d columns", len(df), len(df.columns))

    columns = {
        name: profile_column(df[name], name, rules, null_like)
        for name, rules in config["columns"].items()
        if name in df.columns
    }

    exact_dupes = int(df.duplicated(keep="first").sum())
    total_cells = df.shape[0] * df.shape[1]
    missing_cells = sum(c["missing_count"] for c in columns.values())

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "row_count": len(df),
        "column_count": len(df.columns),
        "exact_duplicate_rows": exact_dupes,
        "total_cells": total_cells,
        "missing_cells": missing_cells,
        "overall_completeness_pct": round(100 * (1 - missing_cells / total_cells), 2),
        "columns": columns,
        "cross_column": profile_cross_column(df, null_like),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_report(profile: dict) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78

    all_issues = [
        (col, issue)
        for col, c in profile["columns"].items()
        for issue in c["issues"]
    ]
    # "none" (not personal data) and "low_risk" (a judgement call that it
    # needs no protection) are excluded on purpose - everything else in
    # config/schema.yaml's `pii:` taxonomy requires some form of protection.
    pii_cols = [
        c for c, v in profile["columns"].items()
        if v["pii_class"] not in ("none", "low_risk")
    ]

    add(bar)
    add("DATA QUALITY REPORT - customers_raw.csv")
    add(f"Generated: {profile['generated_at']}")
    add(bar)
    add("")
    add("EXECUTIVE SUMMARY  (read this if you read nothing else)")
    add("-" * 78)
    add(f"  Rows profiled ................ {profile['row_count']:,}")
    add(f"  Columns ...................... {profile['column_count']}")
    add(f"  Overall completeness ......... {profile['overall_completeness_pct']}%"
        f"  ({profile['missing_cells']:,} of {profile['total_cells']:,} cells missing)")
    add(f"  Exact duplicate rows ......... {profile['exact_duplicate_rows']}")
    add(f"  Distinct quality issues ...... {len(all_issues)}")
    add(f"  Columns containing PII ....... {len(pii_cols)} of {profile['column_count']}"
        f"  -> {', '.join(pii_cols)}")
    add("")
    add("  VERDICT: this dataset is NOT fit for analysis or sharing in its raw")
    add("  state. It requires cleaning (see cleaning_log.txt) and PII masking")
    add("  (see pii_detection_report.txt) before any downstream use.")
    add("")
    add("  For value distributions, correlations and interactive exploration -")
    add("  the generic statistics no contract check can express - see")
    add("  reports/standard_profile.html (scripts/run_profile.py).")
    add("")

    add(bar)
    add("1. COMPLETENESS BY COLUMN  (missing = blank, or a null-in-disguise")
    add("   such as 'N/A' / 'unknown' / '-')")
    add(bar)
    add(f"  {'column':<18}{'missing':>9}{'%':>9}{'distinct':>11}   PII class")
    add("  " + "-" * 74)
    for name, c in profile["columns"].items():
        add(f"  {name:<18}{c['missing_count']:>9}{c['missing_pct']:>9.2f}"
            f"{c['distinct_count']:>11}   {c['pii_class']}")
    add("")

    add(bar)
    add("2. COLUMN DETAIL")
    add(bar)
    for name, c in profile["columns"].items():
        add("")
        add(f"  {name.upper()}   [declared: {c['declared_type']}  |  PII: {c['pii_class']}]")
        add("  " + "-" * 74)
        add(f"    present / missing : {c['present_count']} / {c['missing_count']}"
            f" ({c['missing_pct']}%)")
        add(f"    distinct values   : {c['distinct_count']} ({c['distinct_pct']}% of present)")
        add(f"    value length      : {c['min_length']} to {c['max_length']} chars")
        if c["whitespace_padded"]:
            add(f"    whitespace padded : {c['whitespace_padded']} value(s)")
        if "min_value" in c:
            add(f"    range             : {c['min_value']} to {c['max_value']}")
        if "mean_value" in c:
            add(f"    mean              : {c['mean_value']:,}")
        if c.get("format_breakdown"):
            add("    formats found     :")
            for label, count in c["format_breakdown"]:
                add(f"        - {label:<38} {count:>5}")
        if c.get("value_counts"):
            add("    value counts      :")
            for val, count in c["value_counts"]:
                flag = "  <-- NOT ALLOWED" if val in c.get("illegal_values", {}) else ""
                shown = repr(val) if val.strip() != val or not val else val
                add(f"        - {shown:<38} {count:>5}{flag}")
        if c["issues"]:
            add("    ISSUES:")
            for i in c["issues"]:
                add(f"        ! {i}")
        else:
            add("    ISSUES: none found")
    add("")

    add(bar)
    add("3. CROSS-COLUMN CHECKS  (defects invisible to single-column profiling)")
    add(bar)
    x = profile["cross_column"]
    add(f"  Born AFTER account was created ....... {x['born_after_account_created']}"
        "   (impossible; suggests a date-format mix-up)")
    add(f"  Implied age over 120 ................. {x['age_over_120']}")
    add(f"  Implied age under 18 ................. {x['age_under_18']}"
        "   (may be a legal problem, not just a data one)")
    add(f"  Age range observed ................... {x['age_min']} to {x['age_max']} years")
    add(f"  Same name + DOB, different customer_id {x['same_person_different_id']}"
        "   (duplicate PEOPLE, not duplicate IDs)")
    add(f"  'active' but no email AND no phone ... {x['active_but_unreachable']}")
    add("")

    add(bar)
    add("4. ALL ISSUES, CONSOLIDATED")
    add(bar)
    for col, issue in all_issues:
        add(f"  [{col}] {issue}")
    add("")
    add(bar)
    add("END OF REPORT")
    add(bar)
    return "\n".join(L)
