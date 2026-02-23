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
    Forces numeric conversion so None -> NaN safely.
    """
    s = pd.to_numeric(series, errors="coerce")

    if not higher_is_better:
        s = -s

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
    df = features.copy()

    # Percentiles (0..1)
    df["p_discount"] = _pct_rank(df["pct_off_52w_high"], higher_is_better=True)   # higher = more off highs
    df["p_trend"] = _pct_rank(df["trend_200"], higher_is_better=True)            # higher = better trend
    df["p_low_vol"] = _pct_rank(df["atr_pct"], higher_is_better=False)           # lower vol => higher percentile
    df["p_vol_risk"] = _pct_rank(df["atr_pct"], higher_is_better=True)           # higher vol => higher risk

    has_liq = "dollar_volume_20d" in df.columns and df["dollar_volume_20d"].notna().any()
    if has_liq:
        df["p_liquidity_good"] = _pct_rank(df["dollar_volume_20d"], higher_is_better=True)
        df["p_illiquidity_risk"] = 1.0 - df["p_liquidity_good"]
    else:
        df["p_liquidity_good"] = np.nan
        df["p_illiquidity_risk"] = np.nan

    # Opportunity components
    opp_components = {
        "discount": (df["p_discount"], CONFIG.opp_w_discount),
        "trend": (df["p_trend"], CONFIG.opp_w_trend),
        "low_vol": (df["p_low_vol"], CONFIG.opp_w_low_vol),
    }

    # Structural risk components
    risk_components = {
        "vol": (df["p_vol_risk"], CONFIG.risk_w_vol),
        "drawdown_proxy": (df["p_discount"], CONFIG.risk_w_drawdown),
    }
    if has_liq:
        risk_components["illiquidity"] = (df["p_illiquidity_risk"], CONFIG.risk_w_illiquidity)

    def weighted_mean(components: dict) -> pd.Series:
        num = None
        den = None
        for _, (s, w) in components.items():
            s = s.astype(float)
            if num is None:
                num = s * w
                den = s.notna().astype(float) * w
            else:
                num = num + s * w
                den = den + s.notna().astype(float) * w
        return num / den.replace(0, np.nan)

    df["opportunity_raw"] = weighted_mean(opp_components)
    df["structural_risk_raw"] = weighted_mean(risk_components)

    df["opportunity_score"] = df["opportunity_raw"].apply(_to_0_100)
    df["structural_risk_score"] = df["structural_risk_raw"].apply(_to_0_100)

    df["discount_score"] = df["p_discount"].apply(lambda x: None if pd.isna(x) else _to_0_100(x))

    # For later: keep a component-coverage measure for confidence
    df["_opp_components_present"] = (
        df["p_discount"].notna().astype(int)
        + df["p_trend"].notna().astype(int)
        + df["p_low_vol"].notna().astype(int)
    )
    df["_risk_components_present"] = (
        df["p_vol_risk"].notna().astype(int)
        + df["p_discount"].notna().astype(int)
        + (df["p_illiquidity_risk"].notna().astype(int) if has_liq else 0)
    )

    return df


def classify_row(row: pd.Series) -> Tuple[str, float, str, list[str]]:
    opp = int(row.get("opportunity_score", 0))
    risk = int(row.get("structural_risk_score", 0))

    # Confidence = data completeness proxy (diagnostic)
    opp_present = int(row.get("_opp_components_present", 0))
    risk_present = int(row.get("_risk_components_present", 0))
    # 3 opp comps, 2–3 risk comps
    max_present = 6
    present = min(max_present, opp_present + risk_present)
    conf = 0.25 + (present / max_present) * 0.65  # 0.25..0.90
    conf = float(np.clip(conf, 0.25, 0.90))

    reasons: list[str] = []
    reasons.append("OPP_HIGH" if opp >= 75 else "OPP_MID" if opp >= 60 else "OPP_LOW")
    reasons.append("RISK_VERY_HIGH" if risk >= 85 else "RISK_HIGH" if risk >= 70 else "RISK_OK")

    # Risk overrides
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