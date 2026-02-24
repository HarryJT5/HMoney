# scripts/build_daily_history.py
from __future__ import annotations

import argparse
import gzip
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd


def _utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_csv_gz(path: Path) -> Optional[pd.DataFrame]:
    try:
        return pd.read_csv(path, compression="gzip")
    except Exception:
        return None


def _write_csv_gz(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wb") as f:
        f.write(df.to_csv(index=False).encode("utf-8"))


def build_daily_rollup_from_snapshots(snapshots_root: Path, day: str) -> pd.DataFrame:
    """
    Build a daily rollup for YYYY-MM-DD from:
      docs/history/snapshots/YYYY-MM-DD/HHMM.csv.gz

    Rule: keep the latest snapshot row per ticker (by HHMM).
    """
    day_dir = snapshots_root / day
    if not day_dir.exists():
        return pd.DataFrame()

    files = sorted(day_dir.glob("*.csv.gz"))
    if not files:
        return pd.DataFrame()

    # Read snapshots in chronological order; later snapshots overwrite earlier
    latest_by_ticker: Dict[str, pd.Series] = {}

    for f in files:
        hhmm = f.stem.split(".")[0]  # "HHMM" from "HHMM.csv.gz" -> stem is "HHMM.csv"
        # safer: derive from filename directly
        name = f.name
        hhmm = name.split(".")[0]

        df = _read_csv_gz(f)
        if df is None or df.empty:
            continue

        # Normalize ticker column
        if "ticker" not in df.columns:
            continue
        df["ticker"] = df["ticker"].astype(str).str.upper()

        # Attach snapshot time for tie-breaking / provenance
        df["snapshot_hhmm"] = hhmm
        df["snapshot_day"] = day

        # Keep last seen per ticker (since we iterate in time order)
        for _, row in df.iterrows():
            t = row["ticker"]
            latest_by_ticker[t] = row

    if not latest_by_ticker:
        return pd.DataFrame()

    out = pd.DataFrame(latest_by_ticker.values()).copy()

    # Reorder some helpful columns up front if present
    preferred = [
        "ticker",
        "snapshot_day",
        "snapshot_hhmm",
        "as_of_utc",
        "last_price",
        "pct_change_1d",
        "volume",
        "opportunity_score",
        "structural_risk_score",
        "discount_score",
        "label",
        "confidence",
        "deployment_bias",
        "pct_off_52w_high",
        "ma_200",
        "atr_pct",
        "run_mode",
        "shard_count",
        "shard_index",
        "time_bucket",
    ]
    cols = [c for c in preferred if c in out.columns] + [c for c in out.columns if c not in preferred]
    out = out[cols]

    out = out.sort_values("ticker").reset_index(drop=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: UTC today)")
    ap.add_argument("--snapshots-root", default="docs/history/snapshots", help="Root folder for per-run snapshots")
    ap.add_argument("--out-dir", default="docs/history/daily", help="Output folder for daily rollups")
    args = ap.parse_args()

    day = args.day or _utc_today_str()
    snapshots_root = Path(args.snapshots_root)
    out_dir = Path(args.out_dir)

    df = build_daily_rollup_from_snapshots(snapshots_root, day)
    if df.empty:
        print(f"No snapshot files found for day={day} under {snapshots_root}")
        return

    out_path = out_dir / f"{day}.csv.gz"
    _write_csv_gz(df, out_path)
    print(f"Wrote daily rollup: {out_path} ({len(df)} tickers)")


if __name__ == "__main__":
    main()