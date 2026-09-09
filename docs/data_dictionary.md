# Data dictionary — customers

| Column | Type | Rules | PII class | Masking |
|---|---|---|---|---|
| `customer_id` | integer | unique, positive | identifier | salted hash → `CUST_xxxxxxxxxxxx` |
| `first_name` | string | 2–50 chars, alphabetic | direct | initial only |
| `last_name` | string | 2–50 chars, alphabetic | direct | initial only |
| `email` | string | valid email format | direct | `j***@domain` |
| `phone` | string | `NNN-NNN-NNNN` after cleaning | direct | `***-***-4567` |
| `date_of_birth` | date | `YYYY-MM-DD`, age 18–120 | quasi | year only |
| `address` | string | 10–200 chars | direct | full redaction |
| `income` | numeric | 0 – 10,000,000 | sensitive_qi | 25k bands |
| `account_status` | category | active / inactive / suspended | sensitive_business | `[ACCESS CONTROLLED]` |
| `created_date` | date | `YYYY-MM-DD` | low_risk | unchanged |

Cleaning adds `<column>_missing` boolean flags for columns with the `flag`
strategy. These are internal bookkeeping and are dropped from the masked export.

## Quasi-identifiers

`date_of_birth` + `address` (birth year + city) alone identify ~48% of
individuals uniquely before any cleaning or masking - see
`pii_detection_report.txt`. After masking, residual risk is measured again on
`date_of_birth` + `income` + `account_status`: ~56.5% of rows are still
unique (`masked_sample.txt`). That's down from ~71% before `account_status`
was reclassified `sensitive_business` and moved to `access_control` masking -
concrete evidence that removing a quasi-identifier from a shared extract
measurably reduces re-identification risk. Any further change to masking
rules must be re-measured, not assumed.
