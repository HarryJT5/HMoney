"""
src/pipeline.py

Main entry point:
- Load portfolio
- Build universe (portfolio + curated + optional sp500 file)
- Fetch OHLCV
- Compute technical snapshots
- Cross-sectional scoring (percentiles)
- Build Evidence Packs (schema-constrained)
- Validate each pack against schema
- Write public outputs
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from jsonschema import Draft202012Validator

from src.config import CONFIG
from src.providers.yfinance_provider import fetch_history_batch
from src.scoring.technicals import compute_snapshot
from src.scoring.classifier import score_universe, classify_row
from src.render.state_builder import build_state
from src.render.newsletter import write_newsletter


# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs() -> None:
    Path(CONFIG.public_dir).mkdir(parents=True, exist_ok=True)
    Path(CONFIG.evidence_dir).mkdir(parents=True, exist_ok=True)


def load_portfolio() -> pd.DataFrame:
    for fname in CONFIG.portfolio_candidates:
        if Path(fname).exists():
            df = pd.read_csv(fname)
            df.columns = [c.strip().lower() for c in df.columns]

            if "ticker" not in df.columns:
                raise ValueError(f"{fname} must include column 'ticker'")
            if "weight_pct" not in df.columns:
                raise ValueError(f"{fname} must include column 'weight_pct'")

            df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
            df["tag"] = df["tag"].astype(str).str.strip() if "tag" in df.columns else ""

            return df[["ticker", "weight_pct", "tag"]]

    return pd.DataFrame(columns=["ticker", "weight_pct", "tag"])


def load_sp500_symbols(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        return []

    df = pd.read_csv(p)
    df.columns = [c.strip().lower() for c in df.columns]

    col = None
    for candidate in ["symbol", "ticker"]:
        if candidate in df.columns:
            col = candidate
            break

    if col is None:
        return []

    return df[col].astype(str).str.strip().str.upper().tolist()


def build_universe(portfolio_df: pd.DataFrame) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    portfolio_map: Dict[str, Dict[str, Any]] = {}

    if not portfolio_df.empty:
        for _, r in portfolio_df.iterrows():
            sym = str(r["ticker"]).upper()
            portfolio_map[sym] = {
                "weight_pct": float(r["weight_pct"]) if not pd.isna(r["weight_pct"]) else 0.0,
                "tag": str(r.get("tag", "") or ""),
            }

    curated = [s.upper() for s in CONFIG.curated_tickers]
    sp500 = load_sp500_symbols(CONFIG.sp500_csv_path) if CONFIG.include_sp500_file else []

    universe = sorted(set(curated + sp500 + list(portfolio_map.keys())))
    return universe, portfolio_map


def load_schema_validator(schema_path: str) -> Draft202012Validator:
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    return validator


# -----------------------------------------------------------
# Evidence Pack Builder
# -----------------------------------------------------------

def build_evidence_pack(
    symbol: str,
    snap,
    scored_row: pd.Series,
    portfolio_ctx: Optional[Dict[str, Any]],
    as_of_utc: str,
    generated_at_utc: str,
) -> Dict[str, Any]:

    opp = int(scored_row.get("opportunity_score", 0))
    risk = int(scored_row.get("structural_risk_score", 0))

    disc_raw = scored_row.get("discount_score", None)
    disc = None if pd.isna(disc_raw) else int(disc_raw)

    label = str(scored_row.get("label", "🔵"))
    conf = float(scored_row.get("confidence", 0.5))
    bias = str(scored_row.get("deployment_bias", "hold"))
    reasons = list(scored_row.get("reason_codes", []))

    pack: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "pack_id": str(uuid.uuid4()),
        "generated_at_utc": generated_at_utc,
        "as_of_utc": as_of_utc,
        "data_freshness_sec": 0,
        "asset": {
            "symbol": symbol,
            "asset_type": "crypto" if "-USD" in symbol else "equity",
        },
        "market": {
            "last_price": snap.last_price if snap.last_price is not None else 0.0,
            "currency": "USD",
            "volume": snap.volume,
            "avg_volume_20d": snap.avg_volume_20d,
            "ma_50": snap.ma_50,
            "ma_200": snap.ma_200,
            "rsi_14": snap.rsi_14,
            "pct_off_52w_high": snap.pct_off_52w_high,
            "atr_pct": snap.atr_pct,
        },
        "fundamentals": None,
        "scores": {
            "opportunity_score": opp,
            "structural_risk_score": risk,
            "discount_score": disc,
        },
        "classification": {"label": label, "confidence": conf},
        "deployment_bias": bias,
        "explainability": {"reason_codes": reasons, "notes": ""},
    }

    if portfolio_ctx is not None:
        pack["portfolio_context"] = {
            "weight_pct": float(portfolio_ctx.get("weight_pct", 0.0)),
            "tag": str(portfolio_ctx.get("tag", "")),
        }

    return pack


# -----------------------------------------------------------
# Main Pipeline
# -----------------------------------------------------------

def main() -> None:
    ensure_dirs()

    generated_at = utc_now_iso()
    as_of = utc_now_iso()

    portfolio_df = load_portfolio()
    universe, portfolio_map = build_universe(portfolio_df)

    if not universe:
        raise RuntimeError("Universe is empty.")

    fetch = fetch_history_batch(
        universe,
        period=CONFIG.yf_period,
        interval=CONFIG.yf_interval,
        batch_size=CONFIG.max_batch_size,
        throttle_sleep_sec=CONFIG.throttle_sleep_sec,
    )

    rows = []
    snapshots: Dict[str, Any] = {}

    for sym in universe:
        hist = fetch.history_by_symbol.get(sym)
        snap = compute_snapshot(hist) if hist is not None else compute_snapshot(pd.DataFrame())
        snapshots[sym] = snap

        feat = {
            "symbol": sym,
            "pct_off_52w_high": snap.pct_off_52w_high,
            "trend_200": None if snap.ma_200 in (None, 0) else (snap.last_price / snap.ma_200 - 1) if snap.last_price else None,
            "atr_pct": snap.atr_pct,
            "dollar_volume_20d": None if snap.avg_volume_20d is None or snap.last_price is None else snap.avg_volume_20d * snap.last_price,
        }

        rows.append(feat)

    feats = pd.DataFrame(rows).set_index("symbol")

    scored = score_universe(feats)

    labels, confs, biases, reasons_list = [], [], [], []

    for sym, row in scored.iterrows():
        label, conf, bias, reasons = classify_row(row)
        labels.append(label)
        confs.append(conf)
        biases.append(bias)
        reasons_list.append(reasons)

    scored["label"] = labels
    scored["confidence"] = confs
    scored["deployment_bias"] = biases
    scored["reason_codes"] = reasons_list

    state = build_state(scored, as_of)
    with open(CONFIG.state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, allow_nan=False)

    validator = load_schema_validator(CONFIG.schema_path)

    errors = 0

    for sym in universe:
        snap = snapshots[sym]
        row = scored.loc[sym]
        portfolio_ctx = portfolio_map.get(sym)

        pack = build_evidence_pack(
            sym,
            snap,
            row,
            portfolio_ctx,
            as_of,
            generated_at,
        )

        validation_errors = sorted(validator.iter_errors(pack), key=lambda e: e.path)

        if validation_errors:
            errors += 1
            msgs = "; ".join(
                f"{'/'.join(str(p) for p in ve.path)}: {ve.message}"
                for ve in validation_errors[:3]
            )
            pack["explainability"]["notes"] = f"Schema validation errors: {msgs}"

        out_path = Path(CONFIG.evidence_dir) / f"{sym}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(pack, f, indent=2, allow_nan=False)

    # Newsletter generation
    write_newsletter(
        state_path=CONFIG.state_path,
        evidence_dir=CONFIG.evidence_dir,
        md_out=str(Path(CONFIG.public_dir) / "daily_brief.md"),
        html_out=str(Path(CONFIG.public_dir) / "daily_brief.html"),
    )

    if errors:
        print(f"[WARN] {errors} packs had schema validation issues.")
    else:
        print("[OK] All packs validated against schema.")


if __name__ == "__main__":
    main()