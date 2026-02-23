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

# src/providers/yfinance_provider.py

from datetime import datetime, timezone
from typing import Dict, List, Tuple

import pandas as pd

import yfinance as yf


def fetch_market_snapshot(tickers: List[str]) -> Tuple[pd.DataFrame, Dict]:
    """
    Batch fetch minimal market fields for a list of tickers.

    Returns:
      df with at least: ticker, last_price, pct_change_1d, volume
      quality dict for state.json

    NOTE:
      This is intentionally "minimal friction". You can enrich later (MA/RSI/ATR/etc).
    """
    tickers = [t.upper().strip() for t in tickers if t]
    tickers = list(dict.fromkeys(tickers))  # stable dedupe

    quality = {
        "data_provider": "yfinance",
        "success_rate_pct": None,
        "missing_bars_count": 0,
        "skipped_tickers_count": 0,
        "errors_sample": [],
        "notes": []
    }

    if not tickers:
        return pd.DataFrame(columns=["ticker", "last_price", "pct_change_1d", "volume"]), quality

    # Use 2d daily bars to compute 1d % change cheaply
    try:
        df = yf.download(
            tickers=tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
    except Exception as e:
        quality["notes"].append("yfinance download failed")
        quality["errors_sample"].append({"ticker": "*", "error": str(e)})
        return pd.DataFrame(columns=["ticker", "last_price", "pct_change_1d", "volume"]), quality

    rows = []
    skipped = 0
    missing = 0

    # yfinance returns different shapes depending on single vs multi ticker
    for t in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if t not in df.columns.get_level_values(0):
                    skipped += 1
                    continue
                dft = df[t].dropna()
            else:
                dft = df.dropna()

            if dft.empty or "Close" not in dft.columns:
                missing += 1
                continue

            closes = dft["Close"].astype(float)
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
            pct = 0.0 if prev == 0 else (last / prev - 1.0) * 100.0

            vol = None
            if "Volume" in dft.columns and len(dft["Volume"].dropna()) > 0:
                vol = float(dft["Volume"].iloc[-1])

            rows.append({
                "ticker": t,
                "last_price": last,
                "pct_change_1d": pct,
                "volume": vol,
                "ma_50": None,
                "ma_200": None,
                "rsi_14": None,
                "pct_off_52w_high": None,
                "atr_pct": None
            })
        except Exception as e:
            skipped += 1
            quality["errors_sample"].append({"ticker": t, "error": str(e)})

    out = pd.DataFrame(rows)

    total = len(tickers)
    ok = len(out)
    quality["skipped_tickers_count"] = skipped
    quality["missing_bars_count"] = missing
    quality["success_rate_pct"] = round((ok / total) * 100.0, 1) if total else None

    if ok < total:
        quality["notes"].append("Some tickers missing or skipped; breadth reflects successfully processed symbols.")

    return out, quality

from typing import Dict, List, Tuple
import numpy as np
import pandas as pd
import yfinance as yf


def _chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def _compute_features_from_ohlcv(hist: pd.DataFrame) -> pd.DataFrame:
    """
    hist: yfinance download output, MultiIndex columns (ticker, field) for multi-ticker.
    Returns DataFrame indexed by ticker with:
      ma_200, pct_off_52w_high, atr_pct, dollar_volume_20d
    """
    if hist is None or hist.empty:
        return pd.DataFrame(
            columns=["ma_200", "pct_off_52w_high", "atr_pct", "dollar_volume_20d"]
        )

    # Ensure we have multiindex columns: (ticker, field)
    if not isinstance(hist.columns, pd.MultiIndex):
        # Single-ticker case -> convert to MultiIndex with a dummy ticker
        # Caller should pass multi-ticker lists; but handle safely.
        return pd.DataFrame(
            columns=["ma_200", "pct_off_52w_high", "atr_pct", "dollar_volume_20d"]
        )

    tickers = sorted(set(hist.columns.get_level_values(0)))
    out_rows = []

    for t in tickers:
        try:
            df = hist[t].dropna(how="all").copy()
            if df.empty:
                continue

            # Need at least Close for most metrics
            if "Close" not in df.columns:
                continue

            close = pd.to_numeric(df["Close"], errors="coerce")
            high = pd.to_numeric(df["High"], errors="coerce") if "High" in df.columns else close
            low = pd.to_numeric(df["Low"], errors="coerce") if "Low" in df.columns else close
            volume = pd.to_numeric(df["Volume"], errors="coerce") if "Volume" in df.columns else np.nan

            last_close = close.iloc[-1]
            if pd.isna(last_close) or last_close <= 0:
                continue

            # MA200
            ma_200 = close.rolling(200, min_periods=50).mean().iloc[-1]

            # 52w high proxy: rolling max 252 trading days
            high_52w = close.rolling(252, min_periods=50).max().iloc[-1]
            pct_off_52w_high = None
            if pd.notna(high_52w) and high_52w > 0:
                pct_off_52w_high = float(np.clip((high_52w - last_close) / high_52w, 0.0, 1.0))

            # ATR(14) using True Range
            prev_close = close.shift(1)
            tr = pd.concat(
                [
                    (high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr_14 = tr.rolling(14, min_periods=10).mean().iloc[-1]
            atr_pct = None
            if pd.notna(atr_14) and last_close > 0:
                atr_pct = float((atr_14 / last_close) * 100.0)

            # Dollar volume 20d = avg(Volume * Close) over last 20 days
            dollar_volume_20d = None
            if volume is not None and volume.notna().any():
                dv = (volume * close).rolling(20, min_periods=10).mean().iloc[-1]
                if pd.notna(dv):
                    dollar_volume_20d = float(dv)

            out_rows.append(
                {
                    "ticker": t,
                    "ma_200": float(ma_200) if pd.notna(ma_200) else None,
                    "pct_off_52w_high": float(pct_off_52w_high) if pct_off_52w_high is not None else None,
                    "atr_pct": float(atr_pct) if atr_pct is not None else None,
                    "dollar_volume_20d": float(dollar_volume_20d) if dollar_volume_20d is not None else None,
                }
            )
        except Exception:
            continue

    if not out_rows:
        return pd.DataFrame(columns=["ma_200", "pct_off_52w_high", "atr_pct", "dollar_volume_20d"])

    out = pd.DataFrame(out_rows).set_index("ticker")
    return out


def fetch_history_features(
    tickers: List[str],
    *,
    period: str = "1y",
    interval: str = "1d",
    chunk_size: int = 150,
) -> Tuple[pd.DataFrame, Dict]:
    """
    Pull ~1y daily OHLCV for tickers in chunks (to reduce yfinance failure risk),
    compute feature snapshot per ticker (latest row).
    """
    tickers = [t.upper().strip() for t in tickers if t]
    tickers = list(dict.fromkeys(tickers))

    quality = {
        "history_period": period,
        "history_interval": interval,
        "chunks": 0,
        "ok_tickers": 0,
        "requested_tickers": len(tickers),
        "notes": [],
    }

    if not tickers:
        return pd.DataFrame(columns=["ma_200", "pct_off_52w_high", "atr_pct", "dollar_volume_20d"]), quality

    feats_list = []
    chunks = _chunk(tickers, chunk_size)
    quality["chunks"] = len(chunks)

    for c in chunks:
        try:
            hist = yf.download(
                tickers=c,
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
            feats = _compute_features_from_ohlcv(hist)
            if not feats.empty:
                feats_list.append(feats)
        except Exception:
            # keep going; partial progress is fine
            continue

    if not feats_list:
        quality["notes"].append("No history features computed (yfinance returned empty or failed).")
        return pd.DataFrame(columns=["ma_200", "pct_off_52w_high", "atr_pct", "dollar_volume_20d"]), quality

    out = pd.concat(feats_list, axis=0)
    out = out[~out.index.duplicated(keep="first")]

    quality["ok_tickers"] = int(out.shape[0])
    if quality["ok_tickers"] < quality["requested_tickers"]:
        quality["notes"].append("Some tickers missing history; features are best-effort.")

    return out, quality