"""
referral_pipeline.py
====================
Builds the referral-program report and flags potentially fraudulent rewards.

Pipeline stages (mirrors the skeleton in the test brief)
    1. Load      - read the 7 source CSVs
    2. Clean     - fix types, turn "null" text into real nulls, remove duplicate rows
    3. Process   - join tables (one row per referral), convert UTC -> local time,
                   derive referral_source_category, apply Initcap
    4. Validate  - evaluate the business rules -> is_business_logic_valid
    5. Output    - fill remaining nulls, write the final CSV (+ a detail file with reasons)

Usage:
    python src/referral_pipeline.py
Environment variables (optional):
    DATA_DIR      folder containing the source CSV files      (default: data/raw)
    OUTPUT_DIR    folder where reports are written             (default: output)
    DEFAULT_TZ    time zone used when no zone can be found     (default: Asia/Jakarta)
"""

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.environ.get("DATA_DIR", "data/raw")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Jakarta")

# The source files store missing values as the literal text "null".
NULL_TOKENS = ["null", "NULL", "Null", "", "nan", "NaN", "None"]

# Value written wherever a null would otherwise remain (the brief says no nulls in the output).
FILL_TEXT = "Unknown"

# Final column order of the report (exactly as listed in the test brief).
REPORT_COLUMNS = [
    "referral_details_id", "referral_id", "referral_source", "referral_source_category",
    "referral_at", "referrer_id", "referrer_name", "referrer_phone_number", "referrer_homeclub",
    "referee_id", "referee_name", "referee_phone", "referral_status", "num_reward_days",
    "transaction_id", "transaction_status", "transaction_at", "transaction_location",
    "transaction_type", "updated_at", "reward_granted_at", "is_business_logic_valid",
]


# --------------------------------------------------------------------------------------
# 1. LOAD
# --------------------------------------------------------------------------------------
def load_tables(data_dir: str) -> dict:
    """Load every source CSV into a DataFrame. Keys are the table names used in the brief."""
    # lead_logs is delivered as lead_log.csv, so we map file name -> table name explicitly.
    files = {
        "lead_logs": "lead_log.csv",
        "paid_transactions": "paid_transactions.csv",
        "referral_rewards": "referral_rewards.csv",
        "user_logs": "user_logs.csv",
        "user_referral_logs": "user_referral_logs.csv",
        "user_referral_statuses": "user_referral_statuses.csv",
        "user_referrals": "user_referrals.csv",
    }
    tables = {}
    for table, file_name in files.items():
        path = os.path.join(data_dir, file_name)
        if not os.path.exists(path):
            sys.exit(f"Missing source file: {path}")
        tables[table] = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=NULL_TOKENS)
    return tables


# --------------------------------------------------------------------------------------
# 2. CLEAN
# --------------------------------------------------------------------------------------
def to_utc(series: pd.Series) -> pd.Series:
    """Parse ISO-8601 text (with or without milliseconds) into timezone-aware UTC timestamps."""
    return pd.to_datetime(series, utc=True, format="ISO8601", errors="coerce")


def to_bool(series: pd.Series) -> pd.Series:
    """Convert 'true'/'false' text (any case) to booleans; anything else stays null."""
    return series.str.lower().map({"true": True, "false": False})


def clean_tables(t: dict) -> dict:
    """Fix data types and remove duplicate rows so later joins cannot multiply rows."""
    c = {}

    # --- user_referrals: the 'spine' table - one row per referral (referral_id is unique)
    ref = t["user_referrals"].copy()
    ref["referral_at"] = to_utc(ref["referral_at"])
    ref["updated_at"] = to_utc(ref["updated_at"])
    ref["referral_reward_id"] = pd.to_numeric(ref["referral_reward_id"], errors="coerce")
    ref["user_referral_status_id"] = pd.to_numeric(ref["user_referral_status_id"], errors="coerce")
    c["user_referrals"] = ref.drop_duplicates("referral_id")

    # --- user_referral_logs: many log rows per referral (a status history).
    # Keep only the LATEST log per referral so the join stays 1:1.
    logs = t["user_referral_logs"].copy()
    logs["id"] = pd.to_numeric(logs["id"])
    logs["created_at"] = to_utc(logs["created_at"])
    logs["is_reward_granted"] = to_bool(logs["is_reward_granted"])
    logs = logs.sort_values(["user_referral_id", "created_at", "id"])
    c["user_referral_logs"] = logs.drop_duplicates("user_referral_id", keep="last")

    # --- user_logs: the same user appears several times (identical snapshots).
    users = t["user_logs"].copy()
    users["id"] = pd.to_numeric(users["id"])
    users["membership_expired_date"] = pd.to_datetime(users["membership_expired_date"], errors="coerce")
    users["is_deleted"] = to_bool(users["is_deleted"])
    users = users.sort_values(["user_id", "id"])
    c["user_logs"] = users.drop_duplicates("user_id", keep="last")

    # --- lead_logs: a lead has several log rows as its status changes -> keep the latest.
    leads = t["lead_logs"].copy()
    leads["id"] = pd.to_numeric(leads["id"])
    leads["created_at"] = to_utc(leads["created_at"])
    leads = leads.sort_values(["lead_id", "id"])
    c["lead_logs"] = leads.drop_duplicates("lead_id", keep="last")

    # --- paid_transactions
    tx = t["paid_transactions"].copy()
    tx["transaction_at"] = to_utc(tx["transaction_at"])
    c["paid_transactions"] = tx.drop_duplicates("transaction_id")

    # --- referral_rewards: reward_value is text such as "10 days" -> integer number of days
    rw = t["referral_rewards"].copy()
    rw["id"] = pd.to_numeric(rw["id"])
    rw["num_reward_days"] = pd.to_numeric(rw["reward_value"].str.extract(r"(\d+)")[0], errors="coerce")
    c["referral_rewards"] = rw.drop_duplicates("id")

    # --- user_referral_statuses
    st = t["user_referral_statuses"].copy()
    st["id"] = pd.to_numeric(st["id"])
    c["user_referral_statuses"] = st.drop_duplicates("id")
    return c


# --------------------------------------------------------------------------------------
# 3. PROCESS
# --------------------------------------------------------------------------------------
def to_local(utc_series: pd.Series, tz_series: pd.Series) -> pd.Series:
    """
    Convert UTC timestamps to local wall-clock time, row by row, using a per-row time-zone name.
    Returns timezone-naive timestamps (e.g. 2024-05-02 11:49:01) in the local zone.
    """
    out = pd.Series(pd.NaT, index=utc_series.index, dtype="datetime64[ns]")
    for tz in tz_series.dropna().unique():
        mask = (tz_series == tz) & utc_series.notna()
        converted = utc_series[mask].dt.tz_convert(tz).dt.tz_localize(None)
        out.loc[mask] = converted.astype("datetime64[ns]")
    return out


def initcap(series: pd.Series) -> pd.Series:
    """
    Initcap like Spark/SQL: first letter of every word upper-case, the rest lower-case.
    (pandas .str.title() is NOT used because it also capitalises letters after digits,
    which would corrupt values such as hashed IDs.)
    """
    return series.str.lower().str.replace(
        r"(^|\s)(\S)", lambda m: m.group(1) + m.group(2).upper(), regex=True
    )


def build_base(c: dict) -> pd.DataFrame:
    """Join all tables onto user_referrals, keeping exactly one row per referral."""
    ref = c["user_referrals"]
    n_before = len(ref)

    # Statuses and rewards: simple lookups by id
    df = ref.merge(
        c["user_referral_statuses"][["id", "description"]].rename(
            columns={"id": "user_referral_status_id", "description": "referral_status"}),
        on="user_referral_status_id", how="left")
    df = df.merge(
        c["referral_rewards"][["id", "num_reward_days"]].rename(columns={"id": "referral_reward_id"}),
        on="referral_reward_id", how="left")

    # Latest referral log (reward-granted flag + time the log was written)
    df = df.merge(
        c["user_referral_logs"][["user_referral_id", "is_reward_granted", "created_at"]].rename(
            columns={"user_referral_id": "referral_id", "created_at": "reward_log_at"}),
        on="referral_id", how="left")

    # Referrer details (name, phone, home club, time zone, membership, deleted flag)
    users = c["user_logs"][["user_id", "name", "phone_number", "homeclub", "timezone_homeclub",
                            "membership_expired_date", "is_deleted"]]
    df = df.merge(
        users.rename(columns={
            "user_id": "referrer_id", "name": "referrer_name", "phone_number": "referrer_phone_number",
            "homeclub": "referrer_homeclub", "timezone_homeclub": "referrer_tz"}),
        on="referrer_id", how="left")

    # Transaction details
    df = df.merge(
        c["paid_transactions"].rename(columns={"timezone_transaction": "transaction_tz"}),
        on="transaction_id", how="left")

    # Lead details - ONLY for referrals whose source is 'Lead' (referee_id is a lead_id there)
    leads = c["lead_logs"][["lead_id", "source_category", "timezone_location"]].rename(
        columns={"lead_id": "referee_id", "source_category": "lead_source_category",
                 "timezone_location": "lead_tz"})
    df = df.merge(leads, on="referee_id", how="left")
    not_lead = df["referral_source"] != "Lead"
    df.loc[not_lead, ["lead_source_category", "lead_tz"]] = np.nan

    # Safety check: no join may have duplicated or dropped referrals
    assert len(df) == n_before, f"Join changed row count: {n_before} -> {len(df)}"
    assert df["referral_id"].is_unique, "Duplicate referral_id after joins"
    return df


def add_local_times_and_category(df: pd.DataFrame) -> pd.DataFrame:
    """Convert UTC timestamps to local time and derive referral_source_category."""
    # Transaction time uses the zone stored with the transaction.
    df["transaction_at_local"] = to_local(df["transaction_at"], df["transaction_tz"])

    # Referral / updated / reward-granted times have no zone of their own, so we borrow one:
    #   referrer's home-club zone -> lead's preferred-location zone -> transaction zone -> default
    df["referral_tz"] = (df["referrer_tz"]
                         .fillna(df["lead_tz"])
                         .fillna(df["transaction_tz"])
                         .fillna(DEFAULT_TZ))
    df["referral_at_local"] = to_local(df["referral_at"], df["referral_tz"])
    df["updated_at_local"] = to_local(df["updated_at"], df["referral_tz"])

    # reward_granted_at = when the log recording "reward granted = TRUE" was written
    granted_utc = df["reward_log_at"].where(df["is_reward_granted"] == True)  # noqa: E712
    df["reward_granted_at_local"] = to_local(granted_utc, df["referral_tz"])

    # Source category (logic given in the brief)
    df["referral_source_category"] = np.select(
        [df["referral_source"] == "User Sign Up",
         df["referral_source"] == "Draft Transaction",
         df["referral_source"] == "Lead"],
        ["Online", "Offline", df["lead_source_category"].fillna(FILL_TEXT)],
        default=FILL_TEXT)
    return df


# --------------------------------------------------------------------------------------
# 4. BUSINESS LOGIC / FRAUD DETECTION
# --------------------------------------------------------------------------------------
def apply_business_logic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Evaluate the valid / invalid reward rules from the brief and set is_business_logic_valid.

    Each rule is stored as its own True/False column so reviewers can see exactly why a row
    passed or failed (these columns go to the detail file, not the main report).
    """
    # ---- Building-block facts (computed on cleaned data BEFORE nulls are filled) ----
    has_reward = df["num_reward_days"].fillna(0) > 0                     # reward value > 0
    no_reward = ~has_reward                                              # null or 0
    status = df["referral_status"]
    is_success = status == "Berhasil"
    is_pending_or_failed = status.isin(["Menunggu", "Tidak Berhasil"])
    has_txn_id = df["transaction_id"].notna()
    has_txn_row = df["transaction_at"].notna()        # transaction id actually found in paid_transactions
    txn_paid = df["transaction_status"].str.upper() == "PAID"
    txn_new = df["transaction_type"].str.upper() == "NEW"

    # "After" is judged on the real UTC instants (time-zone safe, compared at full precision).
    txn_after_ref = has_txn_row & (df["transaction_at"] > df["referral_at"])
    txn_before_ref = has_txn_row & (df["transaction_at"] < df["referral_at"])

    # "Same month" is judged on local calendar months (what a business user would see).
    same_month = (has_txn_row
                  & (df["transaction_at_local"].dt.to_period("M") == df["referral_at_local"].dt.to_period("M")))

    # Membership is checked as of the referral date; unknown referrer => cannot confirm => False.
    membership_ok = (df["membership_expired_date"].notna()
                     & (df["membership_expired_date"] >= df["referral_at_local"].dt.normalize()))
    account_ok = df["is_deleted"] == False  # noqa: E712  (False only; null/unknown => not confirmed)
    reward_granted = df["is_reward_granted"] == True  # noqa: E712

    # ---- VALID rules ----
    # The ten checks of "Valid Condition 1" are kept individually so a failure can be explained.
    checks = {
        "1 reward value is greater than 0": has_reward,
        "2 referral status is Berhasil": is_success,
        "3 referral has a transaction ID": has_txn_id,
        "4 transaction status is PAID": txn_paid,
        "5 transaction type is NEW": txn_new,
        "6 transaction happened after the referral": txn_after_ref,
        "7 transaction is in the same month as the referral": same_month,
        "8 referrer membership not expired": membership_ok,
        "9 referrer account not deleted": account_ok,
        "10 reward has been granted": reward_granted,
    }
    for name, series in checks.items():
        df["v1_check_" + name.split(" ")[0].zfill(2)] = series
    df["valid_cond_1"] = pd.concat(list(checks.values()), axis=1).all(axis=1)
    df["valid_cond_2"] = is_pending_or_failed & no_reward

    # ---- INVALID rules ----
    df["invalid_cond_1"] = has_reward & ~is_success                                   # reward but not successful
    df["invalid_cond_2"] = has_reward & ~has_txn_id                                   # reward but no transaction
    df["invalid_cond_3"] = no_reward & has_txn_id & txn_paid & txn_after_ref          # paid txn but no reward
    df["invalid_cond_4"] = is_success & no_reward                                     # success but no reward
    df["invalid_cond_5"] = txn_before_ref                                             # txn happened before referral

    # ---- EXTRA (informational) fraud signals: do NOT change is_business_logic_valid ----
    df["flag_txn_shared_by_many_referrals"] = has_txn_id & (df.groupby("transaction_id")["referral_id"].transform("count") > 1)
    df["flag_txn_not_in_paid_transactions"] = has_txn_id & ~has_txn_row
    df["flag_referee_referred_many_times"] = df["referee_id"].notna() & (df.groupby("referee_id")["referral_id"].transform("count") > 1)
    df["flag_referee_phone_reused"] = df.groupby("referee_phone")["referral_id"].transform("count") > 1
    df["flag_referrer_not_found"] = df["referrer_name"].isna()

    invalid_cols = [f"invalid_cond_{i}" for i in range(1, 6)]
    any_invalid = df[invalid_cols].any(axis=1)

    # A referral is VALID only if it satisfies a valid rule AND trips no invalid rule.
    # (Invalid wins on overlap: it is the safer choice for fraud detection.)
    df["is_business_logic_valid"] = (df["valid_cond_1"] | df["valid_cond_2"]) & ~any_invalid

    # Human-readable reason, for the detail file
    reason_text = {
        "invalid_cond_1": "Reward assigned but referral status is not Berhasil",
        "invalid_cond_2": "Reward assigned but referral has no transaction ID",
        "invalid_cond_3": "No reward assigned although a PAID transaction happened after the referral",
        "invalid_cond_4": "Referral is Berhasil but reward is missing or 0",
        "invalid_cond_5": "Transaction happened BEFORE the referral was created",
    }

    def explain(row):
        if row["is_business_logic_valid"]:
            return ("Valid - all reward conditions met" if row["valid_cond_1"]
                    else "Valid - pending/failed referral with no reward")
        reasons = [txt for col, txt in reason_text.items() if row[col]]
        if reasons:
            return "; ".join(reasons)
        # No specific invalid rule fired, so name the Valid-Condition-1 checks that failed.
        failed = [name for name in checks if not row["v1_check_" + name.split(" ")[0].zfill(2)]]
        return "Invalid - failed reward check(s): " + ", ".join(failed)

    df["validation_reason"] = df.apply(explain, axis=1)
    return df


# --------------------------------------------------------------------------------------
# 5. OUTPUT
# --------------------------------------------------------------------------------------
def format_report(df: pd.DataFrame) -> pd.DataFrame:
    """Shape the final report: Initcap, no nulls, ordered columns, surrogate key."""
    out = pd.DataFrame(index=df.index)

    # Sort so the surrogate key is stable between runs (oldest referral first)
    order = df.sort_values(["referral_at", "referral_id"]).index
    out["referral_details_id"] = pd.Series(range(1, len(df) + 1), index=order)

    out["referral_id"] = df["referral_id"]
    out["referral_source"] = initcap(df["referral_source"])
    out["referral_source_category"] = initcap(df["referral_source_category"])
    out["referral_at"] = df["referral_at_local"]
    out["referrer_id"] = df["referrer_id"]
    out["referrer_name"] = initcap(df["referrer_name"])
    out["referrer_phone_number"] = df["referrer_phone_number"]
    out["referrer_homeclub"] = df["referrer_homeclub"]               # club names keep their UPPER case
    out["referee_id"] = df["referee_id"]
    out["referee_name"] = initcap(df["referee_name"])
    out["referee_phone"] = df["referee_phone"]
    out["referral_status"] = initcap(df["referral_status"])
    out["num_reward_days"] = df["num_reward_days"]
    out["transaction_id"] = df["transaction_id"]
    out["transaction_status"] = initcap(df["transaction_status"])
    out["transaction_at"] = df["transaction_at_local"]
    out["transaction_location"] = df["transaction_location"]         # club name -> UPPER case
    out["transaction_type"] = initcap(df["transaction_type"])
    out["updated_at"] = df["updated_at_local"]
    out["reward_granted_at"] = df["reward_granted_at_local"]
    out["is_business_logic_valid"] = df["is_business_logic_valid"]

    # Dates -> text so that a placeholder can sit in the same column
    dt_fmt = "%Y-%m-%d %H:%M:%S"
    for col in ["referral_at", "transaction_at", "updated_at", "reward_granted_at"]:
        out[col] = out[col].dt.strftime(dt_fmt)

    # No nulls allowed: numbers -> 0, everything else -> "Unknown"
    out["num_reward_days"] = out["num_reward_days"].fillna(0).astype(int)
    text_cols = [c for c in out.columns if c not in ("referral_details_id", "num_reward_days",
                                                      "is_business_logic_valid")]
    out[text_cols] = out[text_cols].astype(object).fillna(FILL_TEXT)

    out = out.sort_values("referral_details_id")[REPORT_COLUMNS]
    assert out.isna().sum().sum() == 0, "Nulls remain in final report"
    return out


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("1/5 Loading source tables ...")
    raw = load_tables(DATA_DIR)

    print("2/5 Cleaning (types, nulls, duplicates) ...")
    clean = clean_tables(raw)

    print("3/5 Joining tables, converting time zones, deriving source category ...")
    df = build_base(clean)
    df = add_local_times_and_category(df)

    print("4/5 Applying business-logic / fraud rules ...")
    df = apply_business_logic(df)

    print("5/5 Writing outputs ...")
    report = format_report(df)
    report.to_csv(os.path.join(OUTPUT_DIR, "referral_report.csv"), index=False)

    # Detail file: one row per referral showing which rules fired (audit trail)
    detail_cols = (["referral_id", "is_business_logic_valid", "validation_reason",
                    "valid_cond_1", "valid_cond_2",
                    "invalid_cond_1", "invalid_cond_2", "invalid_cond_3", "invalid_cond_4", "invalid_cond_5"]
                   + sorted(c for c in df.columns if c.startswith("v1_check_"))
                   + sorted(c for c in df.columns if c.startswith("flag_")))
    detail = df[detail_cols].merge(report[["referral_details_id", "referral_id"]], on="referral_id")
    detail = detail.sort_values("referral_details_id")
    detail = detail[["referral_details_id"] + detail_cols]
    detail.to_csv(os.path.join(OUTPUT_DIR, "referral_validation_details.csv"), index=False)

    n_valid = int(report["is_business_logic_valid"].sum())
    print(f"\nDone: {len(report)} referrals -> {n_valid} valid, {len(report) - n_valid} invalid")
    print(f"Report : {OUTPUT_DIR}/referral_report.csv")
    print(f"Details: {OUTPUT_DIR}/referral_validation_details.csv")


if __name__ == "__main__":
    main()
