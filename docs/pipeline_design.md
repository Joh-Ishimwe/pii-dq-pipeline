# Pipeline design decisions

## 1. Why validation runs twice

The brief lists profiling → PII → validate → clean, but Part 6 orders it
clean → validate. Both are right, for different reasons:

- **Pre-clean validation is a MEASUREMENT.** How bad is the input? Produces the
  baseline.
- **Post-clean validation is a VERDICT.** Did cleaning work? A failure here means
  the cleaning code is broken.

## 2. Two contracts, one engine

Validating raw data against the cleaned-data contract quarantined 100% of rows —
a phone as `(373) 474-5298` is not a defect when the next step normalises it.

- **Ingest contract** — what may ARRIVE. Loose: structure, keys, impossible values.
- **Warehouse contract** — what may be LOADED. Strict: normalised formats.

Implemented as `FIXABLE_BY_CLEANING` in `validation.py`: those checks downgrade
to `warn` at the pre-clean stage and are fully enforced post-clean. The same
rule is legitimately a warning in one place and an error in another.

The circuit breaker follows the same logic — it is post-clean only. Pre-clean, a
high failure rate on a known-messy file is the baseline, not a regression.

## 3. "Required" means present OR explicitly accounted for

The contract said `email: required`; the cleaning policy said `email: flag`.
Post-clean validation tripped at 82.8% on the contradiction. Resolved by
splitting the check:

- `required_missing` — missing and unaccounted for → quarantine
- `required_missing_recorded` — missing, with a `<col>_missing` flag set → warn

## 4. Load as text, convert deliberately

`dtype=str, keep_default_na=False, na_values=[]`. Type inference destroys
evidence: leading zeros, `"N/A"` vs blank, phone numbers becoming integers.
Conversion happens in cleaning, where every failure is logged.

## 5. Blocking vs non-blocking steps

Profiling and PII detection are non-blocking — losing insight must not stop data
from being processed. Cleaning and masking are blocking. A masking failure is
logged as a **security event**, because unmasked output already exists on disk
at that point.

## 6. Known assumptions

- `ambiguous_date_policy: day_first` — affects ~60 rows per run. Unconfirmed
  with the data owner. Recorded in every manifest.
- `pending` in `account_status` is not mapped to any allowed value; it may
  indicate a source-system state the warehouse model does not know about.
