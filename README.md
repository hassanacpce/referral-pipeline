# Referral Program Pipeline

Data profiling and a Pandas pipeline that models a gym-membership referral program and flags potentially fraudulent referral rewards.

**Output:** `output/referral_report.csv` – one row per referral (**46 rows**, 22 columns, no nulls) with an `is_business_logic_valid` flag.

---

## 1. Project layout

```
.
├── src/
│   ├── data_profiling.py        # profiles every source table (null count, distinct count, min/max, ...)
│   └── referral_pipeline.py     # load -> clean -> join -> time zones -> fraud rules -> report
├── data/raw/                    # the 7 source CSV files
├── output/                      # generated reports (mounted out of the container)
│   ├── referral_report.csv                # THE report (46 rows)
│   ├── referral_validation_details.csv    # audit trail: why each row is valid / invalid
│   └── data_profiling_report.csv / .xlsx  # profiling results for all tables
├── docs/data_dictionary.xlsx    # business-user data dictionary (for non-technical readers)
├── Dockerfile  docker-compose.yml  requirements.txt
└── README.md
```

## 2. Run with Docker (recommended)

Prerequisite: Docker installed and running.

```bash
# 1. Build the image
docker build -t referral-pipeline .

# 2. Run it. The -v flag maps ./output on your machine to /app/output in the container,
#    so the reports are stored OUTSIDE the container and survive after it exits.
#    Linux / macOS
docker run --rm -v "$(pwd)/output:/app/output" referral-pipeline
#    Windows PowerShell
docker run --rm -v "${PWD}/output:/app/output" referral-pipeline
#    Windows cmd.exe
docker run --rm -v "%cd%/output:/app/output" referral-pipeline
```

Or with Compose: `docker compose up --build`

The container runs profiling first, then the pipeline. Check `./output/` afterwards.

**Using your own CSV files:** mount them over the baked-in data (file names must stay the same):

```bash
docker run --rm -v "$(pwd)/my_csvs:/app/data/raw:ro" -v "$(pwd)/output:/app/output" referral-pipeline
```

**Run only one step:**

```bash
docker run --rm -v "$(pwd)/output:/app/output" referral-pipeline python src/data_profiling.py
docker run --rm -v "$(pwd)/output:/app/output" referral-pipeline python src/referral_pipeline.py
```

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `data/raw` (`/app/data/raw` in Docker) | Folder containing the source CSVs |
| `OUTPUT_DIR` | `output` (`/app/output` in Docker) | Where reports are written |
| `DEFAULT_TZ` | `Asia/Jakarta` | Last-resort time zone (see §4.3). Override with `-e DEFAULT_TZ=...` |

No credentials are used or required anywhere. Reports are written to local disk only (no cloud upload).

## 3. Run locally without Docker

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                       # Python 3.11+
python src/data_profiling.py
python src/referral_pipeline.py
```

## 4. How the pipeline works

### 4.1 Cleaning
* The CSVs use the literal text `null` for missing values → converted to real nulls.
* Timestamps parsed as UTC; booleans parsed; `reward_value` (`"10 days"`) → integer `num_reward_days`.
* **De-duplication before joining** (otherwise the 46 referrals would multiply):

| Table | Problem found | Rule applied |
|---|---|---|
| `user_logs` | same user repeated up to 13× (identical snapshots) | keep the latest row per `user_id` |
| `lead_logs` | same lead repeated 4× with changing `current_status` | keep the latest row per `lead_id` |
| `user_referral_logs` | one referral has 19 log rows | keep the latest log per referral |

The script asserts that the join result still has exactly one row per `referral_id`.

### 4.2 Joins (spine = `user_referrals`, all LEFT joins)
`referral_status` ← `user_referral_statuses` · `num_reward_days` ← `referral_rewards` · reward-granted flag ← `user_referral_logs` · referrer name/phone/club/zone/membership/deleted ← `user_logs` (on `referrer_id`) · transaction fields ← `paid_transactions` · lead category ← `lead_logs` (**only** when `referral_source = 'Lead'`, as the ERD note says).

### 4.3 Time zones (all source timestamps are UTC)
* `transaction_at` → the transaction's own `timezone_transaction`.
* `referral_at`, `updated_at`, `reward_granted_at` have no zone of their own, so a zone is borrowed in this order: **referrer's `timezone_homeclub` → lead's `timezone_location` → transaction zone → `DEFAULT_TZ`**. In the supplied data: 29 rows use the referrer's zone, 10 the transaction's, 3 the lead's and 4 the default.
* "Transaction after/before referral" is evaluated on the real UTC instants (time-zone safe) at full precision; "same month" is evaluated on local calendar months.

### 4.4 Formatting rules
* **Initcap** on text values (names, source, status, type, category) – implemented to match Spark `initcap` (not pandas `.title()`, which would wrongly capitalise letters after digits in IDs). **Club names (`referrer_homeclub`, `transaction_location`) keep UPPER CASE**; IDs and phone numbers are left untouched.
* **No nulls in the output:** missing text/date values → `Unknown`; missing `num_reward_days` → `0`. Rules are evaluated on the real nulls *before* they are filled.
* `referral_source_category`: `User Sign Up`→Online, `Draft Transaction`→Offline, `Lead`→`lead_logs.source_category`.

### 4.5 Fraud / business-logic rules → `is_business_logic_valid`
Implemented exactly as written in the brief (Valid 1 & 2, Invalid 1–5). Decisions where the brief is silent:

| Topic | Decision |
|---|---|
| Rules overlap | **Invalid wins.** A row is True only if it meets Valid 1 or 2 **and** trips no Invalid rule. |
| Matches no rule | False (cannot be proven valid). |
| "Membership not expired" | Checked **as of the referral date**. Unknown referrer → not confirmed → fails. |
| "Has a PAID transaction" | The `transaction_id` must exist in `paid_transactions`. An ID with no matching record counts as not PAID. |
| "Reward granted" | Latest `user_referral_logs` row for the referral has `is_reward_granted = TRUE`. |
| `reward_granted_at` | `created_at` of that "granted" log; `Unknown` if none. |
| `referral_details_id` | A generated 1..46 counter ordered by referral time. The log table can't supply it (only 7 of 46 referrals have logs). |

## 5. Results on the supplied data

* **46 rows** → **24 valid, 22 invalid.** Every row has a plain-English `validation_reason` in `referral_validation_details.csv`.
* All 24 valid rows are "Valid 2" (pending/failed, no reward). **No row can satisfy "Valid 1"**: not one referral in `user_referrals` has an `is_reward_granted = TRUE` log. All 17 "granted" logs belong to referral IDs that do not exist in `user_referrals`.
* Invalid breakdown: 8 paid transaction but no reward · 3 successful but no reward · 3 transaction before referral · 3 successful and paid, but the reward was never granted · 3 reward on non-successful referral (one also has no transaction) · 2 successful with a reward but the transaction isn't in `paid_transactions`.

### Additional warning signals (informational – they do **not** change the flag)
Found while profiling and exposed as `flag_*` columns in `referral_validation_details.csv`:
* **19** referrals point to a `transaction_id` that does not exist in `paid_transactions`.
* **9** referrals share a transaction with another referral (one transaction is credited to up to 4 referrals).
* **9** referrals involve a referee/lead referred more than once (one lead referred 4 times by different referrers); **16** share a `referee_phone` with another referral (one phone number appears on 10).
* **17** referrals have an unknown referrer.
* **71 of 96** `user_referral_logs` rows reference referral IDs missing from `user_referrals` (orphan logs).

## 6. Notes & limitations
* Names/phones in the source are anonymised hashes, so Initcap affects them only where the hash starts with a letter (e.g. `Ec55b0c6…`). This is expected.
* The pipeline uses **Pandas** (permitted by the brief). The dataset is tiny, so PySpark would add image size without benefit; the functions are small and separable if a Spark port is ever needed.
* The Docker image was written to the standard `python:3.11-slim` pattern; the pipeline itself was executed and verified locally, but the container build was not run in the authoring environment, so please run the build once on your machine.
* Business-user documentation: `docs/data_dictionary.xlsx`.
