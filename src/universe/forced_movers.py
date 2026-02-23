# src/universe/forced_movers.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class ForcedMoversResult:
    tickers: List[str]
    asof_utc: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_forced_movers_from_prices(
    df_prices: pd.DataFrame,
    *,
    max_n: int = 120,
) -> ForcedMoversResult:
    """
    df_prices must have columns: ticker, pct_change_1d, volume (volume optional).
    We pick a blend of biggest abs movers + high volume names.
    """
    tickers: List[str] = []

    if df_prices is None or df_prices.empty:
        return ForcedMoversResult(tickers=[], asof_utc=_utc_now_iso())

    d = df_prices.copy()

    if "ticker" not in d.columns:
        return ForcedMoversResult(tickers=[], asof_utc=_utc_now_iso())

    if "pct_change_1d" not in d.columns:
        d["pct_change_1d"] = 0.0

    d["abs_move"] = d["pct_change_1d"].abs()

    # Top movers (by absolute % change)
    movers = (
        d.sort_values("abs_move", ascending=False)
        .dropna(subset=["ticker"])
        .head(max_n)
    )["ticker"].astype(str).str.upper().tolist()

    tickers.extend(movers)

    # Add volume leaders if available (adds breadth to “activity”)
    if "volume" in d.columns:
        vol = (
            d.sort_values("volume", ascending=False)
            .dropna(subset=["ticker"])
            .head(max_n // 2)
        )["ticker"].astype(str).str.upper().tolist()
        tickers.extend(vol)

    # Dedup stable
    seen = set()
    out = []
    for t in tickers:
        t = t.strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)

    return ForcedMoversResult(tickers=out[:max_n], asof_utc=_utc_now_iso())


def write_forced_movers_json(path: str, movers: ForcedMoversResult) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof_utc": movers.asof_utc,
        "tickers": movers.tickers,
        "source": "hmoney_internal_observed_movers"
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")