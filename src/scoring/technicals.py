from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd

"""
src/scoring/technicals.py

Computes technical indicators from OHLCV.
All functions are deterministic and NaN-safe.
"""





@dataclass
class TechnicalSnapshot:
    last_price: Optional[float]
    volume: Optional[float]
    avg_volume_20d: Optional[float]
    ma_50: Optional[float]
    ma_200: Optional[float]
    rsi_14: Optional[float]
    pct_off_52w_high: Optional[float]  # 0.25 means 25% below 52w high
    atr_pct: Optional[float]  # ATR(14) / last_price


def _safe_last(series: pd.Series) -> Optional[float]:
    if series is None or series.empty:
        return None
    v = series.dropna()
    if v.empty:
        return None
    return float(v.iloc[-1])


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    # Classic Wilder RSI
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val


def atr_pct(df: pd.DataFrame, window: int = 14) -> pd.Series:
    # True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    last_close = close.replace(0, np.nan)
    return atr / last_close


def compute_snapshot(history: pd.DataFrame) -> TechnicalSnapshot:
    """
    Expects OHLCV DataFrame with columns at least:
    Open, High, Low, Close, Volume.
    Index is dates.
    """
    if history is None or history.empty:
        return TechnicalSnapshot(
            last_price=None,
            volume=None,
            avg_volume_20d=None,
            ma_50=None,
            ma_200=None,
            rsi_14=None,
            pct_off_52w_high=None,
            atr_pct=None,
        )

    close = history.get("Close")
    volume = history.get("Volume")

    last_price = _safe_last(close)
    last_vol = _safe_last(volume)

    ma_50_series = sma(close, 50) if close is not None else pd.Series(dtype=float)
    ma_200_series = sma(close, 200) if close is not None else pd.Series(dtype=float)

    rsi_14_series = rsi(close, 14) if close is not None else pd.Series(dtype=float)
    atr_pct_series = atr_pct(history, 14) if {"High", "Low", "Close"}.issubset(history.columns) else pd.Series(dtype=float)

    avg_vol_20 = volume.rolling(20, min_periods=20).mean() if volume is not None else pd.Series(dtype=float)

    # 52w high using ~252 trading days
    if close is not None and len(close.dropna()) >= 2:
        window = min(252, len(close))
        high_52w = close.rolling(window, min_periods=window).max()
        last_high_52w = _safe_last(high_52w)
        if last_high_52w and last_price:
            pct_off_high = max(0.0, (last_high_52w - last_price) / last_high_52w)
        else:
            pct_off_high = None
    else:
        pct_off_high = None

    return TechnicalSnapshot(
        last_price=last_price,
        volume=last_vol,
        avg_volume_20d=_safe_last(avg_vol_20),
        ma_50=_safe_last(ma_50_series),
        ma_200=_safe_last(ma_200_series),
        rsi_14=_safe_last(rsi_14_series),
        pct_off_52w_high=pct_off_high,
        atr_pct=_safe_last(atr_pct_series),
    )