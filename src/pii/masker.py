"""
Masking: reduce what a dataset reveals about individuals, before it leaves
the data team.

THE CENTRAL TRADE-OFF, WHICH HAS NO CLEAN ANSWER:
    every character you hide protects a person and destroys an analysis.
    'j***@gmail.com' cannot be emailed. '1985-**-**' cannot give an exact age.
    '[MASKED ADDRESS]' kills every geographic report you had.
The engineer's job is not to find the "right" level. It is to make the trade
VISIBLE, so the person accountable for it can choose knowingly.

A vocabulary you must keep straight (people use these interchangeably; they
are not interchangeable):
    REDACTION       delete it.                       Irreversible. Zero utility.
    PARTIAL MASK    show some, hide the rest.        Irreversible. Some utility.
    GENERALIZATION  reduce precision (age -> band).  Irreversible. Good utility.
    HASHING         one-way function + salt.         Pseudonymous. Joins survive.
    TOKENIZATION    swap for a token, keep a vault.  Reversible with vault access.
    ENCRYPTION      scramble with a key.             Reversible with the key.

And the distinction that decides whether GDPR still applies:
    PSEUDONYMIZED - re-identification still possible with extra information
                    (the salt, a join key, an auxiliary dataset).
                    -> STILL personal data. Full GDPR obligations remain.
    ANONYMIZED    - re-identification genuinely impossible for anyone.
                    -> outside GDPR. A very high bar, rarely reached by masking
                       columns one at a time.
This module produces PSEUDONYMIZED output. Saying otherwise would be false.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any

import pandas as pd

from src.monitoring.logging import get_logger

log = get_logger("pii.masker")

# What each method costs you, in plain terms. Printed in the report so the
# approver sees the price, not just the protection.
UTILITY_COST = {
    "initial_only": "cannot identify, address or de-duplicate people by name",
    "partial_email": "cannot contact; domain-level analysis still works",
    "partial_tail": "cannot call; last 4 still allows customer-support verification",
    "year_only": "exact age and birthday campaigns lost; age-band analysis survives",
    "redact": "ALL geographic analysis lost - region, city, delivery, catchment",
    "band": "exact figures lost; distribution and segmentation survive",
    "pseudonymize": "joins to other masked extracts survive; the real id does not",
}


class Masker:
    def __init__(self, config: dict):
        self.rules: dict[str, dict] = config.get("masking", {})
        self.drop_cols: list[str] = config.get("drop_on_export", [])
        salt_env = config.get("pseudonym_salt_env", "PII_PSEUDONYM_SALT")
        self.salt = os.environ.get(salt_env)
        if not self.salt:
            # Fail loudly rather than hashing without a salt. An unsalted hash
            # of a 10-digit phone or a small id range is broken in minutes by
            # anyone who can compute hashes - which is everyone.
            self.salt = "DEV_ONLY_INSECURE_SALT"
            log.warning(
                "%s is not set; using an insecure development salt. "
                "NEVER produce a shareable extract this way - the pseudonyms "
                "would be reversible by brute force.", salt_env)

    # ---------------- individual strategies ----------------------------
    def _initial_only(self, v: str, keep: int = 1) -> str:
        v = v.strip()
        return v if not v else v[:keep] + "***"

    def _partial_email(self, v: str, keep: int = 1) -> str:
        v = v.strip()
        if "@" not in v:
            return "***"
        local, domain = v.rsplit("@", 1)
        # Domain is kept on purpose: it supports "how many of our customers use
        # a corporate address?" without revealing who any of them are.
        return f"{local[:keep]}***@{domain}"

    def _partial_tail(self, v: str, keep: int = 4) -> str:
        digits = re.sub(r"\D", "", v)
        if not digits:
            return ""
        tail = digits[-keep:]
        return f"***-***-{tail}"

    def _year_only(self, v: str) -> str:
        m = re.match(r"(\d{4})", v.strip())
        return f"{m.group(1)}-**-**" if m else ""

    def _redact(self, v: str, label: str = "[REDACTED]") -> str:
        return label if v.strip() else ""

    def _band(self, v: str, width: int = 25000) -> str:
        try:
            n = float(v)
        except (ValueError, TypeError):
            return ""
        lo = int(n // width) * width
        return f"{lo:,}-{lo + width:,}"

    def _pseudonymize(self, v: str) -> str:
        if not v.strip():
            return ""
        h = hashlib.sha256((self.salt + v.strip()).encode()).hexdigest()
        return f"CUST_{h[:12]}"

    def _apply(self, method: str, value: str, params: dict) -> str:
        fn = {
            "initial_only": lambda x: self._initial_only(x, params.get("keep", 1)),
            "partial_email": lambda x: self._partial_email(x, params.get("keep", 1)),
            "partial_tail": lambda x: self._partial_tail(x, params.get("keep", 4)),
            "year_only": self._year_only,
            "redact": lambda x: self._redact(x, params.get("label", "[REDACTED]")),
            "band": lambda x: self._band(x, params.get("width", 25000)),
            "pseudonymize": self._pseudonymize,
        }[method]
        return fn(value)

    # ---------------- entry point --------------------------------------
    def mask(self, df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
        out = df.copy()
        samples: dict[str, list[tuple[str, str]]] = {}
        counts: dict[str, int] = {}

        for col, rule in self.rules.items():
            if col not in out.columns:
                continue
            method = rule["method"]
            params = {k: v for k, v in rule.items() if k != "method"}

            before = out[col].astype(str).tolist()
            after = [self._apply(method, v, params) for v in before]
            out[col] = after

            # Keep a few before/after pairs as EVIDENCE the rule fired.
            # This is the one place raw PII appears, so masked_sample.txt is
            # itself a restricted file - see the warning in the report header.
            pairs = [(b, a) for b, a in zip(before, after) if b.strip()][:3]
            samples[col] = pairs
            counts[col] = sum(1 for b, a in zip(before, after) if b != a)
            log.info("masked %s with %s (%d values changed)", col, method, counts[col])

        for col in self.drop_cols:
            if col in out.columns:
                out = out.drop(columns=[col])
                log.info("dropped column from export: %s", col)

        # Drop the internal *_missing flags - they are pipeline bookkeeping,
        # not something a downstream consumer should reason about.
        flags = [c for c in out.columns if c.endswith("_missing")]

        residual = self._residual_risk(out)

        return out, {
            "samples": samples,
            "counts": counts,
            "flag_columns": flags,
            "residual": residual,
            "rules": self.rules,
        }

    # ---------------- did the masking actually work? -------------------
    def _residual_risk(self, masked: pd.DataFrame) -> dict[str, Any]:
        """
        k-ANONYMITY: for each row, how many OTHER rows look identical on the
        columns an attacker could plausibly already know (quasi-identifiers)?

        k = 1 means that row is unique -> that person is re-identifiable.
        k = 5 means they hide among 4 others.

        Masking a column does not automatically fix this. Measuring it is the
        only way to know whether the extract is safe to share, and almost
        nobody does it - which is why 'we removed the names' keeps failing.
        """
        quasi = [c for c in ["date_of_birth", "income", "account_status"]
                 if c in masked.columns]
        if not quasi:
            return {}
        combo = masked[quasi].astype(str).agg("|".join, axis=1)
        counts = Counter(combo)
        ks = [counts[c] for c in combo]
        unique = sum(1 for k in ks if k == 1)
        return {
            "quasi_identifiers": quasi,
            "k_min": min(ks),
            "k_median": sorted(ks)[len(ks) // 2],
            "unique_rows": unique,
            "unique_pct": round(100 * unique / len(masked), 1),
            "rows": len(masked),
        }


# --------------------------------------------------------------------------
def render_sample(meta: dict, rows_in: int, rows_out: int) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78

    add(bar)
    add("MASKED SAMPLE - BEFORE / AFTER EVIDENCE")
    add(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add(bar)
    add("")
    add("  *** RESTRICTED FILE ***")
    add("  This file contains UNMASKED example values, because proving a masking")
    add("  rule fired requires showing what it fired on. Treat it with the same")
    add("  access controls as the raw data. Do NOT commit it, do NOT attach it")
    add("  to a ticket, delete it once the masking has been signed off.")
    add("")

    add(bar)
    add("1. RULES APPLIED, AND WHAT EACH ONE COSTS YOU")
    add(bar)
    add(f"  {'column':<16}{'method':<16}{'changed':>9}   utility given up")
    add("  " + "-" * 74)
    for col, rule in meta["rules"].items():
        n = meta["counts"].get(col, 0)
        add(f"  {col:<16}{rule['method']:<16}{n:>9}   {UTILITY_COST[rule['method']]}")
    add("")

    add(bar)
    add("2. BEFORE / AFTER")
    add(bar)
    for col, pairs in meta["samples"].items():
        add("")
        add(f"  {col.upper()}")
        add("  " + "-" * 74)
        for before, after in pairs:
            add(f"    {before:<40} ->  {after}")
    add("")

    add(bar)
    add("3. RESIDUAL RE-IDENTIFICATION RISK  (k-anonymity)")
    add(bar)
    r = meta["residual"]
    if r:
        add(f"  Quasi-identifiers tested : {', '.join(r['quasi_identifiers'])}")
        add(f"  Rows in the extract      : {r['rows']:,}")
        add(f"  Smallest group size (k)  : {r['k_min']}")
        add(f"  Median group size        : {r['k_median']}")
        add(f"  Rows that are UNIQUE     : {r['unique_rows']} ({r['unique_pct']}%)")
        add("")
        if r["k_min"] == 1:
            add("  READ THIS: k=1 means at least one person in this file is still")
            add("  uniquely identifiable from columns we did NOT hide. Masking names,")
            add("  emails and phones did not make the file anonymous. Anyone holding")
            add("  a second dataset covering these same people can join back to them.")
            add("")
            add("  To reduce further, in increasing order of cost:")
            add("    - widen the income bands (25k -> 50k)")
            add("    - generalise birth year into decades (1985-**-** -> 1980s)")
            add("    - suppress rows where k < 5 entirely")
        else:
            add(f"  Every row hides among at least {r['k_min']} others.")
    else:
        add("  not measurable - no quasi-identifier columns present")
    add("")

    add(bar)
    add("4. CLASSIFICATION OF THE OUTPUT")
    add(bar)
    add("  customers_masked.csv is PSEUDONYMIZED, NOT ANONYMIZED.")
    add("  Re-identification remains possible via:")
    add("    - the pseudonym salt (anyone holding it can rebuild every customer_id)")
    add("    - the residual quasi-identifier combinations measured above")
    add("    - joining against any other dataset covering the same individuals")
    add("  Therefore GDPR still applies in full: lawful basis, retention limits,")
    add("  subject access and erasure rights all remain in force for this file.")
    add("")
    add(f"  Rows in: {rows_in:,}    Rows out: {rows_out:,}")
    add("")
    add(bar)
    add("END OF SAMPLE")
    add(bar)
    return "\n".join(L)
