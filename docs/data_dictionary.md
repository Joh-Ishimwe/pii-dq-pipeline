# Data dictionary — customers

| Column | Type | Rules | PII class | Masking |
|---|---|---|---|---|
| `customer_id` | integer | unique, positive | none (but a linking key) | salted hash → `CUST_xxxxxxxxxxxx` |
| `first_name` | string | 2–50 chars, alphabetic | direct | initial only |
| `last_name` | string | 2–50 chars, alphabetic | direct | initial only |
| `email` | string | valid email format | direct | `j***@domain` |
| `phone` | string | `NNN-NNN-NNNN` after cleaning | direct | `***-***-4567` |
| `date_of_birth` | date | `YYYY-MM-DD`, age 18–120 | quasi | year only |
| `address` | string | 10–200 chars | direct | full redaction |
| `income` | numeric | 0 – 10,000,000 | sensitive | 25k bands |
| `account_status` | category | active / inactive / suspended | none | unchanged |
| `created_date` | date | `YYYY-MM-DD` | none | unchanged |

Cleaning adds `<column>_missing` boolean flags for columns with the `flag`
strategy. These are internal bookkeeping and are dropped from the masked export.

## Quasi-identifiers

`date_of_birth` and `address` alone identify ~51% of individuals uniquely. After
masking, `date_of_birth` + `income` band + `account_status` identify ~71%. Any
change to masking rules must be re-measured, not assumed.
