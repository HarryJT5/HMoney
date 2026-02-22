from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
import pandas as pd
import yfinance as yf

"""
src/providers/yfinance_provider.py

Fetches historical OHLCV from yfinance in a batch-friendly way.
Robust to missing tickers; returns what it can.
"""





@dataclass
class FetchResult:
    history_by_symbol: Dict[str, pd.DataFrame]
    errors_by_symbol: Dict[str, str]


def _chunked(items: List[str], chunk_size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


def fetch_history_batch(
    symbols: List[str],
    period: str = "2y",
    interval: str = "1d",
    batch_size: int = 150,
    throttle_sleep_sec: float = 1.0,
) -> FetchResult:
    """
    Fetches OHLCV for many tickers. Uses yfinance.download which can return
    a MultiIndex columns frame when multiple symbols are requested.

    Returns a dict symbol -> DataFrame with columns: Open, High, Low, Close, Adj Close, Volume.
    """
    clean = sorted({s.strip().upper() for s in symbols if s and isinstance(s, str)})
    history_by_symbol: Dict[str, pd.DataFrame] = {}
    errors_by_symbol: Dict[str, str] = {}

    if not clean:
        return FetchResult(history_by_symbol=history_by_symbol, errors_by_symbol={"__all__": "No symbols provided"})

    for chunk in _chunked(clean, batch_size):
        try:
            df = yf.download(
                tickers=chunk,
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
        except Exception as e:
            # Mark all tickers in this chunk as failed
            msg = f"yfinance download failed for chunk ({len(chunk)}): {e}"
            for s in chunk:
                errors_by_symbol[s] = msg
            time.sleep(throttle_sleep_sec)
            continue

        # If single ticker, columns are normal
        if isinstance(df.columns, pd.Index) and not isinstance(df.columns, pd.MultiIndex):
            # Only one ticker in chunk
            s = chunk[0]
            if df.empty:
                errors_by_symbol[s] = "Empty history returned"
            else:
                history_by_symbol[s] = df.copy()
            time.sleep(throttle_sleep_sec)
            continue

        # MultiIndex: top level is ticker, second is field
        if isinstance(df.columns, pd.MultiIndex):
            top_symbols = sorted(set(df.columns.get_level_values(0)))
            for s in chunk:
                if s not in top_symbols:
                    errors_by_symbol[s] = "Ticker missing from batch response"
                    continue
                sub = df[s].copy()
                # Some tickers may return all NaNs
                if sub.dropna(how="all").empty:
                    errors_by_symbol[s] = "All-NaN history returned"
                    continue
                history_by_symbol[s] = sub
        else:
            # Unexpected shape
            for s in chunk:
                errors_by_symbol[s] = "Unexpected yfinance dataframe shape"

        time.sleep(throttle_sleep_sec)

    return FetchResult(history_by_symbol=history_by_symbol, errors_by_symbol=errors_by_symbol)