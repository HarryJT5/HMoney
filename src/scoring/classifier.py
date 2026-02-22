from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import numpy as np
import pandas as pd
from src.config import CONFIG

"""
src/scoring/classifier.py

Cross-sectional ("B") scoring:
- Compute features per ticker
- Percentile-rank features across the day's universe
- Combine into Opportunity (0–100) and Structural Risk (0–100)
"""






@dataclass
class ScoredRow:
    opportunity_score: int
    structural_risk_score: int
    discount_score: Optional[int]
    label: str
    confidence: float
    deployment_bias: str
    reason_codes: list[str]


def _pct_rank(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """
    Percentile rank in [0,1]. NaNs remain NaN.
    If higher_is_better=False, we rank the negative so "lower" becomes "better".
    """
    s = series.copy()
    if not higher_is_better:
        s = -s
    # rank(pct=True) gives 0..1, but ties may compress; that's fine.
    return s.rank(pct=True, na_option="keep")


def _to_0_100(x) -> int:
    try:
        if x is None or pd.isna(x):
            return 0
        val = float(x)
        if np.isnan(val):
            return 0
        return int(round(np.clip(val, 0.0, 1.0) * 100))
    except Exception:
        return 0


def score_universe(features: pd.DataFrame) -> pd.DataFrame:
    """
    Input: DataFrame indexed by symbol, with numeric feature columns:
    - pct_off_52w_high (0..1, higher = more discounted)
    - trend_200 (price/ma200 - 1, higher = better)
    - atr_pct (higher = more volatile/risky)
    - dollar_volume_20d (higher = more liquid/safer)

    Returns features + percentiles + final scores.
    """
    df = features.copy()

    # Percentiles
    df["p_discount"] = _pct_rank(df["pct_off_52w_high"], higher_is_better=True)
    df["p_trend"] = _pct_rank(df["trend_200"], higher_is_better=True)
    df["p_vol"] = _pct_rank(df["atr_pct"], higher_is_better=False)  # lower vol is better for "opportunity"
    # For risk we want high vol => high risk, so also keep:
    df["p_vol_risk"] = _pct_rank(df["atr_pct"], higher_is_better=True)

    # Illiquidity: lower dollar volume = worse (more risk)
    if "dollar_volume_20d" in df.columns and df["dollar_volume_20d"].notna().any():
        df["p_liquidity_good"] = _pct_rank(df["dollar_volume_20d"], higher_is_better=True)
        df["p_illiquidity_risk"] = 1.0 - df["p_liquidity_good"]
        has_liq = True
    else:
        df["p_liquidity_good"] = np.nan
        df["p_illiquidity_risk"] = np.nan
        has_liq = False

    # Opportunity: discount + trend + low vol
    opp_components = {
        "discount": (df["p_discount"], CONFIG.opp_w_discount),
        "trend": (df["p_trend"], CONFIG.opp_w_trend),
        "low_vol": (df["p_vol"], CONFIG.opp_w_low_vol),
    }

    # Structural risk: high vol + drawdown + illiquidity
    risk_components = {
        "vol": (df["p_vol_risk"], CONFIG.risk_w_vol),
        "drawdown": (df["p_discount"], CONFIG.risk_w_drawdown),  # discount proxy doubles as drawdown proxy
        "illiquidity": (df["p_illiquidity_risk"], CONFIG.risk_w_illiquidity),
    }

    def weighted_mean(components: dict) -> pd.Series:
        num = None
        den = None
        for _, (s, w) in components.items():
            if num is None:
                num = s * w
                den = s.notna().astype(float) * w
            else:
                num = num + s * w
                den = den + s.notna().astype(float) * w
        # Avoid division by zero
        out = num / den.replace(0, np.nan)
        return out

    df["opportunity_raw"] = weighted_mean(opp_components)
    df["structural_risk_raw"] = weighted_mean(risk_components)

    df["opportunity_score"] = df["opportunity_raw"].apply(_to_0_100)
    df["structural_risk_score"] = df["structural_risk_raw"].apply(_to_0_100)

    # Discount score (optional but useful): basically p_discount
    df["discount_score"] = df["p_discount"].apply(lambda x: None if pd.isna(x) else _to_0_100(x))

    return df


def classify_row(row: pd.Series) -> Tuple[str, float, str, list[str]]:
    """
    Maps scores -> 🟢🟡🔵🟠🔴 plus confidence and deployment_bias.
    """
    opp = int(row.get("opportunity_score", 0))
    risk = int(row.get("structural_risk_score", 0))

    # Data quality proxy for confidence: based on missing key features
    missing = 0
    for k in ["pct_off_52w_high", "trend_200", "atr_pct"]:
        if pd.isna(row.get(k, np.nan)):
            missing += 1
    conf = 0.85 - 0.20 * missing
    conf = float(np.clip(conf, 0.25, 0.90))

    reasons: list[str] = []
    if opp >= 75:
        reasons.append("OPP_HIGH")
    elif opp >= 60:
        reasons.append("OPP_MID")
    else:
        reasons.append("OPP_LOW")

    if risk >= 85:
        reasons.append("RISK_VERY_HIGH")
    elif risk >= 70:
        reasons.append("RISK_HIGH")
    else:
        reasons.append("RISK_OK")

    # Classification logic: risk can override opportunity
    if risk >= CONFIG.red_min_risk:
        label = "🔴"
        bias = "avoid"
        reasons.append("CLASS_RED")
    elif risk >= CONFIG.orange_min_risk:
        label = "🟠"
        bias = "reduce"
        reasons.append("CLASS_ORANGE")
    else:
        if opp >= CONFIG.green_min_opp:
            label = "🟢"
            bias = "deploy"
            reasons.append("CLASS_GREEN")
        elif opp >= CONFIG.yellow_min_opp:
            label = "🟡"
            bias = "watch"
            reasons.append("CLASS_YELLOW")
        else:
            label = "🔵"
            bias = "hold"
            reasons.append("CLASS_BLUE")

    return label, conf, bias, reasons