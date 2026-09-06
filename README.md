# PII Detection & Data Quality Validation Pipeline

Profiles a raw customer file, detects personal data, validates it against a
contract, cleans it, masks it, and reports on everything it did.

## Run it

```bash
pip install -r requirements.txt
export PII_PSEUDONYM_SALT="$(openssl rand -hex 32)"   # required for real extracts
python scripts/generate_raw_data.py     # synthetic messy input
python scripts/run_pipeline.py          # the whole thing
```

Exit codes: `0` ok · `1` input/config problem · `2` quality gate failed · `3` bug.

Individual stages can also be run alone: `scripts/run_profiling.py`,
`run_pii_detection.py`, `run_validation.py`, `run_cleaning.py`, `run_masking.py`.

## Pipeline order

```
Load (as text) → Profile → Detect PII → Validate (measure)
              → Clean → Validate (prove) → Mask → Save + Report
```

Validation runs twice on purpose: before cleaning to measure how bad the input
is, after cleaning to prove the cleaning worked.

## Outputs

| File | What it is |
|---|---|
| `reports/data_quality_report.txt` | profiling: completeness, formats, cross-column checks |
| `reports/pii_detection_report.txt` | which columns hold personal data, and the risk |
| `reports/validation_results.txt` | rule failures before and after cleaning |
| `reports/cleaning_log.txt` | every change made, with a reason |
| `reports/masked_sample.txt` | before/after evidence — **restricted, not committed** |
| `reports/pipeline_execution_report.txt` | run timeline, metrics, outputs |
| `reports/reflection.md` | written analysis |
| `data/processed/customers_cleaned.csv` | cleaned, contract-compliant |
| `data/processed/customers_masked.csv` | pseudonymized extract for sharing |
| `data/quarantine/duplicate_keys.csv` | rows needing a human decision |
| `logs/manifest-<run_id>.json` | lineage: source, outputs, metrics, assumptions |

## Configuration

`config/schema.yaml` holds the column contract, severity policy, missing-value
strategy, masking rules and the ambiguous-date policy. Changing a rule is a
config edit, not a code change.

## Warnings

- `data/raw/` and `reports/masked_sample.txt` contain personal data and are
  gitignored. Keep them that way.
- `customers_masked.csv` is **pseudonymized, not anonymized** — 71% of rows
  remain uniquely identifiable on quasi-identifiers. GDPR still applies.
- Without `PII_PSEUDONYM_SALT` set, pseudonyms are reversible by brute force.
  The pipeline warns; do not ignore it.
