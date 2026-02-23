# scripts/build_universe.py
from __future__ import annotations

import argparse
import io
import re
from pathlib import Path
from typing import List, Set, Tuple

import pandas as pd
import requests


NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

ALWAYS_INCLUDE = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLI", "XLY", "XLP", "XLV", "XLU", "XLB", "XLRE",
]


BAD_NAME_HINTS = (
    "WARRANT", "WTS",
    "RIGHT",
    "UNIT",
    "PREFERRED", "PFD", "PREF",
    "NOTES", "NOTE",
    "DEPOSITARY",
    "SERIES",
)


def _download_text(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def _clean_symbol(sym) -> str:
    if sym is None:
        return ""
    try:
        if pd.isna(sym):
            return ""
    except Exception:
        pass

    sym = str(sym).strip().upper()
    if not sym or sym == "NAN":
        return ""

    # BRK.B -> BRK-B (Yahoo class share format)
    sym = sym.replace(".", "-")

    # Only allow A-Z, 0-9, hyphen
    sym = re.sub(r"[^A-Z0-9\-]", "", sym)
    return sym


def _read_pipe_table(text: str) -> pd.DataFrame:
    lines = []
    for line in text.splitlines():
        if line.startswith("File Creation Time"):
            break
        if line.strip():
            lines.append(line)
    cleaned = "\n".join(lines)
    return pd.read_csv(io.StringIO(cleaned), sep="|")


def _get_cols(df: pd.DataFrame) -> Tuple[str | None, str | None]:
    """
    Returns (symbol_col, name_col) where name_col is Security Name if present.
    """
    symbol_col = None
    for c in ("Symbol", "ACT Symbol"):
        if c in df.columns:
            symbol_col = c
            break

    name_col = None
    for c in ("Security Name", "SecurityName"):
        if c in df.columns:
            name_col = c
            break

    return symbol_col, name_col


def _is_yahoo_friendly(sym: str, name: str | None) -> bool:
    # Basic symbol sanity
    if not sym:
        return False

    # Exclude extreme long / weird symbols
    if len(sym) > 10:
        return False

    # Exclude common “instrument suffix” patterns that often fail on Yahoo
    # (Units/Warrants/Rights often show up as -U, -W, -R or similar in some feeds)
    if sym.endswith("-U") or sym.endswith("-W") or sym.endswith("-R"):
        return False

    # Name-based filtering (best win)
    if name:
        n = str(name).upper()
        for hint in BAD_NAME_HINTS:
            if hint in n:
                return False

    return True


def _extract_symbols(df: pd.DataFrame) -> List[str]:
    symbol_col, name_col = _get_cols(df)
    if symbol_col is None:
        return []

    # Filter out test issues if present
    if "Test Issue" in df.columns:
        mask = df["Test Issue"].astype(str).str.upper().fillna("") == "N"
        df = df.loc[mask].copy()

    out: List[str] = []
    for _, row in df.iterrows():
        sym = _clean_symbol(row.get(symbol_col))
        name = row.get(name_col) if name_col else None
        if _is_yahoo_friendly(sym, name):
            out.append(sym)
    return out


def build_universe(max_symbols: int | None = None) -> List[str]:
    nasdaq_text = _download_text(NASDAQ_LISTED_URL)
    other_text = _download_text(OTHER_LISTED_URL)

    nasdaq_df = _read_pipe_table(nasdaq_text)
    other_df = _read_pipe_table(other_text)

    syms: List[str] = []
    syms.extend(_extract_symbols(nasdaq_df))
    syms.extend(_extract_symbols(other_df))

    seen: Set[str] = set()
    final: List[str] = []

    for t in ALWAYS_INCLUDE:
        t = _clean_symbol(t)
        if t and t not in seen:
            seen.add(t)
            final.append(t)

    for s in syms:
        if not s or s in seen:
            continue
        seen.add(s)
        final.append(s)
        if max_symbols is not None and len(final) >= max_symbols:
            break

    return final


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/universe_all.csv")
    ap.add_argument("--max", type=int, default=6000)
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tickers = build_universe(max_symbols=args.max)
    pd.DataFrame({"ticker": tickers}).to_csv(out_path, index=False)

    print(f"Wrote {len(tickers)} symbols -> {out_path}")


if __name__ == "__main__":
    main()