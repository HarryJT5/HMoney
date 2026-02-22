"""
src/render/newsletter.py

Renders a daily brief (Markdown + HTML) from public/state.json and evidence packs.
V1: no external templates, no email. Pure file output.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


LABEL_NAME = {
    "🟢": "High-Confidence Discount",
    "🟡": "Opportunistic",
    "🔵": "Neutral",
    "🟠": "Deterioration",
    "🔴": "Structural Risk",
}


def _score_label(score: int) -> str:
    # Simple gamified labels (tune anytime)
    if score >= 80:
        return "Elite"
    if score >= 65:
        return "Strong"
    if score >= 50:
        return "Mixed"
    return "Weak"

def _opp_band(score: int) -> str:
    if score >= 75:
        return "Strong"
    if score >= 55:
        return "Favorable"
    if score >= 40:
        return "Mixed"
    if score >= 25:
        return "Weak"
    return "Very weak"


def _risk_band(score: int) -> str:
    # Higher score = higher structural risk
    if score >= 80:
        return "High"
    if score >= 60:
        return "Elevated"
    if score >= 40:
        return "Moderate"
    return "Low"


def _bias_explain(mb: str, opp: int, risk: int, counts: Dict[str, Any], universe_size: int) -> str:
    g = int(counts.get("🟢", 0))
    y = int(counts.get("🟡", 0))
    o = int(counts.get("🟠", 0))
    r = int(counts.get("🔴", 0))

    opp_breadth = g + y
    risk_breadth = o + r

    # Minimal, honest V1 explanation:
    # bias is a diagnostic summary of breadth + composites
    return (
        f"Bias driver: opportunity breadth {opp_breadth}/{universe_size}, "
        f"risk flags {risk_breadth}/{universe_size}, "
        f"opportunity {opp}/100, risk {risk}/100."
    )


def _fmt_pct(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x) * 100:.1f}%"
    except Exception:
        return "—"


def _fmt_num(x: Any, digits: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "—"


def _badge(label: str) -> str:
    return f"{label} {LABEL_NAME.get(label, '')}".strip()


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
    asset = p.get("asset", {})
    market = p.get("market", {})
    scores = p.get("scores", {})
    cls = p.get("classification", {})
    pc = p.get("portfolio_context", {}) or {}

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
        "pct_off_high": market.get("pct_off_52w_high", None),
        "rsi": market.get("rsi_14", None),
        "dist200": dist200,
        "weight_pct": pc.get("weight_pct", None),
        "tag": pc.get("tag", ""),
        "reasons": (p.get("explainability", {}) or {}).get("reason_codes", []),
        "bias": p.get("deployment_bias", "hold"),
    }


def select_sections(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    # Core sorting: prioritize discount score if present, else opportunity
    def disc_key(r):
        d = r.get("disc")
        return (-1 if d is None else int(d))

    greens = [r for r in rows if r["label"] == "🟢"]
    yellows = [r for r in rows if r["label"] == "🟡"]
    risks = [r for r in rows if r["label"] in ("🟠", "🔴")]

    greens_sorted = sorted(greens, key=lambda r: (disc_key(r), r["opp"], r["confidence"]), reverse=True)[:10]
    yellows_sorted = sorted(yellows, key=lambda r: (r["opp"], r["confidence"]), reverse=True)[:10]
    risks_sorted = sorted(risks, key=lambda r: (r["risk"], -r["opp"]), reverse=True)[:10]

    return {
        "greens": greens_sorted,
        "yellows": yellows_sorted,
        "risks": risks_sorted,
    }


def portfolio_snapshot(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    port = [r for r in rows if r.get("weight_pct") is not None]
    if not port:
        return {"has_portfolio": False}

    total = 0.0
    weights = []
    for r in port:
        try:
            w = float(r["weight_pct"])
        except Exception:
            w = 0.0
        total += w
        weights.append((r["symbol"], w, r.get("tag", "")))

    weights.sort(key=lambda x: x[1], reverse=True)
    top = weights[:5]
    concentration_flag = (top[0][1] >= 30.0) if top else False

    return {
        "has_portfolio": True,
        "total_weight_pct": total,
        "top_positions": top,
        "concentration_flag": concentration_flag,
    }

def _bar(score: int, filled: str, empty: str = "⚪") -> str:
    s = max(0, min(100, int(score)))
    n = int(round(s / 20))  # 0..5
    return filled * n + empty * (5 - n)

    opp_bar = _bar(opp, "🟢")
    risk_bar = _bar(risk, "🔴")
    tilt_bar = _bar(max(0, min(100, (opp - risk) + 50)), "🟡")


def _tilt_label(opp: int, risk: int) -> str:
    """
    Diagnostic posture tilt based on difference between setup density (opp)
    and fragility (risk). Not a recommendation.
    """
    delta = opp - risk
    if delta >= 25:
        return "Lean Deploy"
    if delta >= 10:
        return "Slight Lean Deploy"
    if delta <= -25:
        return "Lean Reduce"
    if delta <= -10:
        return "Slight Lean Reduce"
    return "Neutral"


def _posture_color(posture: str) -> str:
    """
    Color block for posture headline.
    """
    p = posture.lower()
    if "constructive" in p:
        return "🟩"
    if "defensive" in p:
        return "🟥"
    if "volatile" in p or "mixed" in p:
        return "🟧"
    if "quiet" in p:
        return "🟦"
    return "🟡"


def render_md(state: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    as_of = state.get("as_of_utc", "")
    opp = int(state.get("opportunity_score", 0))
    risk = int(state.get("structural_risk_score", 0))

    counts = state.get("counts_by_label", {}) or {}
    universe_size = int(state.get("universe_size", len(rows)))

    sections = select_sections(rows)
    port = portfolio_snapshot(rows)

    # Breadth rollups
    calm = int(counts.get("🔵", 0))
    pulled_back = int(counts.get("🟢", 0))
    watch = int(counts.get("🟡", 0))
    deteriorating = int(counts.get("🟠", 0))
    broken = int(counts.get("🔴", 0))

    opp_breadth = pulled_back + watch
    risk_breadth = deteriorating + broken

    # --- Posture (descriptive quadrant) ---
    if opp < 40 and risk < 40:
        posture = "Quiet"
        overview = (
            "Across the tracked universe, most assets appear to be trading in relatively stable ranges. "
            "Very few names look meaningfully compressed relative to recent highs, and persistent breakdown conditions are not widespread. "
            "The model reads this as a calm but low-dislocation environment."
        )
    elif opp < 40 and risk >= 60:
        posture = "Defensive"
        overview = (
            "Across the tracked universe, pullback-with-stability patterns look sparse, while persistent downtrends and elevated volatility appear more common. "
            "Breakdown characteristics appear more prevalent than compression."
        )
    elif opp >= 60 and risk < 40:
        posture = "Constructive"
        overview = (
            "Across the tracked universe, pullback-with-stability patterns appear more common than persistent breakdown behavior. "
            "Compression-type characteristics look more prevalent than fragility."
        )
    else:
        posture = "Volatile / Mixed"
        overview = (
            "Across the tracked universe, both pullback-with-stability patterns and breakdown characteristics appear at the same time. "
            "Price behavior looks mixed rather than clearly calm or clearly fragile."
        )

    posture_block = _posture_color(posture)
    tilt = _tilt_label(opp, risk)

    # Bars
    opp_bar = _bar(opp, "🟩")         # setup density bar
    risk_bar = _bar(risk, "🟥")       # fragility bar (more red = more stress)
    tilt_bar = _bar(max(0, min(100, (opp - risk) + 50)), "🟨")  # centered at 50 = neutral

    out: List[str] = []
    out.append(f"# Daily Brief — {as_of}")
    out.append("")

    # ---- Top visual strip ----
    out.append(f"## {posture_block} Market Posture: {posture} (diagnostic)")
    out.append("")
    out.append("### Signal bars (descriptive, not advice)")
    out.append(f"- Pullback/Stabilization density: {opp_bar}  **{opp}/100**")
    out.append(f"- Breakdown/Fragility presence: {risk_bar}  **{risk}/100**")
    out.append(f"- Net tilt (setup minus fragility): {tilt_bar}  **{tilt}**")
    out.append("")
    out.append(f"- Universe: **{universe_size}** tracked assets")
    out.append("")

    # ---- Narrative overview ----
    out.append("## Market Overview (descriptive)")
    out.append("")
    out.append(overview)
    out.append("")
    out.append("In short: price behavior appears relatively stable, but broad discount-type conditions look limited.")
    out.append("")

    # ---- Pullback / Stabilization ----
    out.append("## Pullback & Stabilization Characteristics")
    out.append("")
    out.append(f"- Pullback/Stabilization bar: {opp_bar}")
    out.append(
        f"- Opportunity score: **{opp}/100** "
        "(describes how many assets appear pulled back relative to recent highs without extreme breakdown traits)."
    )
    out.append("- On this scale, readings below ~20 typically align with sparse compression across the universe.")
    out.append(
        f"- {opp_breadth} of {universe_size} tracked assets currently show pullback-with-stability characteristics."
    )

    if opp_breadth == 0:
        out.append("- No meaningful compression-type behavior is being detected at this time.")
    elif opp_breadth < universe_size * 0.1:
        out.append("- Pullback-with-stability behavior appears isolated rather than broad.")
    elif opp_breadth < universe_size * 0.4:
        out.append("- Pullback-with-stability behavior appears in pockets across the universe.")
    else:
        out.append("- Pullback-with-stability behavior appears relatively widespread.")
    out.append("")

    # ---- Breakdown / Fragility ----
    out.append("## Breakdown & Fragility Characteristics")
    out.append("")
    out.append(f"- Breakdown/Fragility bar: {risk_bar}")
    out.append(
        f"- Structural risk score: **{risk}/100** "
        "(describes how many assets exhibit persistent downtrends, elevated volatility, or extended drawdowns)."
    )
    out.append("- On this scale, readings below ~20 typically align with broad technical stability rather than systemic stress.")
    out.append(
        f"- {risk_breadth} of {universe_size} tracked assets currently show downtrend + elevated volatility characteristics."
    )

    if risk_breadth == 0:
        out.append("- Persistent breakdown characteristics are not currently widespread.")
    elif risk_breadth < universe_size * 0.1:
        out.append("- Breakdown behavior appears isolated rather than systemic.")
    elif risk_breadth < universe_size * 0.4:
        out.append("- Breakdown behavior appears in several areas of the universe.")
    else:
        out.append("- Breakdown behavior appears broad and persistent.")
    out.append("")

    out.append(
        "_These signals describe observable price characteristics across the tracked universe. "
        "They are not forecasts, predictions, or investment recommendations._"
    )
    out.append("")

    # ---- Portfolio Snapshot (if applicable) ----
    if port.get("has_portfolio"):
        out.append("## Portfolio Snapshot")
        if port.get("concentration_flag"):
            out.append("- ⚠️ Top position represents ≥ 30% of portfolio weight.")
        out.append("- Top weights:")
        for sym, w, tag in port["top_positions"]:
            tag_s = f" ({tag})" if tag else ""
            out.append(f"  - **{sym}**: {w:.1f}%{tag_s}")
        out.append("")

    # ---- Tables ----
    def table(rows_: List[Dict[str, Any]]) -> None:
        out.append("| Ticker | State | Opp | Risk | Discount | % off High | RSI | Δ200DMA |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for r0 in rows_:
            out.append(
                f"| **{r0['symbol']}** | {_badge(r0['label'])} | {int(r0['opp'])} | {int(r0['risk'])} | "
                f"{('—' if r0['disc'] is None else int(r0['disc']))} | "
                f"{_fmt_pct(r0['pct_off_high'])} | "
                f"{_fmt_num(r0['rsi'], 1)} | "
                f"{_fmt_pct(r0['dist200'])} |"
            )
        out.append("")

    out.append("## 🟢 Pulled Back Without Breakdown (Top)")
    if sections["greens"]:
        table(sections["greens"])
    else:
        out.append("_None currently._\n")

    out.append("## 🟡 Watchlist (Partial Pullback / Mixed Signals)")
    if sections["yellows"]:
        table(sections["yellows"])
    else:
        out.append("_None currently._\n")

    out.append("## 🟠 / 🔴 Persistent Downtrend or Elevated Volatility")
    if sections["risks"]:
        table(sections["risks"])
    else:
        out.append("_None currently._\n")

    out.append("")
    out.append("_Diagnostic view of current cross-sectional price behavior. Not advice._")
    out.append("")
    return "\n".join(out)

def render_html(md_text: str) -> str:
    # Minimal markdown-to-html: headings + paragraphs + tables. (V1, no deps)
    lines = md_text.splitlines()
    html_lines: List[str] = []
    html_lines.append("<!doctype html>")
    html_lines.append("<html><head><meta charset='utf-8'/>")
    html_lines.append("<meta name='viewport' content='width=device-width, initial-scale=1'/>")
    html_lines.append("<title>HMoney Daily Brief</title>")
    html_lines.append(
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;}"
        "h1{font-size:22px} h2{font-size:18px;margin-top:18px}"
        "table{border-collapse:collapse;width:100%;margin:10px 0 18px}"
        "th,td{border:1px solid #ddd;padding:8px;font-size:13px}"
        "th{background:#f6f6f6;text-align:left}"
        "code{background:#f2f2f2;padding:2px 4px;border-radius:4px}"
        ".muted{color:#666}"
        "</style></head><body>"
    )

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        if line.startswith("# "):
            html_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
            i += 1
            continue
        if line.startswith("## "):
            html_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
            i += 1
            continue
        if line.startswith("|") and "|" in line[1:]:
            # parse markdown table
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            # table_lines[1] is separator
            headers = [h.strip() for h in table_lines[0].strip("|").split("|")]
            rows = []
            for tline in table_lines[2:]:
                rows.append([c.strip() for c in tline.strip("|").split("|")])
            html_lines.append("<table>")
            html_lines.append("<thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr></thead>")
            html_lines.append("<tbody>")
            for r in rows:
                html_lines.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
            html_lines.append("</tbody></table>")
            continue

        if not line:
            html_lines.append("<div style='height:6px'></div>")
            i += 1
            continue

        # bold markdown **x**
        safe = html.escape(line)
        safe = safe.replace("**", "<b>", 1).replace("**", "</b>", 1) if safe.count("**") >= 2 else safe
        html_lines.append(f"<p>{safe}</p>")
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