# scripts/build_universe.py
from __future__ import annotations

import argparse
import io
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# These always go first (and will be included even if filtering is enabled)
ALWAYS_INCLUDE = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLI", "XLY", "XLP", "XLV", "XLU", "XLB", "XLRE",
    "VTI", "VT", "VXUS", "VEA", "VWO",
    "BND", "TLT", "UUP", "^VIX", "GLD", "SLV", "USO", "BTC-USD",
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

# Allow common Yahoo-style symbols:
# - letters/numbers
# - ^ prefix for indices (e.g. ^VIX)
# - hyphen for share classes and Yahoo formats (BRK-B)
# - dot sometimes appears in sources (we convert . -> -)
_ALLOWED = re.compile(r"[^A-Z0-9\-\^\.]")


def _download_text(url: str) -> str:
    """
    Fetch text with a tiny retry loop (NASDAQ Trader occasionally flakes).
    """
    sess = requests.Session()
    headers = {"User-Agent": "HMoney/1.0 (universe builder)"}

    last_err = None
    for attempt in range(1, 4):
        try:
            r = sess.get(url, timeout=30, headers=headers)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(attempt * 1.5)
    raise RuntimeError(f"Failed to download: {url} ({last_err})")


def _read_pipe_table(text: str) -> pd.DataFrame:
    """
    NASDAQ Trader SymDir files are pipe-delimited with footer lines.
    Stop at 'File Creation Time' footer.
    """
    lines: List[str] = []
    for line in text.splitlines():
        if line.startswith("File Creation Time"):
            break
        if line.strip():
            lines.append(line)
    cleaned = "\n".join(lines)
    return pd.read_csv(io.StringIO(cleaned), sep="|")


def _clean_symbol(sym) -> str:
    if sym is None:
        return ""
    try:
        if pd.isna(sym):
            return ""
    except Exception:
        pass

    s = str(sym).strip().upper()
    if not s or s == "NAN":
        return ""

    # Convert class-share dot format to Yahoo hyphen format: BRK.B -> BRK-B
    s = s.replace(".", "-")

    # Remove disallowed characters
    s = _ALLOWED.sub("", s)

    # Collapse repeated hyphens
    s = re.sub(r"-{2,}", "-", s)

    return s


def _get_cols(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
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


def _to_bool_test_issue(v) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    if s == "Y":
        return True
    if s == "N":
        return False
    return None


def _name_has_bad_hint(name: str) -> bool:
    n = (name or "").upper()
    return any(hint in n for hint in BAD_NAME_HINTS)


def _is_yahoo_friendly(sym: str, name: Optional[str]) -> bool:
    """
    Optional filter: keep things that are more likely to resolve in yfinance.
    This is NOT used for the default "max catalog" build.
    """
    if not sym:
        return False

    # Very long tickers are commonly preferred/notes/structured products
    # which can be flaky on Yahoo; keep them out *if* filtering is enabled.
    if len(sym) > 10:
        return False

    # Common instrument suffixes that often fail on Yahoo
    if sym.endswith("-U") or sym.endswith("-W") or sym.endswith("-R"):
        return False

    if name and _name_has_bad_hint(name):
        return False

    return True


def _extract_records(df: pd.DataFrame, source: str) -> List[Dict[str, object]]:
    symbol_col, name_col = _get_cols(df)
    if symbol_col is None:
        return []

    # Filter out test issues when possible
    test_col = "Test Issue" if "Test Issue" in df.columns else None

    out: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        raw = row.get(symbol_col)
        sym = _clean_symbol(raw)

        if not sym:
            continue

        # Drop explicit test issues (best practice even for max catalog)
        if test_col is not None:
            is_test = _to_bool_test_issue(row.get(test_col))
            if is_test is True:
                continue

        name = str(row.get(name_col)) if name_col and row.get(name_col) is not None else ""

        exchange = ""
        for c in ("Exchange", "Market Category"):
            if c in df.columns:
                exchange = str(row.get(c) or "").strip()
                break

        etf_flag = None
        if "ETF" in df.columns:
            v = str(row.get("ETF") or "").strip().upper()
            if v == "Y":
                etf_flag = True
            elif v == "N":
                etf_flag = False

        out.append(
            {
                "ticker": sym,
                "source": source,
                "exchange": exchange,
                "security_name": name,
                "is_etf_flag": etf_flag,
            }
        )
    return out


def build_universe(max_symbols: Optional[int] = None, yahoo_friendly_only: bool = False) -> pd.DataFrame:
    nasdaq_text = _download_text(NASDAQ_LISTED_URL)
    other_text = _download_text(OTHER_LISTED_URL)

    nasdaq_df = _read_pipe_table(nasdaq_text)
    other_df = _read_pipe_table(other_text)

    records: List[Dict[str, object]] = []
    records.extend(_extract_records(nasdaq_df, "nasdaqlisted"))
    records.extend(_extract_records(other_df, "otherlisted"))

    # De-dupe by ticker (keep first seen, but we’ll overwrite with non-empty metadata where available)
    by_ticker: Dict[str, Dict[str, object]] = {}
    for r in records:
        t = str(r["ticker"])
        if t not in by_ticker:
            by_ticker[t] = dict(r)
        else:
            # merge metadata (prefer non-empty)
            cur = by_ticker[t]
            for k, v in r.items():
                if k == "ticker":
                    continue
                if cur.get(k) in (None, "", "nan") and v not in (None, "", "nan"):
                    cur[k] = v

    # ALWAYS_INCLUDE first
    final: List[Dict[str, object]] = []
    seen = set()

    for t in ALWAYS_INCLUDE:
        tt = _clean_symbol(t)
        if not tt or tt in seen:
            continue
        seen.add(tt)
        final.append(
            {
                "ticker": tt,
                "source": "always_include",
                "exchange": "",
                "security_name": "",
                "is_etf_flag": None,
            }
        )

    # Then everything else (optionally filtered)
    for t in sorted(by_ticker.keys()):
        if t in seen:
            continue
        r = by_ticker[t]
        if yahoo_friendly_only:
            if not _is_yahoo_friendly(t, str(r.get("security_name") or "")):
                continue
        final.append(r)
        seen.add(t)
        if max_symbols is not None and len(final) >= max_symbols:
            break

    return pd.DataFrame(final)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/universe_all.csv")
    ap.add_argument("--max", type=int, default=None, help="Optional cap on output rows (default: no cap)")
    ap.add_argument(
        "--yahoo-friendly-only",
        action="store_true",
        help="Optional filter for symbols more likely to work on yfinance. Default is OFF (max catalog).",
    )
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = build_universe(max_symbols=args.max, yahoo_friendly_only=args.yahoo_friendly_only)
    df.to_csv(out_path, index=False)

    print(f"Wrote {len(df)} rows -> {out_path}")
    if args.yahoo_friendly_only:
        print("Mode: yahoo-friendly-only")
    else:
        print("Mode: max-catalog (minimal filtering: removes explicit test issues only)")


if __name__ == "__main__":
    main()