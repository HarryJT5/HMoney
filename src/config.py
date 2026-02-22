"""
src/config.py

Central configuration for HMoney V1 pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


def _default_curated_tickers() -> List[str]:
    return (
        "SPY QQQ IWM DIA VOO".split()
        + "AAPL MSFT NVDA AMZN GOOGL META TSLA".split()
        + "JPM BAC GS XLF".split()
        + "XLE XLI XLK XLV XLY XLP XLU".split()
        + "TLT IEF SHY".split()
        + "GLD SLV".split()
        + "BTC-USD ETH-USD".split()
    )


@dataclass(frozen=True)
class Config:
    # Universe sources
    include_sp500_file: bool = True
    sp500_csv_path: str = "data/sp500.csv"  # optional; pipeline works without it

    curated_tickers: List[str] = field(default_factory=_default_curated_tickers)

    # yfinance settings
    yf_period: str = "2y"
    yf_interval: str = "1d"
    max_batch_size: int = 150
    throttle_sleep_sec: float = 1.0

    # Output paths
    schema_path: str = "schema/evidence_pack.schema.json"
    public_dir: str = "public"
    evidence_dir: str = "public/evidence_packs"
    state_path: str = "public/state.json"

    # Portfolio input (support either casing)
    portfolio_candidates: List[str] = field(default_factory=lambda: ["portfolio.csv", "Portfolio.CSV"])

    # Scoring weights (cross-sectional)
    opp_w_discount: float = 0.40
    opp_w_trend: float = 0.40
    opp_w_low_vol: float = 0.20

    risk_w_vol: float = 0.50
    risk_w_drawdown: float = 0.30
    risk_w_illiquidity: float = 0.20

    # Classification thresholds
    green_min_opp: int = 75
    yellow_min_opp: int = 60
    orange_min_risk: int = 70
    red_min_risk: int = 85

    # Market bias thresholds
    market_green_min: int = 65
    market_red_max: int = 45


CONFIG = Config()