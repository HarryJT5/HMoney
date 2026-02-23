"""
src/render/newsletter.py

Renders a daily brief (Markdown + HTML) from public/state.json and evidence packs.

V3 (updated):
- Adds % Chg 1D (intraday-updating) to Benchmarks + Tables
- Keeps HTML color coding (directional sign/magnitude) for % columns
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


# ----------------------------
# Config
# ----------------------------

LABEL_NAME = {
    "🟢": "High-Confidence Discount",
    "🟡": "Opportunistic",
    "🔵": "Neutral",
    "🟠": "Deterioration",
    "🔴": "Structural Risk",
}

BENCHMARK_GROUPS = [
    ("US Equity", ["SPY", "QQQ", "DIA", "IWM", "VTI"]),
    ("Global Equity", ["VT", "VXUS", "VEA", "VWO"]),
    ("Rates / USD / Vol", ["BND", "TLT", "UUP", "^VIX"]),
    ("Real Assets", ["GLD", "SLV", "USO"]),
    ("Crypto (context)", ["BTC-USD"]),
]

BENCHMARK_META = {
    "SPY": "S&P 500 (US large-cap)",
    "QQQ": "Nasdaq 100 (US growth/tech tilt)",
    "DIA": "Dow 30 (US large-cap)",
    "IWM": "US small-cap (Russell 2000)",
    "VTI": "US total market",
    "VT": "Total world (US + Intl)",
    "VXUS": "International ex-US",
    "VEA": "Developed markets ex-US",
    "VWO": "Emerging markets",
    "BND": "US total bond market",
    "TLT": "Long-duration US Treasuries",
    "UUP": "US dollar strength proxy",
    "^VIX": "Volatility index (VIX)",
    "GLD": "Gold",
    "SLV": "Silver",
    "USO": "Oil (WTI proxy)",
    "BTC-USD": "Bitcoin (risk appetite proxy)",
}

DEFAULT_LOCAL_TZ = "America/Los_Angeles"
NEG_ORANGE_CUTOFF_PCT = -10.0  # mild negative threshold for red vs orange


# ----------------------------
# Utilities
# ----------------------------

def _get(d: Dict[str, Any], path: str, default=None):
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _badge(label: str) -> str:
    return f"{label} {LABEL_NAME.get(label, '')}".strip()


def _fmt_pct_already(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.1f}%"
    except Exception:
        return "—"


def _fmt_pct_ratio(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x) * 100:.1f}%"
    except Exception:
        return "—"


def _fmt_pct_change_1d(x: Any) -> str:
    """
    pct_change_1d can be provided as:
      - percent units (e.g. 1.2 meaning 1.2%)
      - or ratio units (e.g. 0.012 meaning 1.2%)
    We infer:
      abs(x) <= 0.25 -> treat as ratio; else treat as percent.
    """
    if x is None:
        return "—"
    try:
        v = float(x)
        if abs(v) <= 0.25:
            v = v * 100.0
        return f"{v:.1f}%"
    except Exception:
        return "—"


def _fmt_num(x: Any, digits: int = 1) -> str:
    if x is None:
        return "—"
    try:
        f = float(x)
        return f"{f:.{digits}f}"
    except Exception:
        return "—"


def _fmt_int(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return str(int(x))
    except Exception:
        return "—"


def _fmt_ratio(n: Any, d: Any) -> str:
    if n is None or d in (None, 0):
        return "—"
    try:
        return f"{int(n)}/{int(d)}"
    except Exception:
        return "—"


def _bar_from_pct(pct: Any, filled: str = "🟩", empty: str = "⚪", n: int = 5) -> str:
    if pct is None:
        return empty * n
    try:
        x = float(pct)
    except Exception:
        return empty * n
    x = max(0.0, min(100.0, x))
    k = int(x // (100 / n))
    if k >= n:
        k = n
    return filled * k + empty * (n - k)


def _tilt_bar_from_points(points: Any) -> str:
    if points is None:
        return "⚪⚪⚪⚪⚪"
    try:
        x = float(points)
    except Exception:
        return "⚪⚪⚪⚪⚪"
    x = max(-50.0, min(50.0, x))
    pct = (x + 50.0)
    return _bar_from_pct(pct, filled="🟨", empty="⚪", n=5)


def _posture_plain_english(posture: Dict[str, Any]) -> str:
    pe = posture.get("plain_english")
    if isinstance(pe, str) and pe.strip():
        return pe.strip()

    label = (posture.get("label") or "Unknown").strip()
    mapping = {
        "Quiet": (
            "Across the tracked universe, neither pullback/stabilization (setup-like) patterns nor fragility/breakdown "
            "(weakness + instability) patterns are especially widespread right now."
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
            "Across the tracked universe, setup-like patterns and fragility/breakdown patterns are both common at the same time "
            "(cross-currents across names)."
        ),
        "Unknown": "Not enough data to summarize cross-sectional posture for this run.",
    }
    return mapping.get(label, mapping["Unknown"])


def _index_by_symbol(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        if sym:
            out[sym] = r
    return out


def _short_posture_explanation(state: Dict[str, Any]) -> List[str]:
    posture = state.get("posture", {}) or {}
    breadth = state.get("breadth", {}) or {}
    sample = breadth.get("sample", {}) or {}
    basis = posture.get("basis", {}) or {}
    thresholds = (breadth.get("thresholds", {}) or {})

    label = posture.get("label", "Unknown")
    denom = sample.get("denom_used_for_presence")
    pull_ct = sample.get("pullback_presence_count")
    frag_ct = sample.get("fragility_presence_count")
    pull_pct = sample.get("pullback_density_pct")
    frag_pct = sample.get("fragility_presence_pct")
    tilt = sample.get("net_tilt_pct_points")

    opp_med = basis.get("opportunity_median")
    risk_med = basis.get("risk_median")
    opp_thr = _get(basis, "thresholds_used.opp_green", thresholds.get("opportunity_score_green"))
    risk_thr = _get(basis, "thresholds_used.risk_red", thresholds.get("risk_score_red"))

    qden = basis.get("quadrant_denom")
    qpct = basis.get("quadrant_pct", {}) or {}

    lines: List[str] = []
    lines.append("Macro/inter-firm summary of how common setup-like vs fragility-like patterns are (not a forecast).")

    if denom is not None:
        lines.append(
            f"Presence: setup-like {_fmt_ratio(pull_ct, denom)} (~{_fmt_pct_already(pull_pct)}); "
            f"fragility-like {_fmt_ratio(frag_ct, denom)} (~{_fmt_pct_already(frag_pct)}); "
            f"net balance {_fmt_num(tilt, 1)} pct-pts."
        )

    if opp_med is not None or risk_med is not None:
        lines.append(
            f"Medians vs thresholds: Opportunity {_fmt_num(opp_med, 1)} (≥{_fmt_num(opp_thr, 0)}); "
            f"Risk {_fmt_num(risk_med, 1)} (≥{_fmt_num(risk_thr, 0)})."
        )

    if qden not in (None, 0):
        lines.append(
            f"Quadrants (both scores present, n={_fmt_int(qden)}): "
            f"hi-opp/lo-risk {_fmt_pct_already(qpct.get('hi_opp_lo_risk'))}, "
            f"lo-opp/hi-risk {_fmt_pct_already(qpct.get('lo_opp_hi_risk'))}, "
            f"hi-opp/hi-risk {_fmt_pct_already(qpct.get('hi_opp_hi_risk'))}, "
            f"lo-opp/lo-risk {_fmt_pct_already(qpct.get('lo_opp_lo_risk'))}."
        )

    lines.append(f"Posture label '{label}' is a compact tag for the pattern mix above.")
    return lines


def _format_dual_time(utc_iso: str, local_tz: str) -> str:
    if not utc_iso:
        return ""
    try:
        s = utc_iso.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(ZoneInfo(local_tz))
        return (
            f"UTC {dt_utc.strftime('%Y-%m-%d %H:%M:%S')}Z"
            f" | Local {dt_local.strftime('%Y-%m-%d %H:%M:%S')} {local_tz}"
        )
    except Exception:
        return utc_iso


# ----------------------------
# Loaders
# ----------------------------

def load_state(state_path: str) -> Dict[str, Any]:
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_packs(evidence_dir: str) -> List[Dict[str, Any]]:
    packs: List[Dict[str, Any]] = []
    for p in sorted(Path(evidence_dir).glob("*.json")):
        try:
            packs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return packs


def _pack_row(p: Dict[str, Any]) -> Dict[str, Any]:
    asset = p.get("asset", {}) or {}
    market = p.get("market", {}) or {}
    scores = p.get("scores", {}) or {}
    cls = p.get("classification", {}) or {}
    pc = p.get("portfolio_context", {}) or {}
    expl = p.get("explainability", {}) or {}

    last = market.get("last_price")
    ma200 = market.get("ma_200")
    dist200 = None
    if last is not None and ma200 not in (None, 0):
        try:
            dist200 = (float(last) / float(ma200)) - 1.0
        except Exception:
            dist200 = None

    return {
        "symbol": asset.get("symbol", ""),
        "label": cls.get("label", "🔵"),
        "confidence": cls.get("confidence", 0.0),
        "opp": scores.get("opportunity_score", 0),
        "risk": scores.get("structural_risk_score", 0),
        "disc": scores.get("discount_score", None),
        "pct_change_1d": market.get("pct_change_1d", None),  # ✅ NEW
        "pct_off_high": market.get("pct_off_52w_high", None),  # ratio
        "rsi": market.get("rsi_14", None),
        "dist200": dist200,  # ratio
        "weight_pct": pc.get("weight_pct", None),
        "tag": pc.get("tag", ""),
        "reasons": expl.get("reason_codes", []),
        "bias": p.get("deployment_bias", "hold"),
    }


def select_sections(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    def disc_key(r):
        d = r.get("disc")
        return (-1 if d is None else int(d))

    greens = [r for r in rows if r["label"] == "🟢"]
    yellows = [r for r in rows if r["label"] == "🟡"]
    risks = [r for r in rows if r["label"] in ("🟠", "🔴")]
    mixed = [r for r in rows if (int(r.get("opp", 0)) >= 60 and int(r.get("risk", 0)) >= 70)]

    greens_sorted = sorted(greens, key=lambda r: (disc_key(r), r["opp"], r["confidence"]), reverse=True)[:12]
    yellows_sorted = sorted(yellows, key=lambda r: (r["opp"], r["confidence"]), reverse=True)[:12]
    risks_sorted = sorted(risks, key=lambda r: (r["risk"], -r["opp"]), reverse=True)[:12]
    mixed_sorted = sorted(mixed, key=lambda r: (r["opp"] + r["risk"], r["confidence"]), reverse=True)[:12]

    return {"greens": greens_sorted, "yellows": yellows_sorted, "risks": risks_sorted, "mixed": mixed_sorted}


# ----------------------------
# Markdown rendering
# ----------------------------

def render_md(state: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    as_of = state.get("generated_at_utc") or state.get("as_of_utc") or ""
    local_tz = os.getenv("HMONEY_LOCAL_TZ", DEFAULT_LOCAL_TZ)
    as_of_dual = _format_dual_time(as_of, local_tz=local_tz)

    posture = state.get("posture", {}) or {}
    breadth = state.get("breadth", {}) or {}
    sample = breadth.get("sample", {}) or {}
    basis = posture.get("basis", {}) or {}
    thresholds = (breadth.get("thresholds", {}) or {})

    universe_actual = _get(state, "universe.actual_size", None)
    if universe_actual is None:
        universe_actual = int(state.get("universe_size", len(rows)))

    target_size = _get(state, "universe.target_size", None)

    success_rate = _get(state, "quality.success_rate_pct", None)
    missing_bars = _get(state, "quality.missing_bars_count", None)
    skipped = _get(state, "quality.skipped_tickers_count", None)

    denom = sample.get("denom_used_for_presence")
    pull_ct = sample.get("pullback_presence_count")
    frag_ct = sample.get("fragility_presence_count")

    pull_pct = sample.get("pullback_density_pct")
    frag_pct = sample.get("fragility_presence_pct")
    tilt_pts = sample.get("net_tilt_pct_points")

    posture_label = posture.get("label", "Unknown")
    posture_pe = _posture_plain_english(posture)

    pull_bar = _bar_from_pct(pull_pct, filled="🟩")
    frag_bar = _bar_from_pct(frag_pct, filled="🟥")
    tilt_bar = _tilt_bar_from_points(tilt_pts)

    opp_med = basis.get("opportunity_median")
    risk_med = basis.get("risk_median")
    opp_thr = _get(basis, "thresholds_used.opp_green", thresholds.get("opportunity_score_green"))
    risk_thr = _get(basis, "thresholds_used.risk_red", thresholds.get("risk_score_red"))

    qden = basis.get("quadrant_denom")
    qpct = basis.get("quadrant_pct", {}) or {}

    opp_q25 = basis.get("opportunity_q25")
    opp_q75 = basis.get("opportunity_q75")
    opp_iqr = basis.get("opportunity_iqr")
    risk_q25 = basis.get("risk_q25")
    risk_q75 = basis.get("risk_q75")
    risk_iqr = basis.get("risk_iqr")

    sections = select_sections(rows)
    idx = _index_by_symbol(rows)

    out: List[str] = []
    out.append(f"# Daily Brief — {as_of_dual or as_of}")
    out.append("")

    out.append("## Run Snapshot (diagnostic)")
    if target_size is not None:
        out.append(f"- Universe: **{universe_actual}** tracked (target {int(target_size)})")
    else:
        out.append(f"- Universe: **{universe_actual}** tracked")
    cov_bits = []
    if success_rate is not None:
        cov_bits.append(f"**{_fmt_num(success_rate, 1)}%** success")
    if missing_bars is not None:
        cov_bits.append(f"missing bars: {_fmt_int(missing_bars)}")
    if skipped is not None:
        cov_bits.append(f"skipped: {_fmt_int(skipped)}")
    if cov_bits:
        out.append("- Coverage: " + " | ".join(cov_bits))
    if denom is not None:
        out.append(f"- Presence denominator (for %): **{_fmt_int(denom)}** (rows with ≥1 score available)")
    out.append("")

    out.append("## Benchmarks (context)")
    out.append("")
    out.append("These are widely-used market indicators shown for context alongside the cross-sectional posture.")
    out.append("_Coloring in the HTML version is directional (sign/magnitude), not advice._")
    out.append("_% Chg 1D updates during the session and can be noisy._")
    out.append("")

    def _bench_table(symbols: List[str]) -> None:
        out.append("| Ticker | What it represents | Opp | Risk | % Chg 1D | % off High (drawdown) | Δ200DMA (trend) |")
        out.append("|---|---|---:|---:|---:|---:|---:|")
        any_row = False
        for sym in symbols:
            r = idx.get(sym.upper())
            if not r:
                continue
            any_row = True
            out.append(
                f"| **{sym}** | {BENCHMARK_META.get(sym, 'Benchmark')} | "
                f"{int(r.get('opp', 0))} | {int(r.get('risk', 0))} | "
                f"{_fmt_pct_change_1d(r.get('pct_change_1d'))} | "
                f"{_fmt_pct_ratio(r.get('pct_off_high'))} | {_fmt_pct_ratio(r.get('dist200'))} |"
            )
        if not any_row:
            out.append("| — | (no benchmark packs for this group in this run) | — | — | — | — | — |")
        out.append("")

    for group_name, syms in BENCHMARK_GROUPS:
        out.append(f"### {group_name}")
        _bench_table(syms)

    out.append("## Market Posture (macro / cross-sectional, not advice)")
    out.append(f"**Posture:** {posture_label}")
    out.append(f"**Plain English:** {posture_pe}")
    out.append("")
    out.append("### Evidence (why this posture)")
    if denom is not None:
        out.append(f"- Setup-like presence (pullback + stabilization): **{_fmt_ratio(pull_ct, denom)}** (≈{_fmt_pct_already(pull_pct)})")
        out.append(f"- Fragility-like presence (weakness + instability): **{_fmt_ratio(frag_ct, denom)}** (≈{_fmt_pct_already(frag_pct)})")
    if tilt_pts is not None:
        out.append(f"- Net balance: **{_fmt_num(tilt_pts, 1)}** pct-pts (setup-like minus fragility-like)")
    if opp_med is not None or risk_med is not None:
        out.append(
            f"- Medians vs thresholds: Opportunity **{_fmt_num(opp_med,1)}** (≥{_fmt_num(opp_thr,0)}) | "
            f"Risk **{_fmt_num(risk_med,1)}** (≥{_fmt_num(risk_thr,0)})"
        )
    if qden not in (None, 0):
        out.append(f"- Quadrants (both scores available: **{_fmt_int(qden)}**):")
        out.append(f"  - hi-opp/lo-risk: {_fmt_pct_already(qpct.get('hi_opp_lo_risk'))}")
        out.append(f"  - lo-opp/hi-risk: {_fmt_pct_already(qpct.get('lo_opp_hi_risk'))}")
        out.append(f"  - hi-opp/hi-risk: {_fmt_pct_already(qpct.get('hi_opp_hi_risk'))}")
        out.append(f"  - lo-opp/lo-risk: {_fmt_pct_already(qpct.get('lo_opp_lo_risk'))}")
    if opp_q25 is not None or risk_q25 is not None:
        out.append("- Dispersion (middle 50% of tickers):")
        out.append(f"  - Opportunity Q25–Q75: {_fmt_num(opp_q25,1)}–{_fmt_num(opp_q75,1)} (IQR {_fmt_num(opp_iqr,1)})")
        out.append(f"  - Risk Q25–Q75: {_fmt_num(risk_q25,1)}–{_fmt_num(risk_q75,1)} (IQR {_fmt_num(risk_iqr,1)})")

    out.append("")
    out.append("**Auto explanation (short):**")
    for s in _short_posture_explanation(state)[:4]:
        out.append(f"- {s}")
    out.append("")
    out.append("_Full explanation is available in public/state.json (posture.explanation)._")
    out.append("")

    out.append("### Signal bars (descriptive)")
    out.append(f"- Pullback/Stabilization presence: {pull_bar}  **{_fmt_pct_already(pull_pct)}**")
    out.append(f"- Breakdown/Fragility presence: {frag_bar}  **{_fmt_pct_already(frag_pct)}**")
    out.append(f"- Net tilt: {tilt_bar}  **{_fmt_num(tilt_pts, 1)}** pct-pts")
    out.append("")
    out.append("> **How to read:** This is macro (inter-firm) breadth. “Setup-like” and “fragility-like” describe price behavior relative to each asset’s own history. Not forecasts.")
    out.append("")

    out.append("## Tables (sample highlights)")
    out.append("")
    out.append("**Column definitions (quick):**")
    out.append("- **Opp** = pullback + stabilization score (setup-like) relative to the asset’s own history")
    out.append("- **Risk** = weakness + instability score (fragility-like) relative to the asset’s own history")
    out.append("- **% Chg 1D** = short-horizon context (updates during session; can be noisy)")
    out.append("- **% off High** = distance from a recent high (drawdown-style)")
    out.append("- **Δ200DMA** = distance from 200-day moving average")
    out.append("- **RSI** = momentum oscillator (context only)")
    out.append("_HTML coloring is directional (sign/magnitude), not advice._")
    out.append("")

    def table(rows_: List[Dict[str, Any]]) -> None:
        out.append("| Ticker | State | Opp | Risk | % Chg 1D | Discount | % off High | RSI | Δ200DMA |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r0 in rows_:
            out.append(
                f"| **{r0['symbol']}** | {_badge(r0['label'])} | {int(r0['opp'])} | {int(r0['risk'])} | "
                f"{_fmt_pct_change_1d(r0.get('pct_change_1d'))} | "
                f"{('—' if r0['disc'] is None else int(r0['disc']))} | "
                f"{_fmt_pct_ratio(r0['pct_off_high'])} | "
                f"{_fmt_num(r0['rsi'], 1)} | "
                f"{_fmt_pct_ratio(r0['dist200'])} |"
            )
        out.append("")

    out.append("### 🟢 Pulled Back / Stabilizing (Top)")
    if sections["greens"]:
        table(sections["greens"])
    else:
        out.append("_None currently._\n")

    out.append("### 🔴 Breakdown / Fragile (Top)")
    if sections["risks"]:
        table(sections["risks"])
    else:
        out.append("_None currently._\n")

    out.append("### 🟡 High Opportunity + High Risk (Cross-currents)")
    if sections["mixed"] or sections["yellows"]:
        if sections["mixed"]:
            table(sections["mixed"])
        elif sections["yellows"]:
            table(sections["yellows"])
    else:
        out.append("_None currently._\n")

    out.append("")
    out.append("_Disclaimer: Diagnostic view of cross-sectional price behavior. Not investment advice._")
    out.append("")
    return "\n".join(out)


# ----------------------------
# HTML rendering (minimal markdown + color coding)
# ----------------------------

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"_(.+?)_")


def _md_inline_to_html(text: str) -> str:
    safe = html.escape(text)
    safe = _BOLD_RE.sub(r"<b>\1</b>", safe)
    safe = _ITALIC_RE.sub(r"<em>\1</em>", safe)
    return safe


def _parse_numeric(cell_text: str) -> Optional[float]:
    s = cell_text.strip()
    if not s or s == "—":
        return None
    s = s.replace(",", "")
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s)
    except Exception:
        return None


def _cell_classes(header: str, cell_text: str) -> str:
    h = header.lower().strip()
    n = _parse_numeric(cell_text)

    classes: List[str] = []
    if n is not None:
        classes.append("num")

    # don't color interpretive score columns
    if h in ("opp", "risk", "discount", "rsi"):
        return " ".join(classes)

    # color percent / trend / drawdown / chg columns
    if "%" in header or "200dma" in h or "trend" in h or "drawdown" in h or "chg" in h:
        if n is None:
            return " ".join(classes)

        if n >= 0:
            classes.append("pos")
        else:
            if n >= NEG_ORANGE_CUTOFF_PCT:
                classes.append("mildneg")
            else:
                classes.append("neg")
        return " ".join(classes)

    return " ".join(classes)


def render_html(md_text: str) -> str:
    lines = md_text.splitlines()
    html_lines: List[str] = []
    html_lines.append("<!doctype html>")
    html_lines.append("<html><head><meta charset='utf-8'/>")
    html_lines.append("<meta name='viewport' content='width=device-width, initial-scale=1'/>")
    html_lines.append("<title>HMoney Daily Brief</title>")
    html_lines.append(
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;line-height:1.35}"
        "h1{font-size:22px;margin:0 0 8px} h2{font-size:18px;margin:18px 0 8px} h3{font-size:15px;margin:14px 0 6px}"
        "table{border-collapse:collapse;width:100%;margin:10px 0 18px}"
        "th,td{border:1px solid #ddd;padding:8px;font-size:13px;vertical-align:top}"
        "th{background:#f6f6f6;text-align:left}"
        "td.num, th.num{text-align:right;font-variant-numeric:tabular-nums}"
        "td.pos{background:#e8f5e9}"
        "td.mildneg{background:#fff3e0}"
        "td.neg{background:#ffebee}"
        "code{background:#f2f2f2;padding:2px 4px;border-radius:4px}"
        "blockquote{border-left:3px solid #ddd;margin:10px 0;padding:6px 10px;color:#333;background:#fafafa}"
        ".muted{color:#666}"
        "</style></head><body>"
    )

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        if line.startswith("# "):
            html_lines.append(f"<h1>{_md_inline_to_html(line[2:])}</h1>")
            i += 1
            continue
        if line.startswith("## "):
            html_lines.append(f"<h2>{_md_inline_to_html(line[3:])}</h2>")
            i += 1
            continue
        if line.startswith("### "):
            html_lines.append(f"<h3>{_md_inline_to_html(line[4:])}</h3>")
            i += 1
            continue
        if line.startswith("> "):
            html_lines.append(f"<blockquote>{_md_inline_to_html(line[2:])}</blockquote>")
            i += 1
            continue

        # tables
        if line.startswith("|") and "|" in line[1:]:
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1

            if len(table_lines) >= 2:
                headers = [h.strip() for h in table_lines[0].strip("|").split("|")]
                rows = []
                for tline in table_lines[2:]:
                    rows.append([c.strip() for c in tline.strip("|").split("|")])

                html_lines.append("<table>")
                ths = [f"<th>{_md_inline_to_html(h)}</th>" for h in headers]
                html_lines.append("<thead><tr>" + "".join(ths) + "</tr></thead>")
                html_lines.append("<tbody>")
                for r in rows:
                    tds = []
                    for j, c in enumerate(r):
                        header = headers[j] if j < len(headers) else ""
                        cls = _cell_classes(header, c)
                        tds.append(f"<td class='{cls}'>{_md_inline_to_html(c)}</td>")
                    html_lines.append("<tr>" + "".join(tds) + "</tr>")
                html_lines.append("</tbody></table>")
            continue

        if not line:
            html_lines.append("<div style='height:6px'></div>")
            i += 1
            continue

        # list items
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:])
                i += 1
            html_lines.append("<ul>")
            for it in items:
                html_lines.append(f"<li>{_md_inline_to_html(it)}</li>")
            html_lines.append("</ul>")
            continue

        html_lines.append(f"<p>{_md_inline_to_html(line)}</p>")
        i += 1

    html_lines.append("</body></html>")
    return "\n".join(html_lines)


def write_newsletter(state_path: str, evidence_dir: str, md_out: str, html_out: str) -> None:
    state = load_state(state_path)
    packs = load_packs(evidence_dir)
    rows = [_pack_row(p) for p in packs]

    md = render_md(state, rows)
    Path(md_out).write_text(md, encoding="utf-8")

    html_doc = render_html(md)
    Path(html_out).write_text(html_doc, encoding="utf-8")