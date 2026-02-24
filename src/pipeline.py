# src/pipeline.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from src.universe.selector import load_universe_all, load_forced_movers, select_universe
from src.universe.forced_movers import compute_forced_movers_from_prices, write_forced_movers_json
from src.render.state_builder import build_state_json
from src.render.evidence_packs import write_evidence_pack
from src.render.newsletter import write_newsletter
from src.providers.yfinance_provider import fetch_market_snapshot, fetch_history_features
from src.scoring.classifier import score_universe, classify_row


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _top(df: pd.DataFrame, col: str, n: int = 25, ascending: bool = False) -> List[Dict[str, Any]]:
    if df.empty or col not in df.columns:
        return []

    d = df.sort_values(col, ascending=ascending).head(n).copy()
    rows: List[Dict[str, Any]] = []
    for _, r in d.iterrows():
        rows.append(
            {
                "ticker": str(r.get("ticker", "")).upper(),
                "last": float(r["last_price"]) if pd.notna(r.get("last_price", np.nan)) else None,
                "pct_change_1d": float(r["pct_change_1d"]) if pd.notna(r.get("pct_change_1d", np.nan)) else None,
                "opportunity_score": int(r.get("opportunity_score", 0)) if pd.notna(r.get("opportunity_score", 0)) else 0,
                "risk_score": int(r.get("structural_risk_score", 0)) if pd.notna(r.get("structural_risk_score", 0)) else 0,
                "summary": "Diagnostic snapshot based on recent price behavior relative to its own history.",
                "tags": [],
            }
        )
    return rows


def main() -> None:
    mode = os.getenv("HMONEY_MODE", "intraday")
    cadence = int(os.getenv("HMONEY_CADENCE_MIN", "5" if mode == "intraday" else "15"))
    shard_count = int(os.getenv("UNIVERSE_SHARDS", "10"))
    target_size = int(os.getenv("HMONEY_TARGET_SIZE", "1100"))
    shard_index_env = os.getenv("UNIVERSE_SHARD_INDEX", "").strip()
    shard_index_override = int(shard_index_env) if shard_index_env else None

    # default: intraday uses 2 shards/run to get closer to target_size; override via env anytime
    shards_per_run = int(os.getenv("HMONEY_SHARDS_PER_RUN", "2" if mode == "intraday" else "1"))

    # Breadth thresholds (dashboard bars only)
    opp_green_threshold = int(os.getenv("HMONEY_OPP_GREEN_THRESHOLD", "60"))
    risk_red_threshold = int(os.getenv("HMONEY_RISK_RED_THRESHOLD", "70"))

    universe_csv = os.getenv("HMONEY_UNIVERSE_CSV", "data/universe_all.csv")
    forced_movers_path = os.getenv("HMONEY_FORCED_MOVERS_JSON", "data/forced_movers.json")

    # ✅ IMPORTANT: default publish path = docs/ (GitHub Pages)
    evidence_base = os.getenv("HMONEY_EVIDENCE_PACK_BASE", "docs/evidence_packs")
    brief_md = os.getenv("HMONEY_DAILY_BRIEF_MD", "docs/daily_brief.md")
    brief_html = os.getenv("HMONEY_DAILY_BRIEF_HTML", "docs/daily_brief.html")
    state_path = os.getenv("HMONEY_STATE_JSON", "docs/state.json")

    now = _utc_now()
    pack_dir = f"{evidence_base}/{now.strftime('%Y-%m-%d')}/{now.strftime('%H%M')}"

    # --- Macro panel (always included) ---
    sentinels = [
        "SPY", "QQQ", "DIA", "IWM", "VTI",
        "VT", "VXUS", "VEA", "VWO",
        "BND", "TLT",
        "UUP", "^VIX",
        "GLD", "SLV", "USO",
        "BTC-USD",
    ]
    panel = sentinels[:]

    universe_all = load_universe_all(universe_csv)
    forced_movers = load_forced_movers(forced_movers_path, max_n=150)

    selection = select_universe(
    universe_all=universe_all,
    target_size=target_size,
    shard_count=shard_count,
    shards_per_run=shards_per_run,
    cadence_minutes=cadence,
    panel_tickers=panel,
    sentinel_tickers=sentinels,
    forced_movers=forced_movers,
    now_utc=now,
    shard_index_override=shard_index_override,
)

    # --- Snapshot fetch ---
    df_market, quality = fetch_market_snapshot(selection.final)

    # --- History feature fetch ---
    df_hist_feats, hist_q = fetch_history_features(
        selection.final,
        period="1y",
        interval="1d",
        chunk_size=150,
    )

    # Merge history features into df_market safely (avoid _x/_y collisions)
    if df_market is not None and not df_market.empty and df_hist_feats is not None and not df_hist_feats.empty:
        df_market = df_market.copy()
        df_market["ticker"] = df_market["ticker"].astype(str).str.upper()

        df_market = df_market.merge(
            df_hist_feats.reset_index(),
            on="ticker",
            how="left",
            suffixes=("", "_hist"),
        )

        for c in ["ma_200", "pct_off_52w_high", "atr_pct", "dollar_volume_20d"]:
            hist_c = f"{c}_hist"
            if hist_c in df_market.columns:
                if c in df_market.columns:
                    df_market[c] = df_market[hist_c].combine_first(df_market[c])
                else:
                    df_market[c] = df_market[hist_c]
                df_market = df_market.drop(columns=[hist_c])

    # Attach history notes to quality
    if hist_q and "notes" in hist_q:
        quality = quality or {}
        quality.setdefault("notes", [])
        quality["notes"].extend(hist_q.get("notes", []))

    # If still no data, write minimal state and exit
    if df_market is None or df_market.empty:
        build_state_json(
            as_of_utc=now,
            legacy_market_bias="🔵",
            legacy_opportunity_score=0,
            legacy_structural_risk_score=0,
            legacy_counts_by_label={"🟢": 0, "🟡": 0, "🔵": 0, "🟠": 0, "🔴": 0},
            selection_meta={
                "mode": mode,
                "cadence_minutes": cadence,
                "rotation": "per_run",
                "target_size": target_size,
                "actual_size": 0,
                "panel_count": len(selection.panel),
                "sentinels_count": len(selection.sentinels),
                "forced_movers_count": len(selection.forced_movers),
                "rolling_shard_count": len(selection.rolling_shard),
                "panel_tickers": selection.panel,
                "sentinel_tickers": selection.sentinels,
                "forced_movers_tickers": selection.forced_movers,
                "rolling_shard_tickers": selection.rolling_shard,
                "shard_count": selection.shard_count,
                "shard_index": selection.shard_index,
                "shards_per_run": getattr(selection, "shards_per_run", 1),
                "time_bucket": selection.time_bucket,
                "coverage_note": "No market data returned in this run.",
                "opp_green_threshold": opp_green_threshold,
                "risk_red_threshold": risk_red_threshold,
                "data_provider": (quality or {}).get("data_provider", "yfinance"),
                "success_rate_pct": (quality or {}).get("success_rate_pct"),
                "missing_bars_count": (quality or {}).get("missing_bars_count"),
                "skipped_tickers_count": (quality or {}).get("skipped_tickers_count"),
                "errors_sample": (quality or {}).get("errors_sample", []),
                "quality_notes": (quality or {}).get("notes", []),
            },
            df_scores=pd.DataFrame(),
            tables={"pulled_back": [], "fragile": [], "mixed": []},
            evidence_pack_base_path=pack_dir,
            n_packs_written=0,
            out_path=state_path,
        )

        try:
            write_newsletter(state_path, pack_dir, brief_md, brief_html)
        except Exception:
            pass
        return

    # Normalize ticker casing
    df_market["ticker"] = df_market["ticker"].astype(str).str.upper()

    # --- Build features for classifier ---
    df_features = pd.DataFrame({"ticker": df_market["ticker"]})
    df_features["pct_off_52w_high"] = df_market["pct_off_52w_high"] if "pct_off_52w_high" in df_market.columns else np.nan

    if "ma_200" in df_market.columns and "last_price" in df_market.columns:
        ma200 = pd.to_numeric(df_market["ma_200"], errors="coerce")
        lastp = pd.to_numeric(df_market["last_price"], errors="coerce")
        df_features["trend_200"] = (lastp / ma200) - 1.0
    else:
        df_features["trend_200"] = np.nan

    df_features["atr_pct"] = df_market["atr_pct"] if "atr_pct" in df_market.columns else np.nan

    if "dollar_volume_20d" in df_market.columns:
        df_features["dollar_volume_20d"] = df_market["dollar_volume_20d"]
    elif "volume" in df_market.columns and "last_price" in df_market.columns:
        vol = pd.to_numeric(df_market["volume"], errors="coerce")
        lastp = pd.to_numeric(df_market["last_price"], errors="coerce")
        df_features["dollar_volume_20d"] = vol * lastp
    else:
        df_features["dollar_volume_20d"] = np.nan

    df_features = df_features.set_index("ticker")

    # --- Score & classify ---
    df_scored = score_universe(df_features)

    labels: List[str] = []
    confs: List[float] = []
    biases: List[str] = []
    reasons: List[list[str]] = []

    for _, row in df_scored.iterrows():
        lab, conf, bias, reason_codes = classify_row(row)
        labels.append(lab)
        confs.append(conf)
        biases.append(bias)
        reasons.append(reason_codes)

    df_scored["label"] = labels
    df_scored["confidence"] = confs
    df_scored["deployment_bias"] = biases
    df_scored["reason_codes"] = reasons

    df_scores = df_scored.reset_index()[
        [
            "ticker",
            "opportunity_score",
            "structural_risk_score",
            "discount_score",
            "label",
            "confidence",
            "deployment_bias",
            "reason_codes",
        ]
    ].copy()

    # Merge scores into market rows
    df_out = df_market.merge(df_scores, on="ticker", how="left")
    df_out["opportunity_score"] = df_out["opportunity_score"].fillna(0).astype(int)
    df_out["structural_risk_score"] = df_out["structural_risk_score"].fillna(0).astype(int)

    # Tables
    tables = {
        "pulled_back": _top(df_out, "opportunity_score", n=25, ascending=False),
        "fragile": _top(df_out, "structural_risk_score", n=25, ascending=False),
        "mixed": [],
    }

    # Evidence packs for surfaced tickers
    surfaced: set[str] = set()
    for k in ("pulled_back", "fragile", "mixed"):
        for row in tables.get(k, []):
            surfaced.add(row["ticker"])
    for t in selection.panel:
        surfaced.add(t)
    for t in selection.forced_movers:
        surfaced.add(t)

    n_packs_written = 0
    by_ticker: Dict[str, pd.Series] = {str(r["ticker"]).upper(): r for _, r in df_out.iterrows()}

    for t in sorted(surfaced):
        r = by_ticker.get(t)
        if r is None:
            continue

        market = {
            "last_price": float(r["last_price"]) if pd.notna(r.get("last_price", np.nan)) else 0.0,
            "pct_change_1d": float(r["pct_change_1d"]) if pd.notna(r.get("pct_change_1d", np.nan)) else None,
            "currency": "USD",
            "volume": float(r["volume"]) if pd.notna(r.get("volume", np.nan)) else None,
            "avg_volume_20d": None,
            "ma_50": float(r["ma_50"]) if pd.notna(r.get("ma_50", np.nan)) else None,
            "ma_200": float(r["ma_200"]) if pd.notna(r.get("ma_200", np.nan)) else None,
            "rsi_14": float(r["rsi_14"]) if pd.notna(r.get("rsi_14", np.nan)) else None,
            "pct_off_52w_high": float(r["pct_off_52w_high"]) if pd.notna(r.get("pct_off_52w_high", np.nan)) else None,
            "atr_pct": float(r["atr_pct"]) if pd.notna(r.get("atr_pct", np.nan)) else None,
        }

        scores = {
            "opportunity_score": int(r.get("opportunity_score", 0)),
            "structural_risk_score": int(r.get("structural_risk_score", 0)),
            "discount_score": int(r.get("discount_score")) if pd.notna(r.get("discount_score", np.nan)) else None,
        }

        classification = {
            "label": str(r.get("label", "🔵")),
            "confidence": float(r.get("confidence", 0.5)) if pd.notna(r.get("confidence", 0.5)) else 0.5,
        }

        deployment_bias = str(r.get("deployment_bias", "hold"))

        reason_codes = r.get("reason_codes", [])
        if not isinstance(reason_codes, list):
            reason_codes = []

        write_evidence_pack(
            out_dir=pack_dir,
            as_of_utc=now,
            symbol=t,
            asset_type="equity",
            market=market,
            scores=scores,
            classification=classification,
            deployment_bias=deployment_bias,
            reason_codes=reason_codes,
            notes="",
        )
        n_packs_written += 1

    # Update forced movers
    movers = compute_forced_movers_from_prices(df_market, max_n=120)
    write_forced_movers_json(forced_movers_path, movers)

    # ---- Market mini summaries for state.json (benchmarks + vol proxies)
    by_mkt: Dict[str, pd.Series] = {str(r["ticker"]).upper(): r for _, r in df_market.iterrows()}

    def _mini(t: str) -> Dict[str, Any]:
        r = by_mkt.get(t)
        if r is None:
            return {"ticker": t, "last": None, "pct_change_1d": None}
        last = float(r["last_price"]) if pd.notna(r.get("last_price", np.nan)) else None
        chg = float(r["pct_change_1d"]) if pd.notna(r.get("pct_change_1d", np.nan)) else None
        return {"ticker": t, "last": last, "pct_change_1d": chg}

    benchmarks = [_mini(t) for t in ["SPY", "QQQ", "DIA", "IWM", "VTI", "VT", "VXUS", "VEA", "VWO"]]
    volatility_proxies = [_mini(t) for t in ["BND", "TLT", "UUP", "^VIX", "GLD", "SLV", "USO", "BTC-USD"]]
    market_primary_benchmark = "SPY"
    market_pct_change_1d = next((b["pct_change_1d"] for b in benchmarks if b["ticker"] == market_primary_benchmark), None)

    # State meta
    selection_meta = {
        "mode": mode,
        "cadence_minutes": cadence,
        "rotation": "per_run",
        "target_size": target_size,
        "actual_size": len(selection.final),
        "panel_count": len(selection.panel),
        "sentinels_count": len(selection.sentinels),
        "forced_movers_count": len(selection.forced_movers),
        "rolling_shard_count": len(selection.rolling_shard),
        "panel_tickers": selection.panel,
        "sentinel_tickers": selection.sentinels,
        "forced_movers_tickers": selection.forced_movers,
        "rolling_shard_tickers": selection.rolling_shard,
        "shard_count": selection.shard_count,
        "shard_index": selection.shard_index,
        "time_bucket": selection.time_bucket,
        "coverage_note": "Sample includes a fixed panel plus a rolling shard and forced movers.",
        "opp_green_threshold": opp_green_threshold,
        "risk_red_threshold": risk_red_threshold,
        "data_provider": (quality or {}).get("data_provider", "yfinance"),
        "success_rate_pct": (quality or {}).get("success_rate_pct"),
        "missing_bars_count": (quality or {}).get("missing_bars_count"),
        "skipped_tickers_count": (quality or {}).get("skipped_tickers_count"),
        "errors_sample": (quality or {}).get("errors_sample", []),
        "quality_notes": (quality or {}).get("notes", []),

        # NEW optional market section
        "benchmarks": benchmarks,
        "volatility_proxies": volatility_proxies,
        "market_primary_benchmark": market_primary_benchmark,
        "market_pct_change_1d": market_pct_change_1d,
    }

    legacy_market_bias = "🔵"
    legacy_opportunity_score = int(df_scores["opportunity_score"].median()) if not df_scores.empty else 0
    legacy_structural_risk_score = int(df_scores["structural_risk_score"].median()) if not df_scores.empty else 0

    vc = df_scores["label"].value_counts().to_dict() if "label" in df_scores.columns else {}
    counts = {
        "🟢": int(vc.get("🟢", 0)),
        "🟡": int(vc.get("🟡", 0)),
        "🔵": int(vc.get("🔵", 0)),
        "🟠": int(vc.get("🟠", 0)),
        "🔴": int(vc.get("🔴", 0)),
    }

    build_state_json(
        as_of_utc=now,
        legacy_market_bias=legacy_market_bias,
        legacy_opportunity_score=legacy_opportunity_score,
        legacy_structural_risk_score=legacy_structural_risk_score,
        legacy_counts_by_label=counts,
        selection_meta=selection_meta,
        df_scores=df_scores,
        tables=tables,
        evidence_pack_base_path=pack_dir,
        n_packs_written=n_packs_written,
        out_path=state_path,
    )

    # Render daily brief from state + this run’s packs
    try:
        write_newsletter(state_path, pack_dir, brief_md, brief_html)
    except Exception as e:
        print(f"[warn] daily brief render failed: {e}")


if __name__ == "__main__":
    main()