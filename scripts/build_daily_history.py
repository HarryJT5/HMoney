# scripts/build_daily_history.py
from __future__ import annotations

import argparse
import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd


@dataclass
class Row:
    ticker: str
    day: str
    run_hhmm: str
    run_id: str
    as_of_utc: Optional[str]

    last_price: Optional[float]
    pct_change_1d: Optional[float]
    volume: Optional[float]

    opportunity_score: Optional[int]
    structural_risk_score: Optional[int]
    discount_score: Optional[int]

    label: Optional[str]
    confidence: Optional[float]
    deployment_bias: Optional[str]

    pct_off_52w_high: Optional[float]
    ma_200: Optional[float]
    atr_pct: Optional[float]


def _utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        return v
    except Exception:
        return None


def _safe_int(x) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _get_ticker(pack: Dict[str, Any], path: Path) -> str:
    # Prefer pack schema fields; fallback to filename stem
    for k in ("symbol", "ticker"):
        if isinstance(pack.get(k), str) and pack.get(k).strip():
            return pack[k].strip().upper()

    asset = pack.get("asset") or {}
    if isinstance(asset, dict):
        for k in ("symbol", "ticker"):
            if isinstance(asset.get(k), str) and asset.get(k).strip():
                return asset[k].strip().upper()

    return path.stem.strip().upper()


def _extract(pack: Dict[str, Any], day: str, run_hhmm: str, run_id: str, path: Path) -> Row:
    ticker = _get_ticker(pack, path)

    market = pack.get("market") or {}
    scores = pack.get("scores") or {}
    classification = pack.get("classification") or {}

    as_of_utc = pack.get("as_of_utc") or pack.get("as_of") or pack.get("generated_at_utc")

    return Row(
        ticker=ticker,
        day=day,
        run_hhmm=run_hhmm,
        run_id=run_id,
        as_of_utc=str(as_of_utc) if as_of_utc else None,

        last_price=_safe_float(market.get("last_price")),
        pct_change_1d=_safe_float(market.get("pct_change_1d")),
        volume=_safe_float(market.get("volume")),

        opportunity_score=_safe_int(scores.get("opportunity_score")),
        structural_risk_score=_safe_int(scores.get("structural_risk_score")),
        discount_score=_safe_int(scores.get("discount_score")),

        label=str(classification.get("label")) if classification.get("label") is not None else None,
        confidence=_safe_float(classification.get("confidence")),
        deployment_bias=str(pack.get("deployment_bias")) if pack.get("deployment_bias") is not None else None,

        pct_off_52w_high=_safe_float(market.get("pct_off_52w_high")),
        ma_200=_safe_float(market.get("ma_200")),
        atr_pct=_safe_float(market.get("atr_pct")),
    )


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_run_parts(p: Path) -> Optional[Tuple[str, str]]:
    """
    Expect: .../evidence_packs/YYYY-MM-DD/HHMM/TICKER.json
    Returns (day, hhmm)
    """
    try:
        hhmm = p.parent.name
        day = p.parent.parent.name
        if len(day) == 10 and len(hhmm) == 4:
            return day, hhmm
        return None
    except Exception:
        return None


def build_daily_rollup(packs_root: Path, day: str) -> pd.DataFrame:
    """
    For a given YYYY-MM-DD, scan all packs and keep the latest observation per ticker.
    (Long-term history = daily EOD-ish snapshots; intraday history stays in run folders.)
    """
    day_dir = packs_root / day
    if not day_dir.exists():
        return pd.DataFrame()

    # All json packs for the day across all HHMM folders
    paths = sorted(day_dir.glob("*/*.json"))
    if not paths:
        return pd.DataFrame()

    latest_by_ticker: Dict[str, Row] = {}

    for p in paths:
        parts = _parse_run_parts(p)
        if not parts:
            continue
        d, hhmm = parts
        pack = _read_json(p)
        if not pack:
            continue

        run_id = pack.get("run_id") or f"{d}/{hhmm}"
        r = _extract(pack, d, hhmm, str(run_id), p)

        # Choose latest by HHMM (string compare works for 0000-2359)
        prev = latest_by_ticker.get(r.ticker)
        if prev is None or r.run_hhmm >= prev.run_hhmm:
            latest_by_ticker[r.ticker] = r

    if not latest_by_ticker:
        return pd.DataFrame()

    df = pd.DataFrame([vars(v) for v in latest_by_ticker.values()])
    df = df.sort_values("ticker").reset_index(drop=True)
    return df


def write_csv_gz(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    with gzip.open(out_path, "wb") as f:
        f.write(csv_bytes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs-root", default="docs/evidence_packs", help="Root folder for evidence packs")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: UTC today)")
    ap.add_argument("--out-dir", default="docs/history/daily", help="Output folder for daily rollups")
    args = ap.parse_args()

    day = args.day or _utc_today_str()
    packs_root = Path(args.packs_root)
    out_dir = Path(args.out_dir)

    df = build_daily_rollup(packs_root, day)
    if df.empty:
        print(f"No packs found for day={day} under {packs_root}")
        return

    out_path = out_dir / f"{day}.csv.gz"
    write_csv_gz(df, out_path)

    print(f"Wrote daily rollup: {out_path} ({len(df)} tickers)")


if __name__ == "__main__":
    main()