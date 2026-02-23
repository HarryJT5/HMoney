# src/universe/selector.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Dict, Optional, Set


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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_time_bucket(dt: datetime, cadence_minutes: int = 15) -> int:
    """
    Deterministic bucket. For 15-min cadence there are 96 buckets per day.
    """
    if cadence_minutes <= 0:
        cadence_minutes = 15
    buckets_per_day = (24 * 60) // cadence_minutes
    minute_bucket = (dt.hour * (60 // cadence_minutes)) + (dt.minute // cadence_minutes)

    yyyymmdd = dt.year * 10000 + dt.month * 100 + dt.day
    return yyyymmdd * buckets_per_day + minute_bucket


def compute_shard_index(dt: datetime, shard_count: int, cadence_minutes: int = 15) -> tuple[int, int]:
    shard_count = max(1, int(shard_count))
    tb = compute_time_bucket(dt, cadence_minutes=cadence_minutes)
    return (tb % shard_count), tb


def _read_lines_csv(path: Path) -> List[str]:
    """
    Minimal CSV reader for a single-column or symbol-first CSV.
    Falls back to naive splitting and ignores header.
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


def select_universe(
    *,
    universe_all: List[str],
    target_size: int = 1100,
    shard_count: int = 10,
    cadence_minutes: int = 15,
    panel_tickers: Optional[List[str]] = None,
    sentinel_tickers: Optional[List[str]] = None,
    forced_movers: Optional[List[str]] = None,
    now_utc: Optional[datetime] = None,
) -> UniverseSelection:
    """
    final = panel + sentinels + forced_movers + rolling_shard (deduped) up to target_size
    rolling_shard is a deterministic slice of universe_all based on shard_index.
    """
    dt = now_utc or _utc_now()
    shard_index, time_bucket = compute_shard_index(dt, shard_count=shard_count, cadence_minutes=cadence_minutes)

    panel = _stable_dedupe(panel_tickers or [])
    sentinels = _stable_dedupe(sentinel_tickers or [])
    movers = _stable_dedupe(forced_movers or [])

    # Remove anything already in panel/sentinels/movers from the shard pool
    exclude = set(panel) | set(sentinels) | set(movers)
    pool = [t for t in _stable_dedupe(universe_all) if t not in exclude]

    # Deterministic shard slicing: take every K-th item starting at shard_index
    # This avoids needing equal chunk sizes and stays stable if universe list grows.
    shard = pool[shard_index::max(1, shard_count)]
    shard = _stable_dedupe(shard)

    # Build final up to target_size
    final = _stable_dedupe([*panel, *sentinels, *movers, *shard])
    if len(final) > target_size:
        # Preserve priority ordering: panel → sentinels → movers → shard
        final = final[:target_size]

        # Recompute what actually made it in (for counts)
        final_set = set(final)
        panel = [t for t in panel if t in final_set]
        sentinels = [t for t in sentinels if t in final_set]
        movers = [t for t in movers if t in final_set]
        shard = [t for t in shard if t in final_set]

    return UniverseSelection(
        final=final,
        panel=panel,
        sentinels=sentinels,
        forced_movers=movers,
        rolling_shard=shard,
        shard_index=shard_index,
        shard_count=max(1, int(shard_count)),
        time_bucket=time_bucket,
    )