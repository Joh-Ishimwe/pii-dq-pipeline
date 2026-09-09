"""
PII detection.

Three independent signals, deliberately kept separate:

  1. DECLARED  - what config/schema.yaml says the column holds.
                 Trustworthy, but only covers columns someone remembered to classify.
  2. NAME      - what the column is CALLED ("email", "dob", "ssn").
                 Fast, but lies: a column called `contact` could be anything,
                 and a column called `notes` could hold everything.
  3. CONTENT   - what the values actually LOOK like, tested with regex.
                 Slow, but it is the only signal that finds PII hiding in
                 free-text fields nobody classified.

Agreement between signals raises confidence. DISAGREEMENT is the interesting
case: content-PII in a column nobody declared as PII is exactly the leak
that ends up in a public export.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

from src.monitoring.logging import get_logger

log = get_logger("pii.detector")

# --------------------------------------------------------------------------
# Signal 2: column-name heuristics
# --------------------------------------------------------------------------
NAME_HINTS: dict[str, str] = {
    r"(first|last|full|given|sur|middle)[_\s]*name|^name$": "name",
    r"e[-_]?mail": "email",
    r"phone|mobile|cell|msisdn|tel(ephone)?": "phone",
    r"dob|date[_\s]*of[_\s]*birth|birth[_\s]*date|birthday": "date_of_birth",
    r"address|street|postcode|zip|city|district|sector": "address",
    r"ssn|social[_\s]*security|nid|national[_\s]*id|passport|nin": "national_id",
    r"income|salary|wage|revenue|balance|credit|iban|account[_\s]*number": "financial",
    r"ip[_\s]*address|device[_\s]*id|mac[_\s]*address": "device",
    r"gender|sex|race|ethnic|religio|health|diagnos|disab": "special_category",
}

# --------------------------------------------------------------------------
# Signal 3: content pattern detectors
# --------------------------------------------------------------------------
CONTENT_DETECTORS: dict[str, re.Pattern] = {
    # local-part @ domain . tld   -- deliberately permissive; we want RECALL here
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),

    # 10 digits with optional separators, optional country code, optional extension
    "phone": re.compile(
        r"(?:\+\d{1,3}[\s.\-]?)?"      # optional +250 / +1
        r"(?:\(\d{3}\)|\d{3})"          # (123) or 123
        r"[\s.\-]?\d{3}[\s.\-]?\d{4}"   # 456-7890
    ),

    # US SSN shape
    "national_id": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),

    # payment card: 13-19 digits, optionally spaced/dashed in groups of 4
    "credit_card": re.compile(r"\b(?:\d[ \-]?){13,19}\b"),

    # dates in any of the formats we know people use
    "date_of_birth": re.compile(
        r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}"
        r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
        r"|\d{1,2}-[A-Za-z]{3}-\d{4})\b"
    ),

    # street address: a house number followed by a street name, then either
    # a comma-separated locality ("123 Elm Mount, Kigali") or a recognized
    # street-type token ("123 Elm Street"). The comma form is deliberately
    # NOT a suffix whitelist - real-world street types are far too varied
    # (Faker alone uses well over a hundred of them: Mount, Ways, Mission,
    # Passage, Cove, ...) for an enumerated list to keep up.
    "address": re.compile(
        r"\b\d{1,6}\s+[\w'.-]+(?:\s+[\w'.-]+){0,4}\s*,\s*[A-Za-z][\w\s'-]{1,30}"
        r"|\b\d{1,5}\s+[\w\s]{2,30}\b"
        r"(?:street|st|road|rd|avenue|ave|lane|ln|way|drive|dr|blvd)\b",
        re.IGNORECASE,
    ),

    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

# --------------------------------------------------------------------------
# Risk model
# --------------------------------------------------------------------------
# How much damage does leaking ONE value of this type do?
# 1 = nuisance, 5 = life-altering. These numbers are a judgement call and
# should be agreed with Legal/Compliance, not invented by an engineer alone.
SENSITIVITY: dict[str, int] = {
    "name": 2,
    "email": 3,
    "phone": 3,
    "date_of_birth": 3,
    "address": 4,
    "financial": 4,
    "national_id": 5,
    "credit_card": 5,
    "special_category": 5,
    "device": 2,
    "ip_address": 2,
    "none": 0,
}

# Can this value point at ONE person by itself?
IDENTIFIABILITY: dict[str, str] = {
    "email": "direct",
    "phone": "direct",
    "national_id": "direct",
    "credit_card": "direct",
    "address": "direct",
    "name": "direct",
    "date_of_birth": "quasi",
    "financial": "sensitive-attribute",
    "special_category": "sensitive-attribute",
    "device": "quasi",
    "ip_address": "quasi",
}

MASKING_RECOMMENDATION: dict[str, str] = {
    "name": "partial mask - keep initial (John -> J***)",
    "email": "partial mask - keep first char + domain (j***@gmail.com)",
    "phone": "partial mask - keep last 4 (***-***-4567)",
    "date_of_birth": "generalize - keep year only (1985-**-**)",
    "address": "full redaction ([MASKED ADDRESS])",
    "financial": "generalize into bands (e.g. 50k-75k) or drop",
    "national_id": "tokenize or drop entirely - never partially mask",
    "credit_card": "tokenize - keep last 4 only, PCI-DSS rules apply",
    "special_category": "drop unless a specific legal basis exists",
    "device": "hash with a salt",
    "ip_address": "truncate final octet",
}

# Every declared class from config/schema.yaml that means "this column needs
# protection", as opposed to "none" (not personal data) or "low_risk" (kept
# as-is). Kept as one set so a new class only has to be added here once.
DECLARED_PII_CLASSES = {
    "direct", "quasi", "sensitive", "sensitive_qi", "sensitive_business", "identifier",
}

# Declared classes that make a column a quasi-identifier even when content
# scanning can't detect it directly (e.g. income: a plain number, or a
# category column - neither looks like anything a regex would flag).
QI_DECLARED_CLASSES = {"quasi", "sensitive_qi"}

SAMPLE_SIZE = 500  # rows sampled for content scanning; enough to be confident


def _match_column_name(col: str) -> str | None:
    c = col.lower()
    for pattern, pii_type in NAME_HINTS.items():
        if re.search(pattern, c):
            return pii_type
    return None


def _scan_content(values: list[str]) -> dict[str, dict]:
    """Run every content detector over a column's values. Returns hit stats."""
    hits: dict[str, dict] = {}
    non_empty = [v for v in values if str(v).strip()]
    if not non_empty:
        return hits

    for pii_type, pattern in CONTENT_DETECTORS.items():
        matched = [v for v in non_empty if pattern.search(str(v))]
        if matched:
            hits[pii_type] = {
                "match_count": len(matched),
                "match_rate": round(100 * len(matched) / len(non_empty), 1),
                # NOTE: examples are stored REDACTED. We never write raw PII
                # into a report that will be shared or committed.
                "example_shape": _shape(matched[0]),
            }
    return hits


def _shape(value: str) -> str:
    """Turn a value into a structural fingerprint. 'a@b.com' -> 'x@x.xxx'.

    Lets a reader verify the detector fired on the right thing WITHOUT
    the report itself becoming a PII leak.
    """
    out = re.sub(r"[A-Za-z]", "x", str(value))
    out = re.sub(r"\d", "9", out)
    return out[:40]


def detect_pii(df: pd.DataFrame, config: dict) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    row_count = len(df)

    for col in df.columns:
        declared = config["columns"].get(col, {}).get("pii", "unclassified")
        name_signal = _match_column_name(col)
        sample = df[col].head(SAMPLE_SIZE).astype(str).tolist()
        content_signals = _scan_content(sample)

        # Decide the column's PII type: name signal first (it is the most
        # semantically meaningful), then the strongest content signal.
        best_content = max(
            content_signals.items(),
            key=lambda kv: kv[1]["match_rate"],
            default=(None, None),
        )[0]
        pii_type = name_signal or best_content

        # Confidence: how many independent signals agree?
        agreeing = sum([
            declared in DECLARED_PII_CLASSES,
            name_signal is not None,
            bool(content_signals) and any(
                s["match_rate"] >= 50 for s in content_signals.values()
            ),
        ])
        confidence = {0: "none", 1: "low", 2: "medium", 3: "high"}[agreeing]

        populated = int((df[col].astype(str).str.strip() != "").sum())

        columns[col] = {
            "declared": declared,
            "name_signal": name_signal,
            "content_signals": content_signals,
            "pii_type": pii_type,
            "confidence": confidence,
            "is_pii": pii_type is not None and confidence != "none",
            "populated_records": populated,
            "sensitivity": SENSITIVITY.get(pii_type, 0),
            "identifiability": IDENTIFIABILITY.get(pii_type, "n/a"),
            "masking": MASKING_RECOMMENDATION.get(pii_type, "review with Legal"),
            "flags": [],
        }

        # The interesting disagreements
        if declared not in DECLARED_PII_CLASSES and pii_type:
            columns[col]["flags"].append(
                f"UNDECLARED PII: schema declares this column '{declared}' "
                f"(not requiring protection), but detection says it holds "
                f"'{pii_type}'. Schema may be wrong."
            )
        if declared in DECLARED_PII_CLASSES and not content_signals:
            columns[col]["flags"].append(
                "Declared as PII but no content pattern matched - verify manually."
            )
        for t, s in content_signals.items():
            if name_signal and t != name_signal and s["match_rate"] >= 20:
                columns[col]["flags"].append(
                    f"HIDDEN PII: '{t}' patterns found inside a column named "
                    f"for '{name_signal}' ({s['match_rate']}% of values)."
                )

    pii_cols = {c: v for c, v in columns.items() if v["is_pii"]}

    # ---- risk quantification -------------------------------------------
    # exposure = for each PII column, records held x how damaging a leak is
    exposure = sum(v["populated_records"] * v["sensitivity"] for v in pii_cols.values())
    max_sensitivity = max((v["sensitivity"] for v in pii_cols.values()), default=0)
    direct_ids = [c for c, v in pii_cols.items() if v["identifiability"] == "direct"]
    # Union two sources of quasi-identifier status: content detection (catches
    # what nobody declared) and the declared class (catches columns like
    # `income` - a plain number/category that no regex will ever flag, but
    # the schema owner knows narrows a row down when combined with others).
    quasi_ids = sorted({
        c for c, v in columns.items()
        if v["identifiability"] == "quasi" or v["declared"] in QI_DECLARED_CLASSES
    })

    # Can a row be pinned to one human? Test the classic quasi-identifier combo.
    reident = _reidentification_test(df)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "row_count": row_count,
        "column_count": len(df.columns),
        "columns": columns,
        "pii_column_count": len(pii_cols),
        "direct_identifiers": direct_ids,
        "quasi_identifiers": quasi_ids,
        "exposure_score": exposure,
        "max_sensitivity": max_sensitivity,
        "affected_individuals": row_count,
        "reidentification": reident,
        "risk_level": _risk_level(max_sensitivity, len(direct_ids), row_count),
    }


def _reidentification_test(df: pd.DataFrame) -> dict:
    """
    How many rows are UNIQUE on quasi-identifiers alone?

    This is the Sweeney test. If most rows are unique on
    (year of birth + city), then stripping names does not anonymize anything:
    anyone with a second dataset can join back to a real person.
    """
    if not {"date_of_birth", "address"}.issubset(df.columns):
        return {}

    year = df["date_of_birth"].astype(str).str.extract(r"(\d{4})")[0].fillna("?")
    city = df["address"].astype(str).str.split(",").str[-1].str.strip().str.lower()
    combo = year + "|" + city

    counts = combo.value_counts()
    unique_rows = int((combo.map(counts) == 1).sum())
    return {
        "quasi_combo": "birth year + city",
        "unique_rows": unique_rows,
        "unique_pct": round(100 * unique_rows / len(df), 1),
        "k_min": int(counts.min()) if len(counts) else 0,
    }


def _risk_level(max_sens: int, n_direct: int, rows: int) -> str:
    if max_sens >= 5 or (n_direct >= 3 and rows > 100):
        return "HIGH"
    if max_sens >= 3 or n_direct >= 1:
        return "MEDIUM"
    return "LOW"


# --------------------------------------------------------------------------
def render_report(r: dict) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78

    add(bar)
    add("PII DETECTION REPORT - customers_raw.csv")
    add(f"Generated: {r['generated_at']}")
    add("NOTE: this report contains NO actual personal data. Value examples are")
    add("shown as structural shapes only (x = letter, 9 = digit).")
    add(bar)
    add("")
    add("EXECUTIVE SUMMARY")
    add("-" * 78)
    add(f"  Overall risk level ............ {r['risk_level']}")
    add(f"  Individuals in this file ...... {r['affected_individuals']:,}")
    add(f"  Columns holding PII ........... {r['pii_column_count']} of {r['column_count']}")
    add(f"  Direct identifiers ............ {', '.join(r['direct_identifiers']) or 'none'}")
    add(f"  Quasi identifiers ............. {', '.join(r['quasi_identifiers']) or 'none'}")
    add(f"  Exposure score ................ {r['exposure_score']:,}"
        "   (records x sensitivity, summed)")
    add("")

    if r["reidentification"]:
        ri = r["reidentification"]
        add("  RE-IDENTIFICATION TEST")
        add(f"    Using only {ri['quasi_combo']} (no names, no email, no phone):")
        add(f"    {ri['unique_rows']} of {r['row_count']} rows ({ri['unique_pct']}%) are UNIQUE.")
        add(f"    Smallest group size (k) = {ri['k_min']}")
        if ri["unique_pct"] > 20:
            add("    -> Deleting the name columns would NOT anonymize this file.")
            add("       Anyone holding a second dataset could join back to real people.")
        add("")

    add("  BREACH IMPACT: a leak of this file exposes every listed individual's")
    add("  identity, contact details and financial position simultaneously. That")
    add("  combination enables identity theft and targeted fraud, not just spam.")
    add("  Under GDPR Art.33 a breach of this kind is reportable to the")
    add("  supervisory authority within 72 hours.")
    add("")

    add(bar)
    add("1. DETECTION MATRIX")
    add(bar)
    add(f"  {'column':<16}{'declared':<20}{'detected':<18}{'conf':<9}{'sens':<6}records")
    add("  " + "-" * 80)
    for col, v in r["columns"].items():
        add(f"  {col:<16}{v['declared']:<20}{str(v['pii_type'] or '-'):<18}"
            f"{v['confidence']:<9}{v['sensitivity']:<6}{v['populated_records']}")
    add("")
    add("  declared = what config/schema.yaml claims")
    add("  detected = what column-name + content scanning concluded")
    add("  conf     = how many of the 3 signals agreed (none/low/medium/high)")
    add("  sens     = damage if one value leaks (1 nuisance ... 5 life-altering)")
    add("")

    add(bar)
    add("2. COLUMN DETAIL")
    add(bar)
    for col, v in r["columns"].items():
        if not v["is_pii"]:
            continue
        add("")
        add(f"  {col.upper()}")
        add("  " + "-" * 74)
        add(f"    PII type        : {v['pii_type']}  ({v['identifiability']})")
        add(f"    confidence      : {v['confidence']}")
        add(f"    sensitivity     : {v['sensitivity']}/5")
        add(f"    records at risk : {v['populated_records']:,}")
        if v["content_signals"]:
            add("    content patterns matched:")
            for t, s in sorted(v["content_signals"].items(),
                               key=lambda kv: -kv[1]["match_rate"]):
                add(f"        - {t:<16} {s['match_count']:>4} hits "
                    f"({s['match_rate']:>5}%)   shape: {s['example_shape']}")
        add(f"    recommended     : {v['masking']}")
        for f in v["flags"]:
            add(f"    ! {f}")
    add("")

    add(bar)
    add("3. FLAGS REQUIRING HUMAN REVIEW")
    add(bar)
    any_flag = False
    for col, v in r["columns"].items():
        for f in v["flags"]:
            add(f"  [{col}] {f}")
            any_flag = True
    if not any_flag:
        add("  none")
    add("")

    add(bar)
    add("4. RECOMMENDED CONTROLS")
    add(bar)
    add("  BEFORE this data is shared with anyone outside the data team:")
    add("    1. Apply the masking listed per column above -> customers_masked.csv")
    add("    2. Keep data/raw/ out of version control and off shared drives")
    add("    3. Restrict raw-file access to named individuals (least privilege)")
    add("    4. Confirm logs contain row IDs only, never field values")
    add("    5. Agree a retention period; raw PII should not be kept indefinitely")
    add("    6. Have Legal confirm the lawful basis for holding income data")
    add("")
    add(bar)
    add("END OF REPORT")
    add(bar)
    return "\n".join(L)
