from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict
import numpy as np
import pandas as pd
from src.config import CONFIG

"""
src/render/state_builder.py

Builds public/state.json from the scored universe.
"""






def _mean_int(series: pd.Series) -> int:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0
    return int(round(float(s.mean())))


def build_state(scored: pd.DataFrame, as_of_utc: str) -> Dict[str, Any]:
    """
    scored: DataFrame indexed by symbol, containing columns:
    - opportunity_score
    - structural_risk_score
    - label
    """
    if scored is None or scored.empty:
        return {
            "as_of_utc": as_of_utc,
            "market_bias": "🟡",
            "opportunity_score": 0,
            "structural_risk_score": 0,
            "counts_by_label": {"🟢": 0, "🟡": 0, "🔵": 0, "🟠": 0, "🔴": 0},
        }

    opp_mean = _mean_int(scored["opportunity_score"])
    risk_mean = _mean_int(scored["structural_risk_score"])

    # Market bias as a light aggregate signal: high opportunity and not-high risk -> green,
    # low opportunity or high risk -> red, otherwise yellow.
    if opp_mean >= CONFIG.market_green_min and risk_mean < 65:
        market_bias = "🟢"
    elif opp_mean <= CONFIG.market_red_max or risk_mean >= 75:
        market_bias = "🔴"
    else:
        market_bias = "🟡"

    counts = scored["label"].value_counts().to_dict()
    counts_by_label = {k: int(counts.get(k, 0)) for k in ["🟢", "🟡", "🔵", "🟠", "🔴"]}

    return {
        "as_of_utc": as_of_utc,
        "market_bias": market_bias,
        "opportunity_score": opp_mean,
        "structural_risk_score": risk_mean,
        "counts_by_label": counts_by_label,
        "universe_size": int(len(scored)),
    }