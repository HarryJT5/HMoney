# HMoney Methodology (Decision Support, Not a Forecast)

HMoney is an **investment intelligence / decision-support** system. It is designed to:
- Summarize **action-relevant conditions** (what looks supportive vs concerning right now)
- Stay **descriptive** rather than predictive
- Provide **diagnostics**, not automatic trade instructions
- Evaluate signals **relative to each asset’s own history** (not a universal “fair value” model)

In other words: HMoney is built to help you **notice** patterns that may warrant *attention, deeper research, or a change in posture*—without claiming certainty about what will happen next.

---

## What the system is measuring

HMoney organizes price behavior into two broad (not mutually exclusive) pattern families:

- **Setup-like / pullback + stabilization characteristics**  
  Pullbacks that appear to be **cooling off** and **stabilizing** relative to the asset’s own recent behavior.

- **Fragility / breakdown characteristics**  
  Persistent weakness and/or instability relative to the asset’s own recent behavior.

These are **not forecasts**. They are compact descriptors of *how price has been behaving*.

---

## Opportunity Score

<a id="opportunity-score"></a>

**Opportunity Score (0–100)** is a proxy for **setup density**: how strongly an asset shows *pullback + stabilization* characteristics **relative to its own history**.

Typical ingredients (high-level, simplified):
- Pullback distance from recent highs (context only)
- Signs of stabilization vs continued slide
- Positioning vs moving averages (trend context)
- Volatility normalization (context)

Interpretation:
- Higher values indicate **more setup-like characteristics** in the recent window.
- A high Opportunity Score does **not** guarantee upside; it indicates conditions that may be **more supportive** than the asset’s typical baseline and may justify **closer review**.

---

## Structural Risk Score

<a id="structural-risk-score"></a>

**Structural Risk Score (0–100)** is a proxy for **fragility**: how strongly an asset shows *breakdown / instability* characteristics **relative to its own history**.

Typical ingredients (high-level, simplified):
- Persistent downtrend characteristics (e.g., distance below longer trend measures)
- Elevated volatility / instability vs its own norms
- Drawdown persistence (context)

Interpretation:
- Higher values indicate **more fragility-like characteristics** in the recent window.
- A high Risk Score does **not** guarantee further downside; it indicates conditions that may be **less supportive / more vulnerable** than the asset’s typical baseline and may justify **risk review**.

---

## Discount Score

<a id="discount-score"></a>

**Discount Score (0–100)** is an optional proxy for “distance from prior reference levels” (e.g., off highs) **relative to the asset’s own history**.

Interpretation:
- Higher values often correspond to **more pulled-back** conditions compared to the asset’s typical range.
- “Discounted” is not the same thing as “cheap.” It is a **price-history** descriptor, not a fundamental valuation claim.

If Discount Score is missing, it typically means the input data needed to compute it wasn’t available for that ticker in that run.

---

## Classifications

<a id="classifications"></a>

HMoney uses five diagnostic labels. They are meant to be **action-relevant summaries** (i.e., “this may deserve attention”), without being automatic instructions:

- **🟢 High-Confidence Discount**  
  Setup-like characteristics are strong and fragility-like characteristics are low **relative to the system’s thresholds**.  
  Often used to prioritize names for **further research**.

- **🟡 Opportunistic**  
  Setup-like characteristics are present, but confidence is lower or context is more mixed.  
  Often used to keep names on a **watchlist**.

- **🔵 Neutral**  
  Neither setup-like nor fragility-like characteristics dominate.  
  Often used as a baseline “no special emphasis” state.

- **🟠 Deterioration**  
  Fragility-like characteristics are rising, but not at the most severe end.  
  Often used to encourage **closer monitoring**.

- **🔴 Structural Risk**  
  Fragility-like characteristics are strong and persistent relative to thresholds.  
  Often used to flag names for **risk review** (position sizing, thesis check, or deeper investigation).

Important notes:
- Labels can change as new data comes in.
- Labels describe observed conditions; they do not “know” your thesis, time horizon, or constraints.
- Treat labels as **signals for attention**, not as automatic decisions.

---

## Deployment Bias Indicator

<a id="deployment-bias"></a>

The **Deployment Bias** is a compact, human-readable tag used for sorting and workflow:

- `deploy`, `watch`, `hold`, `reduce`, `avoid`

It is intentionally phrased like “what you might consider,” but it is **not** an instruction. Think of it as a **workflow bias** that can prompt review, not a mandate.

---

## RSI

<a id="rsi"></a>

**RSI (Relative Strength Index)** is included as **context only**. It is a commonly used momentum oscillator.

HMoney does not treat RSI as a standalone signal. It is displayed to help interpret the asset’s recent momentum regime.

---

## About short-horizon metrics

Metrics like **% Chg 1D** can update during the session and can be noisy. They are included as context, not as a primary driver of posture or classification.

---

## Data Sources (current)

Right now, most market fields come from **Yahoo Finance via yfinance**. Over time, HMoney may integrate additional sources (e.g., filings, fundamentals providers, alternative datasets). When it does, HMoney will attach links to relevant source pages wherever possible.

---

## Glossary

- **Relative-to-self**: compares an asset to its own historical behavior, not to other assets’ absolute levels.
- **Cross-sectional breadth**: how common a pattern is across many names (e.g., “X of Y tickers show setup-like characteristics”).
- **Decision support**: provides signals and context that can inform human judgment; does not automate decisions.