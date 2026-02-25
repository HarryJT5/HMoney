# HMoney Cloudflare Worker

This folder contains the **version-controlled source** for the Cloudflare Worker that powers:
- **/live** (near-live quote overlay using BYO API keys)
- **/pack** (evidence pack fetch with stale fallback)
- **/news** (RSS headline aggregation)
- **/ai** (Workers AI assistant)

> **Important:** API keys are **NOT** stored in this repo. The dashboard stores keys in the browser (localStorage) and sends them to the Worker as request headers.

---

## Worker URL

Deployed Worker (example):
- `https://bearingbullishballs.htgamein.workers.dev`

---

## Endpoints

### 1) `GET /live?symbols=SPY,QQQ,MSFT&provider=auto`
Near-live quote overlay (cached, best-effort).

**Required headers (BYO keys):**
- `X-Finnhub-Key: <your_finnhub_key>` (for equities/ETFs)
- `X-TwelveData-Key: <your_twelvedata_key>` (for caret symbols / crypto mapping, fallback)

**Query params:**
- `symbols` or `tickers` (comma-separated, max ~25)
- `provider`:
  - `auto` (default) = Finnhub + Twelve Data routing
  - `finnhub` = Finnhub only
  - `twelvedata` = Twelve Data only

**Response shape (example):**
```json
{
  "ok": true,
  "as_of_utc": "2026-02-24T19:34:10Z",
  "cache_ttl_s": 20,
  "provider_mode": "auto",
  "quotes": {
    "SPY": { "symbol": "SPY", "price": 688.9, "pct": 0.76, "chg": 5.18 },
    "BTC-USD": { "symbol": "BTC-USD", "price": 52210, "pct": -0.3, "chg": -160 }
  },
  "debug": {
    "used": [{ "provider": "finnhub", "symbols": 6 }, { "provider": "twelvedata", "symbols": 1 }],
    "alias_map": {}
  }
}