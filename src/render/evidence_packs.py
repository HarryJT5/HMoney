# src/render/evidence_packs.py
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _utc_iso(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_id_from_out_dir(out_dir: str) -> str:
    """
    Derive run_id from the output directory path.

    Expected out_dir examples:
      - public/evidence_packs/2026-02-22/0923
      - public/evidence_packs/2026-02-22/0923/

    Returns:
      - "2026-02-22/0923" when possible
      - otherwise falls back to the last folder name
    """
    p = Path(out_dir).resolve()
    parts = list(p.parts)

    # Try to find ".../evidence_packs/<YYYY-MM-DD>/<HHMM>/"
    if "evidence_packs" in parts:
        idx = parts.index("evidence_packs")
        if len(parts) >= idx + 3:
            date_part = parts[idx + 1]
            time_part = parts[idx + 2]
            return f"{date_part}/{time_part}"

    # Fallback: use the last two directory names if available
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"

    # Last resort
    return parts[-1] if parts else "unknown"


def write_evidence_pack(
    *,
    out_dir: str,
    as_of_utc: datetime,
    symbol: str,
    asset_type: str,
    market: Dict[str, Any],
    scores: Dict[str, Any],
    classification: Dict[str, Any],
    deployment_bias: str,
    reason_codes: list[str],
    notes: str = "",
    asset_meta: Optional[Dict[str, Any]] = None,
    portfolio_context: Optional[Dict[str, Any]] = None,
    schema_version: str = "1.0.2",
) -> str:
    """
    Writes one evidence pack JSON file. Returns relative filename (e.g., 'AAPL.json').
    """
    symbol = symbol.upper().strip()
    asset_meta = asset_meta or {}

    # Ensure schema fields are always present with safe defaults.
    run_id = _run_id_from_out_dir(out_dir)

    # Ensure market has pct_change_1d even if the provider didn't supply it.
    if "pct_change_1d" not in market:
        market["pct_change_1d"] = None

    # --- Clickable provenance (deterministic, no extra API calls) ---
    # For now, everything is sourced from Yahoo Finance via yfinance, so we can attach canonical URLs.
    # Later you can swap/augment these without changing the rest of the pipeline.
    quote_url = f"https://finance.yahoo.com/quote/{symbol}"
    profile_url = f"https://finance.yahoo.com/quote/{symbol}/profile"

    # Methodology doc in your repo (create this file later; link will still be valid once it exists).
    methodology_base = "https://github.com/HarryJT5/HMoney/blob/main/docs/methodology.md"

    links = {
        "provider": "yfinance",
        "quote": quote_url,
        "profile": profile_url,
        "methodology": methodology_base,
    }

    # Field-level links: lets you click specific numbers in the future dashboard/newsletter.
    field_links = {
        "asset.symbol": quote_url,
        "market.last_price": quote_url,
        "market.pct_change_1d": quote_url,
        "market.volume": quote_url,
        "market.avg_volume_20d": quote_url,
        "market.ma_50": quote_url,
        "market.ma_200": quote_url,
        "market.rsi_14": quote_url,
        "market.pct_off_52w_high": quote_url,
        "market.atr_pct": quote_url,
        "scores.opportunity_score": f"{methodology_base}#opportunity-score",
        "scores.structural_risk_score": f"{methodology_base}#structural-risk-score",
        "scores.discount_score": f"{methodology_base}#discount-score",
        "classification.label": f"{methodology_base}#classifications",
        "deployment_bias": f"{methodology_base}#deployment-bias",
    }

    pack = {
        "schema_version": schema_version,
        "pack_id": str(uuid.uuid4()),
        "run_id": run_id,
        "generated_at_utc": _utc_iso(),
        "as_of_utc": _utc_iso(as_of_utc),
        "data_freshness_sec": market.get("data_freshness_sec", 0),
        "asset": {
            "symbol": symbol,
            "asset_type": asset_type,
            **{k: v for k, v in asset_meta.items() if v is not None},
        },
        **({"portfolio_context": portfolio_context} if portfolio_context else {}),
        "market": market,
        "scores": scores,
        "classification": classification,
        "deployment_bias": deployment_bias,
        "explainability": {
            "reason_codes": reason_codes,
            "notes": notes or "",
        },
        "links": links,
        "field_links": field_links,
    }

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    filename = f"{symbol}.json"
    (out_path / filename).write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return filename