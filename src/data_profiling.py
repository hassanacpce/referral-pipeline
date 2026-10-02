"""
data_profiling.py
=================
Profiles every source CSV table and writes one summary row per column.

For each column we report (modelled on the example in the test brief):
    * data type (inferred by pandas after real "null" text is converted to NaN)
    * null count and percentage populated
    * distinct value count
    * minimum / maximum value
    * maximum actual length (string columns only)
    * the 5 most frequent values (a lightweight stand-in for the "Frequency" link)

Outputs (written to OUTPUT_DIR):
    * data_profiling_report.csv   - all tables in one flat file
    * data_profiling_report.xlsx  - same content, one sheet per table + summary

Usage:
    python src/data_profiling.py
Environment variables (optional):
    DATA_DIR    folder containing the source CSV files   (default: data/raw)
    OUTPUT_DIR  folder where reports are written          (default: output)
"""

import os
import sys

import pandas as pd

DATA_DIR = os.environ.get("DATA_DIR", "data/raw")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

# The source files use the literal text "null" (and sometimes blanks) for missing values.
NULL_TOKENS = ["null", "NULL", "Null", "", "nan", "NaN", "None"]


def load_all_tables(data_dir: str) -> dict:
    """Read every *.csv in `data_dir` into a DataFrame keyed by table name."""
    tables = {}
    for file_name in sorted(os.listdir(data_dir)):
        if file_name.lower().endswith(".csv"):
            table_name = file_name[:-4]
            # Read everything as text first so profiling reflects the raw file;
            # types are then inferred in `infer_type`.
            tables[table_name] = pd.read_csv(
                os.path.join(data_dir, file_name),
                dtype=str,
                keep_default_na=False,
                na_values=NULL_TOKENS,
            )
    return tables


def infer_type(series: pd.Series) -> str:
    """Best-guess logical type of a text column (ignoring nulls)."""
    values = series.dropna()
    if values.empty:
        return "unknown"
    if values.str.lower().isin(["true", "false"]).all():
        return "boolean"
    if pd.to_numeric(values, errors="coerce").notna().all():
        as_num = pd.to_numeric(values)
        return "integer" if (as_num % 1 == 0).all() else "decimal"
    # ISO timestamps such as 2024-05-13T06:38:58.322Z
    if values.str.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}").all():
        return "timestamp"
    if values.str.match(r"^\d{4}-\d{2}-\d{2}$").all():
        return "date"
    return "string"


def profile_table(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Return one profiling row per column of `df`."""
    rows = []
    total = len(df)
    for col in df.columns:
        s = df[col]
        dtype = infer_type(s)
        non_null = s.dropna()

        # Min / max: compare numerically for numbers, otherwise as text
        if non_null.empty:
            min_v = max_v = None
        elif dtype in ("integer", "decimal"):
            num = pd.to_numeric(non_null)
            min_v, max_v = num.min(), num.max()
        else:
            min_v, max_v = non_null.min(), non_null.max()

        top = non_null.value_counts().head(5)
        top_txt = "; ".join(f"{k} ({v})" for k, v in top.items())

        rows.append(
            {
                "table_name": table_name,
                "column_name": col,
                "data_type": dtype,
                "row_count": total,
                "null_count": int(s.isna().sum()),
                "percentage_populated": round(100 * non_null.size / total, 2) if total else 0.0,
                "distinct_value_count": int(non_null.nunique()),
                "duplicate_row_count_in_column": int(non_null.size - non_null.nunique()),
                "minimum_value": min_v,
                "maximum_value": max_v,
                "max_actual_length": int(non_null.str.len().max()) if dtype in ("string", "timestamp", "date") and not non_null.empty else None,
                "top_5_values": top_txt,
            }
        )
    result = pd.DataFrame(rows)
    result["max_actual_length"] = result["max_actual_length"].astype("Int64")  # whole numbers, blank for non-text
    return result


def main() -> None:
    if not os.path.isdir(DATA_DIR):
        sys.exit(f"DATA_DIR '{DATA_DIR}' not found")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    tables = load_all_tables(DATA_DIR)
    if not tables:
        sys.exit(f"No CSV files found in {DATA_DIR}")

    profiles = {name: profile_table(name, df) for name, df in tables.items()}
    combined = pd.concat(profiles.values(), ignore_index=True)

    # Table-level summary: rows, columns and fully duplicated rows
    summary = pd.DataFrame(
        [
            {
                "table_name": name,
                "row_count": len(df),
                "column_count": df.shape[1],
                "fully_duplicated_rows": int(df.duplicated().sum()),
                "columns_with_nulls": int((df.isna().sum() > 0).sum()),
            }
            for name, df in tables.items()
        ]
    )

    combined.to_csv(os.path.join(OUTPUT_DIR, "data_profiling_report.csv"), index=False)
    with pd.ExcelWriter(os.path.join(OUTPUT_DIR, "data_profiling_report.xlsx")) as xl:
        summary.to_excel(xl, sheet_name="summary", index=False)
        for name, prof in profiles.items():
            prof.drop(columns=["table_name"]).to_excel(xl, sheet_name=name[:31], index=False)

    print(summary.to_string(index=False))
    print(f"\nProfiling complete -> {OUTPUT_DIR}/data_profiling_report.csv (+ .xlsx)")


if __name__ == "__main__":
    main()
