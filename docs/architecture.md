# Architecture

```
data/raw/customers_raw.csv          (PII, gitignored, restricted access)
      |
      v
  ingestion/files.py     load as text; structural failures stop the run
      |
      +--> profiling/profiler.py    -> reports/data_quality_report.txt
      +--> pii/detector.py          -> reports/pii_detection_report.txt
      |
      v
  transformations/validation.py     pre-clean: measure
      |
      v
  transformations/cleaning.py       -> data/processed/customers_cleaned.csv
      |                             -> data/quarantine/duplicate_keys.csv
      |                             -> reports/cleaning_log.txt
      v
  transformations/validation.py     post-clean: prove  [QUALITY GATE]
      |
      v
  pii/masker.py                     -> data/processed/customers_masked.csv
      |                             -> reports/masked_sample.txt (restricted)
      v
  logs/manifest-<run_id>.json       lineage
```

## Where a person's data lives

Relevant to GDPR erasure requests. Deleting from `data/raw/` is not sufficient.

| Location | Findable by | Notes |
|---|---|---|
| `data/raw/customers_raw.csv` | `customer_id` | source of truth |
| `data/processed/customers_cleaned.csv` | `customer_id` | |
| `data/processed/customers_masked.csv` | recomputed salted hash | needs the salt |
| `data/quarantine/duplicate_keys.csv` | `customer_id` | |
| `reports/masked_sample.txt` | not indexed | contains raw example values |
| `logs/pipeline.log` | **not present** | logs row indexes only, never values |

**Erasure mechanism:** delete the record from raw, then reprocess. This is only
safe because the pipeline is idempotent (verified by checksum comparison across
two runs).

## Production gaps

- Salt must move to a secrets manager (AWS Secrets Manager / KMS).
- No retention policy on `data/raw/` — a live GDPR exposure.
- Quarantined rows have no owner or SLA.
- No storage of run metrics over time, so trends cannot be alerted on yet.
