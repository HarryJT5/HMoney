# src/render/state_builder.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def _utc_iso(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _median(x: pd.Series) -> Optional[float]:
    x = _to_num(x).dropna()
    if x.empty:
        return None
    return float(x.median())


def _quantiles_iqr(x: pd.Series) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Returns (q25, q75, iqr) for numeric series, or (None, None, None) if empty.
    """
    x = _to_num(x).dropna()
    if x.empty:
        return None, None, None
    q25 = float(x.quantile(0.25))
    q75 = float(x.quantile(0.75))
    return q25, q75, (q75 - q25)


def _pct(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
        return None
    return float(round(float(x), 1))


def _normalize_site_path(p: str) -> str:
    """
    Convert filesystem-ish paths to a site-relative path for the dashboard.

    Examples:
      - "public/evidence_packs/2026-02-23/1638" -> "evidence_packs/2026-02-23/1638"
      - "docs/evidence_packs/..."               -> "evidence_packs/..."
      - "/evidence_packs/..."                  -> "evidence_packs/..."
    """
    if not p:
        return ""
    s = str(p).replace("\\", "/").strip()

    while s.startswith("./"):
        s = s[2:]
    while s.startswith("/"):
        s = s[1:]

    for prefix in ("public/", "docs/"):
        if s.startswith(prefix):
            s = s[len(prefix) :]

    return s.rstrip("/")


def _derive_posture(
    opp_med: Optional[float],
    risk_med: Optional[float],
    opp_thr: int,
    risk_thr: int,
) -> str:
    """
    Quadrant mapping based on cross-sectional medians vs thresholds.
    Label is just a compact tag; explanation carries meaning.
    """
    if opp_med is None or risk_med is None:
        return "Unknown"

    opp_hi = opp_med >= opp_thr
    risk_hi = risk_med >= risk_thr

    if (not opp_hi) and (not risk_hi):
        return "Quiet"
    if opp_hi and (not risk_hi):
        return "Constructive"
    if (not opp_hi) and risk_hi:
        return "Defensive"
    return "Volatile / Mixed"


def _confidence_tag(
    n: int,
    success_rate_pct: Optional[float],
    net_tilt: Optional[float],
    opp_iqr: Optional[float],
    risk_iqr: Optional[float],
) -> str:
    """
    A coarse diagnostic confidence tag about data coverage and consistency.
    Not a prediction or recommendation.
    """
    if n < 100:
        return "low_sample"
    if success_rate_pct is not None and success_rate_pct < 80:
        return "data_limited"
    if net_tilt is not None and abs(net_tilt) < 3.0:
        return "mixed_evidence"
    if (opp_iqr is not None and opp_iqr > 35) or (risk_iqr is not None and risk_iqr > 35):
        return "mixed_evidence"
    return "stable_read"


def _posture_plain_english(label: str) -> str:
    """
    Non-jargony translation of the posture label.
    This is the primary thing to show users if the label is unclear.
    """
    mapping = {
        "Quiet": (
            "Across the tracked universe, neither pullback/stabilization patterns nor fragility/breakdown patterns "
            "are especially widespread right now."
        ),
        "Constructive": (
            "Across the tracked universe, pullback-and-stabilization (setup-like) patterns are relatively common, "
            "while fragility/breakdown patterns are less common."
        ),
        "Defensive": (
            "Across the tracked universe, fragility/breakdown patterns are relatively common, "
            "while pullback-and-stabilization (setup-like) patterns are less common."
        ),
        "Volatile / Mixed": (
            "Across the tracked universe, both setup-like patterns and fragility/breakdown patterns are common at the same time "
            "(cross-currents across names)."
        ),
        "Unknown": (
            "Not enough data to summarize cross-sectional posture (one or more required breadth statistics are missing)."
        ),
    }
    return mapping.get(label, mapping["Unknown"])


def _make_posture_explanation(
    *,
    label: str,
    opp_thr: int,
    risk_thr: int,
    denom_presence: int,
    pullback_presence_count: Optional[int],
    fragility_presence_count: Optional[int],
    pullback_density_pct: Optional[float],
    fragility_presence_pct: Optional[float],
    net_tilt_pct_points: Optional[float],
    opp_med: Optional[float],
    risk_med: Optional[float],
    opp_q25: Optional[float],
    opp_q75: Optional[float],
    opp_iqr: Optional[float],
    risk_q25: Optional[float],
    risk_q75: Optional[float],
    risk_iqr: Optional[float],
    quadrant_denom: int,
    quadrant_counts: Dict[str, Optional[int]],
    quadrant_pct: Dict[str, Optional[float]],
) -> str:
    """
    Auto-explanation that avoids nebulous lingo.
    It states scope, defines the axes, then provides the exact numeric evidence.
    """
    parts: List[str] = []

    # 1) Scope
    parts.append(
        "Scope: This is a macro (cross-sectional / inter-firm) snapshot summarizing how common certain price-behavior "
        "patterns are across many tickers. It is diagnostic and does not imply direction, timing, or an action."
    )

    # 2) Definitions
    parts.append(
        f"Definitions: 'Setup-like' means pullback-and-stabilization characteristics (Opportunity score) relative to each asset’s own history; "
        f"'Fragility/breakdown-like' means weakness-and-instability characteristics (Structural Risk score) relative to each asset’s own history. "
        f"Presence flags use thresholds: Opportunity ≥ {opp_thr}, Risk ≥ {risk_thr}."
    )

    # 3) Plain-English translation of label
    parts.append(f"Plain-English posture meaning: {_posture_plain_english(label)}")

    # 4) Evidence: breadth presence + counts
    if (
        denom_presence > 0
        and pullback_presence_count is not None
        and fragility_presence_count is not None
        and pullback_density_pct is not None
        and fragility_presence_pct is not None
    ):
        parts.append(
            f"Evidence (breadth): setup-like presence = {pullback_presence_count}/{denom_presence} (~{pullback_density_pct}%); "
            f"fragility-like presence = {fragility_presence_count}/{denom_presence} (~{fragility_presence_pct}%)."
        )
        if net_tilt_pct_points is not None:
            parts.append(f"Evidence (balance): setup-like minus fragility-like = {net_tilt_pct_points} percentage points.")

    # 5) Evidence: medians vs thresholds
    if opp_med is not None and risk_med is not None:
        parts.append(
            f"Evidence (medians): Opportunity median = {round(opp_med,1)} vs threshold {opp_thr}; "
            f"Structural Risk median = {round(risk_med,1)} vs threshold {risk_thr}."
        )

    # 6) Evidence: dispersion context
    disp_bits: List[str] = []
    if opp_q25 is not None and opp_q75 is not None and opp_iqr is not None:
        disp_bits.append(f"Opportunity middle-50% range (Q25–Q75) = {round(opp_q25,1)}–{round(opp_q75,1)} (IQR {round(opp_iqr,1)})")
    if risk_q25 is not None and risk_q75 is not None and risk_iqr is not None:
        disp_bits.append(f"Risk middle-50% range (Q25–Q75) = {round(risk_q25,1)}–{round(risk_q75,1)} (IQR {round(risk_iqr,1)})")
    if disp_bits:
        parts.append("Evidence (dispersion across tickers): " + " | ".join(disp_bits) + ".")

    # 7) Evidence: quadrant grounding
    if quadrant_denom > 0 and all(v is not None for v in quadrant_counts.values()):
        parts.append(
            "Evidence (quadrants among tickers with both scores): "
            f"hi-opp/lo-risk={quadrant_counts['hi_opp_lo_risk']}/{quadrant_denom} (~{quadrant_pct['hi_opp_lo_risk']}%), "
            f"lo-opp/hi-risk={quadrant_counts['lo_opp_hi_risk']}/{quadrant_denom} (~{quadrant_pct['lo_opp_hi_risk']}%), "
            f"hi-opp/hi-risk={quadrant_counts['hi_opp_hi_risk']}/{quadrant_denom} (~{quadrant_pct['hi_opp_hi_risk']}%), "
            f"lo-opp/lo-risk={quadrant_counts['lo_opp_lo_risk']}/{quadrant_denom} (~{quadrant_pct['lo_opp_lo_risk']}%)."
        )

    return " ".join(parts)


def build_state_json(
    *,
    as_of_utc: datetime,
    legacy_market_bias: str,
    legacy_opportunity_score: int,
    legacy_structural_risk_score: int,
    legacy_counts_by_label: Dict[str, int],
    selection_meta: Dict[str, Any],
    df_scores: pd.DataFrame,
    tables: Dict[str, List[Dict[str, Any]]],
    evidence_pack_base_path: str,
    n_packs_written: int,
    out_path: str = "public/state.json",
) -> Dict[str, Any]:
    """
    Snapshot writer for public/state.json
    """

    # Calibrated defaults (can be overridden in selection_meta)
    opp_thr = int(selection_meta.get("opp_green_threshold", 60))
    risk_thr = int(selection_meta.get("risk_red_threshold", 70))

    d = df_scores.copy() if df_scores is not None else pd.DataFrame()
    if not d.empty:
        if "opportunity_score" in d.columns:
            d["opportunity_score"] = _to_num(d["opportunity_score"])
        if "structural_risk_score" in d.columns:
            d["structural_risk_score"] = _to_num(d["structural_risk_score"])

    n = int(selection_meta.get("actual_size", 0)) or (len(d) if not d.empty else 0)

    # --- Breadth presence (percent + exact counts) ---
    pullback_density = None
    fragility_presence = None
    net_tilt = None
    denom_presence = 0
    pullback_presence_count = None
    fragility_presence_count = None

    if (
        not d.empty
        and "opportunity_score" in d.columns
        and "structural_risk_score" in d.columns
    ):
        denom_presence = int(d[["opportunity_score", "structural_risk_score"]].dropna(how="all").shape[0])
        if denom_presence > 0:
            pullback_presence_count = int((d["opportunity_score"] >= opp_thr).sum())
            fragility_presence_count = int((d["structural_risk_score"] >= risk_thr).sum())

            pullback_density = 100.0 * float(pullback_presence_count) / denom_presence
            fragility_presence = 100.0 * float(fragility_presence_count) / denom_presence
            net_tilt = pullback_density - fragility_presence

    pullback_density = _pct(pullback_density)
    fragility_presence = _pct(fragility_presence)
    net_tilt = _pct(net_tilt)

    # --- Dispersion stats (median + quartiles + IQR) ---
    opp_med = _median(d["opportunity_score"]) if ("opportunity_score" in d.columns and not d.empty) else None
    risk_med = _median(d["structural_risk_score"]) if ("structural_risk_score" in d.columns and not d.empty) else None

    opp_q25, opp_q75, opp_iqr = (
        _quantiles_iqr(d["opportunity_score"])
        if ("opportunity_score" in d.columns and not d.empty)
        else (None, None, None)
    )
    risk_q25, risk_q75, risk_iqr = (
        _quantiles_iqr(d["structural_risk_score"])
        if ("structural_risk_score" in d.columns and not d.empty)
        else (None, None, None)
    )

    # --- Quadrant counts (high/low vs thresholds) ---
    quadrant_counts: Dict[str, Optional[int]] = {
        "hi_opp_lo_risk": None,
        "lo_opp_hi_risk": None,
        "hi_opp_hi_risk": None,
        "lo_opp_lo_risk": None,
    }
    quadrant_pct: Dict[str, Optional[float]] = {
        "hi_opp_lo_risk": None,
        "lo_opp_hi_risk": None,
        "hi_opp_hi_risk": None,
        "lo_opp_lo_risk": None,
    }
    quadrant_denom = 0

    if (
        not d.empty
        and "opportunity_score" in d.columns
        and "structural_risk_score" in d.columns
    ):
        dq = d[["opportunity_score", "structural_risk_score"]].dropna()
        quadrant_denom = int(len(dq))
        if quadrant_denom > 0:
            opp_hi = dq["opportunity_score"] >= opp_thr
            risk_hi = dq["structural_risk_score"] >= risk_thr

            c_hi_opp_lo_risk = int((opp_hi & (~risk_hi)).sum())
            c_lo_opp_hi_risk = int(((~opp_hi) & risk_hi).sum())
            c_hi_opp_hi_risk = int((opp_hi & risk_hi).sum())
            c_lo_opp_lo_risk = int(((~opp_hi) & (~risk_hi)).sum())

            quadrant_counts = {
                "hi_opp_lo_risk": c_hi_opp_lo_risk,
                "lo_opp_hi_risk": c_lo_opp_hi_risk,
                "hi_opp_hi_risk": c_hi_opp_hi_risk,
                "lo_opp_lo_risk": c_lo_opp_lo_risk,
            }
            quadrant_pct = {
                k: _pct(100.0 * float(v) / quadrant_denom) if v is not None else None
                for k, v in quadrant_counts.items()
            }

    posture_label = selection_meta.get("posture_label") or _derive_posture(opp_med, risk_med, opp_thr, risk_thr)
    confidence_tag = selection_meta.get("posture_confidence") or _confidence_tag(
        n=n,
        success_rate_pct=selection_meta.get("success_rate_pct"),
        net_tilt=net_tilt,
        opp_iqr=opp_iqr,
        risk_iqr=risk_iqr,
    )

    plain_english = _posture_plain_english(posture_label)
    posture_explanation = _make_posture_explanation(
        label=posture_label,
        opp_thr=opp_thr,
        risk_thr=risk_thr,
        denom_presence=int(denom_presence),
        pullback_presence_count=pullback_presence_count,
        fragility_presence_count=fragility_presence_count,
        pullback_density_pct=pullback_density,
        fragility_presence_pct=fragility_presence,
        net_tilt_pct_points=net_tilt,
        opp_med=opp_med,
        risk_med=risk_med,
        opp_q25=opp_q25,
        opp_q75=opp_q75,
        opp_iqr=opp_iqr,
        risk_q25=risk_q25,
        risk_q75=risk_q75,
        risk_iqr=risk_iqr,
        quadrant_denom=int(quadrant_denom),
        quadrant_counts=quadrant_counts,
        quadrant_pct=quadrant_pct,
    )

    # Glossary stays available for UI/help panels
    glossary = {
        "scope_note": (
            "Macro/cross-sectional snapshot (inter-firm): summarizes how common patterns are across many tickers; "
            "not a forecast and not a per-ticker recommendation."
        ),
        "opportunity_score": (
            "Per-asset score for pullback-and-stabilization characteristics relative to that asset’s own history "
            "(trader shorthand: 'setup-like')."
        ),
        "structural_risk_score": (
            "Per-asset score for weakness-and-instability characteristics relative to that asset’s own history "
            "(trader shorthand: 'fragility/breakdown-like')."
        ),
        "thresholds": "Presence flags use thresholds: Opportunity ≥ opp_thr; Risk ≥ risk_thr.",
        "median": "Median = typical value across the tracked sample.",
        "q25_q75": "Q25–Q75 = middle 50% range across tickers.",
        "iqr": "IQR = Q75 − Q25 (dispersion across tickers).",
        "presence_counts": "Exact numerator/denominator behind breadth percentages.",
        "quadrant_counts": "Counts split tickers into four groups (hi/lo opportunity vs hi/lo risk) among rows with both scores.",
    }

    out: Dict[str, Any] = {
        "schema_version": "hmoney.state.v1",
        "generated_at_utc": _utc_iso(),

        # legacy
        "as_of_utc": as_of_utc.isoformat(),
        "market_bias": legacy_market_bias,
        "opportunity_score": int(legacy_opportunity_score),
        "structural_risk_score": int(legacy_structural_risk_score),
        "counts_by_label": legacy_counts_by_label,
        "universe_size": int(selection_meta.get("actual_size", n)),

        "run": {
            "mode": selection_meta.get("mode", "intraday"),
            "cadence_minutes": int(selection_meta.get("cadence_minutes", 15)),
            "time_bucket": selection_meta.get("time_bucket"),
            "shards": {
                "count": int(selection_meta.get("shard_count", 1)),
                "index": selection_meta.get("shard_index"),
                "rotation": selection_meta.get("rotation", "per_run"),
            },
            "pipeline": {
                "git_sha": selection_meta.get("git_sha"),
                "workflow": selection_meta.get("workflow"),
                "job_id": selection_meta.get("job_id"),
            },
        },

        "universe": {
            "target_size": int(selection_meta.get("target_size", 1100)),
            "actual_size": int(selection_meta.get("actual_size", n)),
            "composition": {
                "panel": {"count": int(selection_meta.get("panel_count", 0))},
                "sentinels": {"count": int(selection_meta.get("sentinels_count", 0))},
                "forced_movers": {"count": int(selection_meta.get("forced_movers_count", 0))},
                "rolling_shard": {"count": int(selection_meta.get("rolling_shard_count", 0))},
            },
            "coverage_note": selection_meta.get("coverage_note", ""),
            "lists": {
                "panel_tickers": selection_meta.get("panel_tickers", []),
                "sentinel_tickers": selection_meta.get("sentinel_tickers", []),
                "forced_movers_tickers": selection_meta.get("forced_movers_tickers", []),
                "rolling_shard_tickers": selection_meta.get("rolling_shard_tickers", []),
            },
        },

        "market": {
            # Optional headline field for UI (pipeline may populate)
            "primary_benchmark": selection_meta.get("market_primary_benchmark"),
            "pct_change_1d": selection_meta.get("market_pct_change_1d"),

            "benchmarks": selection_meta.get("benchmarks", []),
            "volatility_proxies": selection_meta.get("volatility_proxies", []),
        },

        "breadth": {
            "thresholds": {
                "opportunity_score_green": opp_thr,
                "risk_score_red": risk_thr,
            },
            "sample": {
                "count": int(selection_meta.get("actual_size", n)),
                "denom_used_for_presence": int(denom_presence),
                "pullback_presence_count": pullback_presence_count,
                "fragility_presence_count": fragility_presence_count,
                "pullback_density_pct": pullback_density,
                "fragility_presence_pct": fragility_presence,
                "net_tilt_pct_points": net_tilt,
                "scope": "macro_cross_sectional",
            },
            "panel": {
                "count": int(selection_meta.get("panel_count", 0)),
                "pullback_density_pct": None,
                "fragility_presence_pct": None,
                "net_tilt_pct_points": None,
            },
            "bars": [
                {
                    "id": "pullback_density",
                    "label": "Pullback & stabilization presence (\"setup-like\" shorthand)",
                    "value_pct": pullback_density,
                    "interpretation": (
                        "Macro/cross-sectional share of sampled symbols whose Opportunity score meets/exceeds the threshold. "
                        "'Setup-like' means pullback-and-stabilization characteristics relative to each symbol’s own history "
                        "(not a prediction)."
                    ),
                },
                {
                    "id": "fragility_presence",
                    "label": "Breakdown & fragility presence",
                    "value_pct": fragility_presence,
                    "interpretation": (
                        "Macro/cross-sectional share of sampled symbols whose Structural Risk score meets/exceeds the threshold. "
                        "'Fragility' means weakness-and-instability characteristics relative to each symbol’s own history "
                        "(not a prediction)."
                    ),
                },
                {
                    "id": "net_tilt",
                    "label": "Net tilt (setup-like minus fragility-like)",
                    "value_pct_points": net_tilt,
                    "interpretation": (
                        "Descriptive balance metric: setup-like presence minus fragility-like presence (percentage points). "
                        "Summarizes cross-sectional conditions; does not imply direction or timing."
                    ),
                },
            ],
            "glossary": glossary,
        },

        "posture": {
            "label": posture_label,
            "plain_english": plain_english,
            "confidence_tag": confidence_tag,
            "key": (
                "quiet" if posture_label == "Quiet" else
                "constructive" if posture_label == "Constructive" else
                "defensive" if posture_label == "Defensive" else
                "volatile_mixed" if posture_label in ("Volatile / Mixed", "Volatile-Mixed") else
                "unknown"
            ),
            "basis": {
                "computed_at_utc": _utc_iso(),
                "scope": "macro_cross_sectional",
                "method": "median_quadrants_v1",

                "thresholds_used": {"opp_green": opp_thr, "risk_red": risk_thr},

                "opportunity_median": round(opp_med, 1) if opp_med is not None else None,
                "risk_median": round(risk_med, 1) if risk_med is not None else None,

                "opportunity_q25": round(opp_q25, 1) if opp_q25 is not None else None,
                "opportunity_q75": round(opp_q75, 1) if opp_q75 is not None else None,
                "opportunity_iqr": round(opp_iqr, 1) if opp_iqr is not None else None,

                "risk_q25": round(risk_q25, 1) if risk_q25 is not None else None,
                "risk_q75": round(risk_q75, 1) if risk_q75 is not None else None,
                "risk_iqr": round(risk_iqr, 1) if risk_iqr is not None else None,

                "presence_counts": {
                    "denom_used_for_presence": int(denom_presence),
                    "pullback_presence_count": pullback_presence_count,
                    "fragility_presence_count": fragility_presence_count,
                },

                "quadrant_denom": int(quadrant_denom),
                "quadrant_counts": quadrant_counts,
                "quadrant_pct": quadrant_pct,

                "pullback_density_pct": pullback_density,
                "fragility_presence_pct": fragility_presence,
                "net_tilt_pct_points": net_tilt,

                "interpretation_notes": glossary,
            },
            "explanation": posture_explanation,
        },

        "tables": {
            "pulled_back": {
                "title": "Pulled back / stabilizing (sample)",
                "method": "Highest Opportunity Score relative to own history.",
                "rows": tables.get("pulled_back", []),
            },
            "fragile": {
                "title": "Breakdown / fragile (sample)",
                "method": "Highest Structural Risk Score relative to own history.",
                "rows": tables.get("fragile", []),
            },
            "mixed": {
                "title": "High opportunity + high risk (sample)",
                "method": "Simultaneously elevated Opportunity and Structural Risk.",
                "rows": tables.get("mixed", []),
            },
        },

        "evidence_pack_index": {
            "base_path": _normalize_site_path(evidence_pack_base_path),
            "format": "json",
            "n_packs_written": int(n_packs_written),
            "note": "Individual per-asset evidence packs stored separately.",
        },

        "evidence": {
            "sec": {"asof_utc": None, "events_count": 0, "top_forms": [], "events": []},
            "media": {"asof_utc": None, "mentions_count": 0, "top_sources": [], "mentions": []},
        },

        "quality": {
            "data_provider": selection_meta.get("data_provider", "yfinance"),
            "success_rate_pct": selection_meta.get("success_rate_pct"),
            "missing_bars_count": selection_meta.get("missing_bars_count"),
            "skipped_tickers_count": selection_meta.get("skipped_tickers_count"),
            "errors_sample": selection_meta.get("errors_sample", []),
            "notes": selection_meta.get("quality_notes", []),
        },

        "disclaimer": {
            "diagnostic_only": True,
            "not_investment_advice": True,
            "language_policy": [
                "No forecasts",
                "No buy/sell instructions",
                "Scores describe price behavior relative to each asset’s own history",
            ],
            "interpretation_note": (
                "Opportunity and Structural Risk are descriptive cross-sectional indicators, "
                "not expected-return estimates."
            ),
        },
    }

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out