# src/universe/selector.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Set


@dataclass(frozen=True)
class UniverseSelection:
    final: List[str]
    panel: List[str]
    sentinels: List[str]
    forced_movers: List[str]
    rolling_shard: List[str]
    shard_index: int
    shard_count: int
    time_bucket: int
    shards_per_run: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_time_bucket(dt: datetime, cadence_minutes: int = 15) -> int:
    if cadence_minutes <= 0:
        cadence_minutes = 15
    buckets_per_day = (24 * 60) // cadence_minutes
    minute_bucket = (dt.hour * (60 // cadence_minutes)) + (dt.minute // cadence_minutes)
    yyyymmdd = dt.year * 10000 + dt.month * 100 + dt.day
    return yyyymmdd * buckets_per_day + minute_bucket


def _read_lines_csv(path: Path) -> List[str]:
    """
    Ticker-first CSV reader. Works even if there are multiple columns (uses first col).
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: List[str] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if i == 0 and ("symbol" in parts[0].lower() or "ticker" in parts[0].lower()):
            continue
        sym = parts[0].strip().upper()
        if sym and sym.isascii():
            out.append(sym)
    return out


def load_universe_all(universe_csv_path: str) -> List[str]:
    return _read_lines_csv(Path(universe_csv_path))


def load_forced_movers(forced_movers_path: str, max_n: int = 150) -> List[str]:
    p = Path(forced_movers_path)
    if not p.exists():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        tickers = obj.get("tickers", [])
        tickers = [str(t).upper() for t in tickers if t]
        return tickers[:max_n]
    except Exception:
        return []


def _stable_dedupe(seq: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in seq:
        x = (x or "").upper().strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _is_run_eligible(sym: str) -> bool:
    """
    Keep the catalog maximal, but make the per-run universe more Yahoo-friendly.
    Filters common artifacts that frequently fail in yfinance.
    """
    s = (sym or "").upper().strip()
    if not s:
        return False

    # Units/warrants/rights are the biggest failure cluster in your logs
    if s.endswith("-U") or s.endswith("-W") or s.endswith("-R"):
        return False

    # Super long symbols are often preferred/notes/structured products
    if len(s) > 10:
        return False

    # Multiple hyphens often indicates series/rights variants that are flaky
    if s.count("-") >= 2:
        return False

    return True


def _rotate_window(items: List[str], start: int, n: int) -> List[str]:
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return items
    start = start % len(items)
    end = start + n
    if end <= len(items):
        return items[start:end]
    return items[start:] + items[: (end - len(items))]


def select_universe(
    *,
    universe_all: List[str],
    target_size: int = 1100,
    shard_count: int = 10,
    shards_per_run: int = 1,
    cadence_minutes: int = 15,
    panel_tickers: Optional[List[str]] = None,
    sentinel_tickers: Optional[List[str]] = None,
    forced_movers: Optional[List[str]] = None,
    now_utc: Optional[datetime] = None,
    shard_index_override: Optional[int] = None,
) -> UniverseSelection:
    """
    final = panel + sentinels + forced_movers + rolling_shard (deduped) up to target_size

    - shard_index_override lets workflow control which shard runs
    - shards_per_run > 1 fills closer to target_size and increases sweep coverage
    - rotates within shard pool so repeated runs sweep different names
    """
    dt = now_utc or _utc_now()

    shard_count_i = max(1, int(shard_count))
    shards_per_run_i = max(1, min(shard_count_i, int(shards_per_run)))

    time_bucket = compute_time_bucket(dt, cadence_minutes=cadence_minutes)
    if shard_index_override is not None:
        shard_index = int(shard_index_override) % shard_count_i
    else:
        shard_index = time_bucket % shard_count_i

    panel = _stable_dedupe(panel_tickers or [])
    sentinels = _stable_dedupe(sentinel_tickers or [])
    movers = _stable_dedupe(forced_movers or [])

    exclude = set(panel) | set(sentinels) | set(movers)

    # Build eligible pool (selection-time filtering only)
    pool = [
        t for t in _stable_dedupe(universe_all)
        if t not in exclude and _is_run_eligible(t)
    ]

    # Pull multiple shard streams
    shard_pool: List[str] = []
    for k in range(shards_per_run_i):
        idx = (shard_index + k) % shard_count_i
        shard_pool.extend(pool[idx::shard_count_i])
    shard_pool = _stable_dedupe(shard_pool)

    remaining = max(0, int(target_size) - (len(panel) + len(sentinels) + len(movers)))

    # Deterministic rotation to sweep within the shard pool
    start = (time_bucket * 97 + shard_index * 1009)
    shard_window = _rotate_window(shard_pool, start=start, n=remaining)

    final = _stable_dedupe([*panel, *sentinels, *movers, *shard_window])

    if len(final) > target_size:
        final = final[:target_size]
        final_set = set(final)
        panel = [t for t in panel if t in final_set]
        sentinels = [t for t in sentinels if t in final_set]
        movers = [t for t in movers if t in final_set]
        shard_window = [t for t in shard_window if t in final_set]

    return UniverseSelection(
        final=final,
        panel=panel,
        sentinels=sentinels,
        forced_movers=movers,
        rolling_shard=shard_window,
        shard_index=shard_index,
        shard_count=shard_count_i,
        time_bucket=time_bucket,
        shards_per_run=shards_per_run_i,
    )