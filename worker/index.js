// worker/index.js
export default {
  async fetch(request, env) {
    // -------------------------
    // CORS (GitHub Pages -> Worker)
    // -------------------------
    const origin = request.headers.get("Origin") || "";
    const allowedOrigins = new Set([
      "https://harryjt5.github.io",
      "http://localhost:8000",
      "http://127.0.0.1:8000",
    ]);
    const allowOrigin = allowedOrigins.has(origin) ? origin : "https://harryjt5.github.io";

    const cors = {
      "Access-Control-Allow-Origin": allowOrigin,
      "Access-Control-Allow-Methods": "POST, OPTIONS, GET",
      "Access-Control-Allow-Headers": "Content-Type, X-Finnhub-Key, X-TwelveData-Key",
      "Access-Control-Expose-Headers": "X-HMoney-Cache-Hit, X-HMoney-Cache-TTL",
      "Access-Control-Max-Age": "86400",
      "Vary": "Origin",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    // -------------------------
    // Config
    // -------------------------
    const GH_PAGES_BASE =
      env?.GH_PAGES_BASE
        ? String(env.GH_PAGES_BASE).replace(/\/+$/, "")
        : "https://harryjt5.github.io/HMoney";

    const TTL_STATE = 30;
    const TTL_EVID_INDEX = 300;
    const TTL_PACK = 300;
    const TTL_LIVE = 20;
    const TTL_NEWS = 120;
    const TTL_MARKET = 300; // Fear & Greed cache

    const url = new URL(request.url);
    const path = (url.pathname || "/").replace(/\/+$/, "") || "/";

    // -------------------------
    // Utilities
    // -------------------------
    const jsonHeaders = (extra = {}) => ({ ...cors, "Content-Type": "application/json", ...extra });

    function nowUtcIso() {
      return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
    }

    function okJson(obj, status = 200, extraHeaders = {}) {
      return new Response(JSON.stringify(obj, null, 2), { status, headers: jsonHeaders(extraHeaders) });
    }

    function errJson(message, status = 400, extra = {}) {
      return okJson({ ok: false, error: message, ...extra }, status);
    }

    function joinUrl(base, ...parts) {
      let out = base.replace(/\/+$/, "");
      for (const p of parts) {
        if (!p) continue;
        out += "/" + String(p).replace(/^\/+/, "").replace(/\/+$/, "");
      }
      return out;
    }

    function normalizeTicker(t) {
      return String(t || "").trim().toUpperCase().replace(/\s+/g, "");
    }

    async function sha256Short(s) {
      try {
        const data = new TextEncoder().encode(s);
        const digest = await crypto.subtle.digest("SHA-256", data);
        const arr = Array.from(new Uint8Array(digest));
        const hex = arr.map(b => b.toString(16).padStart(2, "0")).join("");
        return hex.slice(0, 12);
      } catch {
        return "nohash";
      }
    }

    function toIsoMaybe(x) {
      if (x === null || x === undefined) return null;

      // numeric epoch (seconds or ms)
      if (typeof x === "number" && Number.isFinite(x)) {
        const ms = (x > 2_000_000_000_000) ? x : (x > 2_000_000_000 ? x * 1000 : x);
        const d = new Date(ms);
        if (!isNaN(d.getTime())) return d.toISOString().replace(/\.\d{3}Z$/, "Z");
        return null;
      }

      const s = String(x).trim();
      if (!s) return null;

      // parseable date string
      const d = new Date(s);
      if (!isNaN(d.getTime())) return d.toISOString().replace(/\.\d{3}Z$/, "Z");

      return null;
    }

    // -------------------------
    // Cache helper (Cloudflare cache)
    // Returns { resp, cacheHit }
    // -------------------------
    async function cachedFetch(urlStr, ttlSeconds, opts = {}) {
      const bypass = opts.bypassCache === true;
      const cache = caches.default;
      const cacheKey = new Request(urlStr, { method: "GET" });

      if (!bypass) {
        const hit = await cache.match(cacheKey);
        if (hit) return { resp: hit, cacheHit: true };
      }

      let resp;
      try {
        resp = await fetch(urlStr, {
          method: "GET",
          headers: {
            "User-Agent": "HMoneyWorker/1.0",
            "Accept": "*/*",
          },
        });
      } catch (e) {
        return { resp: new Response(String(e || "fetch error"), { status: 502 }), cacheHit: false };
      }

      const headers = new Headers(resp.headers);
      headers.set("Cache-Control", `public, max-age=${ttlSeconds}, s-maxage=${ttlSeconds}`);
      headers.set("X-HMoney-Cache-TTL", String(ttlSeconds));
      headers.set("X-HMoney-Cache-Hit", "0");

      const wrapped = new Response(resp.body, { status: resp.status, headers });

      if (!bypass && resp.ok) {
        const toCache = wrapped.clone();
        toCache.headers.set("X-HMoney-Cache-Hit", "1");
        await cache.put(cacheKey, toCache);
      }

      return { resp: wrapped, cacheHit: false };
    }

    // -------------------------
    // Evidence pack helpers
    // -------------------------
    async function fetchState(bypassCache = false) {
      const stateUrl = joinUrl(GH_PAGES_BASE, "state.json");
      const { resp, cacheHit } = await cachedFetch(stateUrl, TTL_STATE, { bypassCache });
      if (!resp.ok) return { state: null, cacheHit };
      try { return { state: await resp.json(), cacheHit }; } catch { return { state: null, cacheHit }; }
    }

    async function fetchEvidenceIndex(bypassCache = false) {
      const idxUrl = joinUrl(GH_PAGES_BASE, "evidence_index.json");
      const { resp, cacheHit } = await cachedFetch(idxUrl, TTL_EVID_INDEX, { bypassCache });
      if (!resp.ok) return { idx: null, cacheHit };
      try { return { idx: await resp.json(), cacheHit }; } catch { return { idx: null, cacheHit }; }
    }

    function computeCurrentPackPathFromState(state, ticker) {
      if (!state || typeof state !== "object") return null;
      const epi = state.evidence_pack_index || {};
      const basePathRaw = (epi.base_path || "").replace(/^\/+/, "").replace(/\/+$/, "");
      const latestDirRaw = (epi.latest_pack_dir || "").replace(/^\/+/, "").replace(/\/+$/, "");

      if (basePathRaw && /^evidence_packs\/\d{4}-\d{2}-\d{2}\/\d{4}$/.test(basePathRaw)) {
        return `${basePathRaw}/${ticker}.json`;
      }

      if (latestDirRaw) {
        if (/^evidence_packs\//.test(latestDirRaw)) return `${latestDirRaw}/${ticker}.json`;
        const root = basePathRaw || "evidence_packs";
        return `${root}/${latestDirRaw}/${ticker}.json`;
      }

      if (basePathRaw && basePathRaw !== "evidence_packs") {
        return `${basePathRaw}/${ticker}.json`;
      }

      return null;
    }

    function slimPack(pack) {
      if (!pack || typeof pack !== "object") return pack;
      return {
        schema_version: pack.schema_version,
        run_id: pack.run_id,
        generated_at_utc: pack.generated_at_utc,
        as_of_utc: pack.as_of_utc,
        asset: pack.asset,
        market: pack.market,
        scores: pack.scores,
        classification: pack.classification,
        deployment_bias: pack.deployment_bias,
        explainability: pack.explainability,
        links: pack.links,
        field_links: pack.field_links,
      };
    }

    async function getPackForTicker(ticker, bypassCache = false) {
      const t = normalizeTicker(ticker);
      if (!t) return { found: false, ticker: t, reason: "empty_ticker" };

      const { state, cacheHit: stateCacheHit } = await fetchState(bypassCache);
      const currentRel = computeCurrentPackPathFromState(state, t);

      const debug = {
        ticker: t,
        gh_pages_base: GH_PAGES_BASE,
        current_rel: currentRel,
        cache: {
          state_cache_hit: stateCacheHit,
          evidence_index_cache_hit: null,
          current_pack_cache_hit: null,
          fallback_pack_cache_hit: null,
        }
      };

      if (currentRel) {
        const currentUrl = joinUrl(GH_PAGES_BASE, currentRel);
        const { resp, cacheHit } = await cachedFetch(currentUrl, TTL_PACK, { bypassCache });
        debug.cache.current_pack_cache_hit = cacheHit;

        if (resp.ok) {
          const pack = await resp.json().catch(() => null);
          return {
            found: true,
            is_stale: false,
            source: "current_run",
            pack_url: currentUrl,
            pack,
            pack_slim: slimPack(pack),
            debug,
          };
        }
        debug.current_status = resp.status;
      }

      const { idx, cacheHit: idxCacheHit } = await fetchEvidenceIndex(bypassCache);
      debug.cache.evidence_index_cache_hit = idxCacheHit;

      const latest = idx && idx.latest && typeof idx.latest === "object" ? idx.latest : null;
      const entry = latest ? latest[t] : null;

      if (entry && entry.path) {
        const fallbackUrl = joinUrl(GH_PAGES_BASE, entry.path);
        const { resp: resp2, cacheHit: cacheHit2 } = await cachedFetch(fallbackUrl, TTL_PACK, { bypassCache });
        debug.cache.fallback_pack_cache_hit = cacheHit2;

        if (resp2.ok) {
          const pack2 = await resp2.json().catch(() => null);
          return {
            found: true,
            is_stale: true,
            source: "evidence_index",
            stale_reason: "current_run_pack_missing",
            pack_url: fallbackUrl,
            pack_meta: entry,
            pack: pack2,
            pack_slim: slimPack(pack2),
            debug: { ...debug, fallback_path: entry.path },
          };
        }
        debug.fallback_status = resp2.status;
      }

      return { found: false, ticker: t, reason: "pack_not_found", debug };
    }

    async function handlePack() {
      const t = normalizeTicker(url.searchParams.get("ticker") || url.searchParams.get("symbol") || "");
      if (!t) return errJson("Missing ticker= (or symbol=)", 400);

      const force = url.searchParams.get("force") === "1";

      const cacheKeyUrl = `https://hmoney.local/pack?ticker=${encodeURIComponent(t)}`;
      if (!force) {
        const hit = await caches.default.match(new Request(cacheKeyUrl));
        if (hit) {
          const j = await hit.json().catch(() => null);
          if (j && typeof j === "object") {
            j.cache_hit = true;
            return okJson(j, 200, {
              "Cache-Control": `public, max-age=${TTL_PACK}, s-maxage=${TTL_PACK}`,
              "X-HMoney-Cache-Hit": "1",
              "X-HMoney-Cache-TTL": String(TTL_PACK),
            });
          }
          return hit;
        }
      }

      const res = await getPackForTicker(t, force);
      const payloadBase = {
        ok: true,
        ticker: t,
        cache_ttl_s: TTL_PACK,
        cache_hit: false,
      };

      if (!res.found) {
        const payload = {
          ...payloadBase,
          found: false,
          reason: res.reason || "pack_not_found",
          debug: res.debug || null
        };
        const out = okJson(payload, 200, {
          "Cache-Control": `public, max-age=${TTL_PACK}, s-maxage=${TTL_PACK}`,
          "X-HMoney-Cache-Hit": "0",
          "X-HMoney-Cache-TTL": String(TTL_PACK),
        });
        if (!force) await caches.default.put(new Request(cacheKeyUrl), out.clone());
        return out;
      }

      const payload = {
        ...payloadBase,
        found: true,
        is_stale: !!res.is_stale,
        source: res.source,
        stale_reason: res.stale_reason || null,
        pack_url: res.pack_url,
        pack_meta: res.pack_meta || null,
        pack_as_of_utc: res.pack?.as_of_utc || res.pack_meta?.as_of_utc || null,
        pack_run_id: res.pack?.run_id || res.pack_meta?.run_id || null,
        pack: res.pack,
        debug: res.debug || null,
      };

      const out = okJson(payload, 200, {
        "Cache-Control": `public, max-age=${TTL_PACK}, s-maxage=${TTL_PACK}`,
        "X-HMoney-Cache-Hit": "0",
        "X-HMoney-Cache-TTL": String(TTL_PACK),
      });

      if (!force) await caches.default.put(new Request(cacheKeyUrl), out.clone());
      return out;
    }

    // -------------------------
    // /live endpoint (BYO keys)
    // -------------------------
    function mapToTwelveDataSymbol(sym) {
      if (sym === "BTC-USD") return "BTC/USD";
      return sym;
    }

    async function fetchFinnhubQuote(sym, key) {
      const endpoint = `https://finnhub.io/api/v1/quote?symbol=${encodeURIComponent(sym)}&token=${encodeURIComponent(key)}`;
      const r = await fetch(endpoint, { method: "GET", headers: { "Accept": "application/json" } });
      if (!r.ok) throw new Error(`FINNHUB_${r.status}`);

      const j = await r.json();
      const c = j?.c ?? null;
      const pc = j?.pc ?? null;
      const t = j?.t ?? null;

      let chg = null, pct = null;
      if (c !== null && pc !== null && Number(pc) !== 0) {
        chg = Number(c) - Number(pc);
        pct = (chg / Number(pc)) * 100;
      }

      return {
        symbol: sym,
        provider: "finnhub",
        price: c,
        chg,
        pct,
        prevClose: pc,
        market_time_utc: t ? new Date(Number(t) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z") : null,
      };
    }

    function parseTwelveDataMulti(respJson) {
      if (!respJson || typeof respJson !== "object") return {};
      const out = {};
      for (const [k, v] of Object.entries(respJson)) {
        if (!v || typeof v !== "object") continue;
        if (v.status && v.status !== "ok") continue;
        const sym = (v.symbol ? String(v.symbol) : String(k)).toUpperCase();
        out[sym] = v;
      }
      if (!Object.keys(out).length && respJson.symbol) {
        const sym = String(respJson.symbol).toUpperCase();
        out[sym] = respJson;
      }
      return out;
    }

    async function fetchTwelveDataQuotes(symbols, key) {
      const mapped = symbols.map(mapToTwelveDataSymbol);
      const endpoint =
        `https://api.twelvedata.com/quote?symbol=${encodeURIComponent(mapped.join(","))}&apikey=${encodeURIComponent(key)}`;

      const r = await fetch(endpoint, { method: "GET", headers: { "Accept": "application/json" } });
      if (!r.ok) throw new Error(`TWELVEDATA_${r.status}`);

      const j = await r.json();
      const multi = parseTwelveDataMulti(j);
      const out = {};

      for (let i = 0; i < symbols.length; i++) {
        const original = symbols[i];
        const mappedSym = mapped[i].toUpperCase();
        const q = multi[mappedSym] || multi[original.toUpperCase()] || null;

        if (!q) {
          out[original] = { symbol: original, provider: "twelvedata", price: null, pct: null, chg: null, debug: "missing" };
          continue;
        }

        const close = q.close ?? q.price ?? null;
        const prev = q.previous_close ?? q.prev_close ?? q.close ?? null;

        let chg = null, pct = null;
        if (close !== null && prev !== null && Number(prev) !== 0) {
          chg = Number(close) - Number(prev);
          pct = (chg / Number(prev)) * 100;
        }

        out[original] = {
          symbol: original,
          provider: "twelvedata",
          price: close !== null ? Number(close) : null,
          chg,
          pct,
          prevClose: prev !== null ? Number(prev) : null,
          market_time_utc: q.datetime ? String(q.datetime) : null,
        };
      }

      return out;
    }

    async function handleLive() {
      const raw = url.searchParams.get("symbols") || url.searchParams.get("tickers") || "";
      const provider = (url.searchParams.get("provider") || "auto").toLowerCase();

      const requested = raw.split(",").map(normalizeTicker).filter(Boolean).slice(0, 25);
      if (!requested.length) return errJson("Missing symbols= (comma-separated)", 400);

      const aliasTo = (sym) => {
        if (sym === "^VIX" || sym === "VIX") return "VXX";
        return sym;
      };

      const aliasMap = {};
      const fetchSymbols = [];
      for (const s of requested) {
        const a = aliasTo(s);
        aliasMap[s] = a;
        if (!fetchSymbols.includes(a)) fetchSymbols.push(a);
      }

      const finnhubKey = request.headers.get("X-Finnhub-Key") || "";
      const tdKey = request.headers.get("X-TwelveData-Key") || "";

      const finnhubHash = finnhubKey ? await sha256Short(finnhubKey) : "nokey";
      const tdHash = tdKey ? await sha256Short(tdKey) : "nokey";

      const cacheKeyUrl =
        `https://hmoney.local/live?provider=${encodeURIComponent(provider)}&fh=${finnhubHash}&td=${tdHash}&symbols=${encodeURIComponent([...requested].sort().join(","))}`;

      const hit = await caches.default.match(new Request(cacheKeyUrl));
      if (hit) {
        const j = await hit.json().catch(() => null);
        if (j && typeof j === "object") {
          j.cache_hit = true;
          return okJson(j, 200, {
            "Cache-Control": `public, max-age=${TTL_LIVE}, s-maxage=${TTL_LIVE}`,
            "X-HMoney-Cache-Hit": "1",
            "X-HMoney-Cache-TTL": String(TTL_LIVE),
          });
        }
        return hit;
      }

      const payload = {
        ok: true,
        as_of_utc: nowUtcIso(),
        cache_ttl_s: TTL_LIVE,
        cache_hit: false,
        provider_mode: provider,
        quotes: {},
        debug: { used: [], alias_map: aliasMap },
      };

      const wantsTwelve = (s) => (s.startsWith("^") || s === "BTC-USD");
      const finnhubSyms = fetchSymbols.filter(s => !wantsTwelve(s));
      const twelveSyms  = fetchSymbols.filter(s => wantsTwelve(s));

      try {
        if (provider === "finnhub" || provider === "auto") {
          if (finnhubSyms.length) {
            if (!finnhubKey) throw new Error("missing_finnhub_key");

            const CONC = 6;
            const results = [];
            for (let i = 0; i < finnhubSyms.length; i += CONC) {
              const chunk = finnhubSyms.slice(i, i + CONC);
              const part = await Promise.allSettled(chunk.map(s => fetchFinnhubQuote(s, finnhubKey)));
              results.push(...part);
            }
            for (const r of results) {
              if (r.status === "fulfilled" && r.value?.symbol) payload.quotes[r.value.symbol] = r.value;
            }
            payload.debug.used.push({ provider: "finnhub", symbols: finnhubSyms.length });
          }
        }

        if (provider === "twelvedata" || provider === "auto") {
          if (twelveSyms.length) {
            if (!tdKey) throw new Error("missing_twelvedata_key");
            const qmap = await fetchTwelveDataQuotes(twelveSyms, tdKey);
            for (const [k, v] of Object.entries(qmap)) payload.quotes[k] = v;
            payload.debug.used.push({ provider: "twelvedata", symbols: twelveSyms.length });
          }
        }

        const outputQuotes = {};
        for (const orig of requested) {
          const fetched = aliasMap[orig] || orig;
          const q = payload.quotes[fetched] || null;
          if (!q) {
            outputQuotes[orig] = { symbol: orig, provider: null, price: null, pct: null, chg: null, debug: "unavailable" };
            continue;
          }
          const out = { ...q, symbol: orig };
          if (fetched !== orig) out.proxy_for = fetched;
          outputQuotes[orig] = out;
        }
        payload.quotes = outputQuotes;

      } catch (e) {
        payload.ok = false;
        payload.error = String(e?.message || e);
      }

      const out = okJson(payload, 200, {
        "Cache-Control": `public, max-age=${TTL_LIVE}, s-maxage=${TTL_LIVE}`,
        "X-HMoney-Cache-Hit": "0",
        "X-HMoney-Cache-TTL": String(TTL_LIVE),
      });

      await caches.default.put(new Request(cacheKeyUrl), out.clone());
      return out;
    }

    // -------------------------
    // Market (/market) — Fear & Greed
    // -------------------------
    function parseFearGreed(json) {
      // CNN feed tends to include: fear_and_greed { score, rating, timestamp }
      const fg = json?.fear_and_greed ?? json?.fearAndGreed ?? json?.fear_greed ?? null;

      // Try likely fields
      const score =
        fg?.score ?? fg?.now?.score ?? fg?.now?.value ?? json?.score ?? null;

      const rating =
        fg?.rating ?? fg?.now?.rating ?? json?.rating ?? null;

      const ts =
        fg?.timestamp_utc ?? fg?.timestamp ?? fg?.last_updated ?? json?.timestamp ?? json?.last_updated ?? null;

      const scoreNum = (score === null || score === undefined) ? null : Number(score);
      const scoreOut = Number.isFinite(scoreNum) ? scoreNum : null;

      const ratingOut = (rating === null || rating === undefined) ? null : String(rating);

      const tsIso = toIsoMaybe(ts);

      return { score: scoreOut, rating: ratingOut, timestamp_utc: tsIso };
    }

    async function handleMarket() {
      const cacheKeyUrl = "https://hmoney.local/market";
      const hit = await caches.default.match(new Request(cacheKeyUrl));
      if (hit) {
        const j = await hit.json().catch(() => null);
        if (j && typeof j === "object") {
          j.cache_hit = true;
          return okJson(j, 200, {
            "Cache-Control": `public, max-age=${TTL_MARKET}, s-maxage=${TTL_MARKET}`,
            "X-HMoney-Cache-Hit": "1",
            "X-HMoney-Cache-TTL": String(TTL_MARKET),
          });
        }
        return hit;
      }

      const sourceUrl = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata";

      try {
        const r = await fetch(sourceUrl, {
          method: "GET",
          headers: {
            "User-Agent": "HMoneyWorker/1.0",
            "Accept": "application/json,*/*",
          },
        });

        if (!r.ok) {
          return errJson("Fear & Greed fetch failed", 502, { status: r.status, source_url: sourceUrl });
        }

        const j = await r.json().catch(() => null);
        if (!j || typeof j !== "object") {
          return errJson("Fear & Greed parse failed", 502, { source_url: sourceUrl });
        }

        const fg = parseFearGreed(j);
        const payload = {
          ok: true,
          generated_at_utc: nowUtcIso(),
          cache_ttl_s: TTL_MARKET,
          cache_hit: false,
          source_url: sourceUrl,
          fear_greed: fg,
          debug: {
            top_keys: Object.keys(j).slice(0, 20),
            has_fear_and_greed: !!(j.fear_and_greed || j.fearAndGreed || j.fear_greed),
          },
        };

        const out = okJson(payload, 200, {
          "Cache-Control": `public, max-age=${TTL_MARKET}, s-maxage=${TTL_MARKET}`,
          "X-HMoney-Cache-Hit": "0",
          "X-HMoney-Cache-TTL": String(TTL_MARKET),
        });

        await caches.default.put(new Request(cacheKeyUrl), out.clone());
        return out;
      } catch (e) {
        return errJson("Fear & Greed error", 502, { details: String(e), source_url: sourceUrl });
      }
    }

    // -------------------------
    // News (RSS, cached)
    // -------------------------
    function stripTags(s) {
      return String(s || "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    }

    function parseRssItems(xmlText, sourceName) {
      const items = [];
      const itemBlocks = xmlText.match(/<item[\s\S]*?<\/item>/gi) || [];
      for (const block of itemBlocks) {
        const title = (block.match(/<title><!\[CDATA\[([\s\S]*?)\]\]><\/title>/i)?.[1]
          ?? block.match(/<title>([\s\S]*?)<\/title>/i)?.[1]
          ?? "").trim();
        const link = (block.match(/<link>([\s\S]*?)<\/link>/i)?.[1] ?? "").trim();
        const pubDate = (block.match(/<pubDate>([\s\S]*?)<\/pubDate>/i)?.[1] ?? "").trim();
        const desc = (block.match(/<description><!\[CDATA\[([\s\S]*?)\]\]><\/description>/i)?.[1]
          ?? block.match(/<description>([\s\S]*?)<\/description>/i)?.[1]
          ?? "").trim();

        if (!title || !link) continue;

        items.push({
          title: stripTags(title),
          url: stripTags(link),
          published_raw: pubDate || null,
          snippet: stripTags(desc).slice(0, 240) || null,
          source: sourceName,
        });
      }
      return items;
    }

    function hashId(s) {
      let h = 2166136261;
      for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = Math.imul(h, 16777619);
      }
      return (h >>> 0).toString(16);
    }

    function yahooTickerRss(t) {
      return `https://feeds.finance.yahoo.com/rss/2.0/headline?s=${encodeURIComponent(t)}&region=US&lang=en-US`;
    }

    async function handleNews() {
      const tickersRaw = url.searchParams.get("tickers") || "";
      const tickers = tickersRaw ? tickersRaw.split(",").map(normalizeTicker).filter(Boolean).slice(0, 8) : [];

      const cacheKey = `https://hmoney.local/news?tickers=${encodeURIComponent(tickers.join(","))}`;
      const hit = await caches.default.match(new Request(cacheKey));
      if (hit) {
        const j = await hit.json().catch(() => null);
        if (j && typeof j === "object") {
          j.cache_hit = true;
          return okJson(j, 200, {
            "Cache-Control": `public, max-age=${TTL_NEWS}, s-maxage=${TTL_NEWS}`,
            "X-HMoney-Cache-Hit": "1",
            "X-HMoney-Cache-TTL": String(TTL_NEWS),
          });
        }
        return hit;
      }

      const marketFeeds = [
        { name: "SEC Press Releases", url: "https://www.sec.gov/news/pressreleases.rss" },
        { name: "CNBC Top News", url: "https://www.cnbc.com/id/10001054/device/rss/rss.html" },
        { name: "CNN Business", url: "https://rss.cnn.com/rss/cnn_business.rss" },
      ];

      const feeds = tickers.length
        ? tickers.map(t => ({ name: `Yahoo Finance (${t})`, url: yahooTickerRss(t), ticker: t }))
        : marketFeeds;

      const all = [];
      for (const f of feeds) {
        const resp = await fetch(f.url, {
          method: "GET",
          headers: {
            "User-Agent": "HMoneyWorker/1.0",
            "Accept": "application/rss+xml,application/xml,text/xml,*/*"
          },
        }).catch(() => null);
        if (!resp || !resp.ok) continue;
        const text = await resp.text().catch(() => "");
        if (!text) continue;

        const items = parseRssItems(text, f.name);
        for (const it of items) {
          it.tickers = f.ticker ? [f.ticker] : [];
          all.push(it);
        }
      }

      const seen = new Set();
      const deduped = [];
      for (const it of all) {
        const key = it.url || (it.title + "|" + it.source);
        if (seen.has(key)) continue;
        seen.add(key);
        deduped.push(it);
      }

      deduped.sort((a, b) => {
        const ad = Date.parse(a.published_raw || "") || 0;
        const bd = Date.parse(b.published_raw || "") || 0;
        return bd - ad;
      });

      const itemsOut = deduped.slice(0, 80).map(it => ({
        id: hashId(it.url || it.title),
        title: it.title,
        url: it.url,
        publisher: it.source,
        published_at: it.published_raw || null,
        tickers: it.tickers || [],
        snippet: it.snippet,
      }));

      const payload = {
        ok: true,
        scope: tickers.length ? "tickers" : "market",
        tickers,
        generated_at_utc: nowUtcIso(),
        cache_ttl_s: TTL_NEWS,
        cache_hit: false,
        items: itemsOut,
      };

      const out = okJson(payload, 200, {
        "Cache-Control": `public, max-age=${TTL_NEWS}, s-maxage=${TTL_NEWS}`,
        "X-HMoney-Cache-Hit": "0",
        "X-HMoney-Cache-TTL": String(TTL_NEWS),
      });

      await caches.default.put(new Request(cacheKey), out.clone());
      return out;
    }

    // -------------------------
    // AI (Workers AI)
    // -------------------------
    function extractAiText(result) {
      if (typeof result === "string") return result;

      if (!result || typeof result !== "object") return "";

      if (typeof result.response === "string") return result.response;
      if (typeof result.output_text === "string") return result.output_text;
      if (typeof result.generated_text === "string") return result.generated_text;
      if (typeof result.text === "string") return result.text;

      if (result.result && typeof result.result === "string") return result.result;
      if (result.result && typeof result.result.response === "string") return result.result.response;

      const c0 = Array.isArray(result.choices) ? result.choices[0] : null;
      if (c0?.message?.content && typeof c0.message.content === "string") return c0.message.content;
      if (typeof c0?.text === "string") return c0.text;

      if (Array.isArray(result.messages) && result.messages.length) {
        const last = result.messages[result.messages.length - 1];
        if (last?.content && typeof last.content === "string") return last.content;
      }

      return "";
    }

    async function handleAi() {
      let body;
      try { body = await request.json(); } catch { return errJson("Invalid JSON body", 400); }

      const question = String(body.question || "").trim();
      const context = body.context ?? null;
      if (!question) return errJson("Missing 'question' in JSON body", 400);

      if (!env || !env.AI || typeof env.AI.run !== "function") {
        return errJson("Workers AI binding is not configured (env.AI missing)", 500, {
          hint: "In Cloudflare Worker settings, enable Workers AI / add AI binding named 'AI'."
        });
      }

      const system = [
        "You are a helpful assistant.",
        "Be clear and concise. Ask for clarification only if absolutely needed.",
        "",
        "Truth policy:",
        "- Do not invent prices, numbers, or headlines.",
        "- If something isn't supported by provided context JSON, say it's unverified.",
        "",
        "End every answer with: 'Not financial advice; educational decision support.'",
      ].join("\n");

      let contextText = "";
      try {
        if (context && typeof context === "object") {
          const c = { ...context };
          delete c.chat_history;
          delete c.chatHistory;
          contextText = JSON.stringify(c);
          if (contextText.length > 16000) contextText = contextText.slice(0, 16000) + "...(truncated)";
        }
      } catch {
        contextText = "(Could not serialize context)";
      }

      const user = [
        "Dashboard context JSON:",
        contextText || "(none)",
        "",
        "User question:",
        question,
        "",
        "Answer:",
      ].join("\n");

      const MODEL_PRIMARY = env?.AI_MODEL
        ? String(env.AI_MODEL)
        : "@cf/meta/llama-3-8b-instruct";

      let result;
      try {
        result = await env.AI.run(MODEL_PRIMARY, {
          messages: [
            { role: "system", content: system },
            { role: "user", content: user },
          ],
          max_tokens: 700,
          temperature: 0.6,
        });
      } catch (e) {
        return errJson("AI model call failed", 502, { details: String(e) });
      }

      const answer = extractAiText(result).trim();

      if (!answer) {
        const keys = (result && typeof result === "object") ? Object.keys(result) : [];
        let preview = "";
        try {
          preview = JSON.stringify(result);
          if (preview.length > 1500) preview = preview.slice(0, 1500) + "...(truncated)";
        } catch {
          preview = "(unserializable)";
        }
        return errJson("AI returned empty response", 502, {
          model: MODEL_PRIMARY,
          debug: { result_type: typeof result, result_keys: keys, result_preview: preview }
        });
      }

      return okJson({ ok: true, model: MODEL_PRIMARY, answer });
    }

    // -------------------------
    // Routing
    // -------------------------
    if (request.method === "GET") {
      if (path === "/" || path === "") {
        return okJson({
          ok: true,
          name: "HMoney Worker",
          gh_pages_base: GH_PAGES_BASE,
          routes: {
            "GET /market": "Fear & Greed (CNN feed, cached)",
            "GET /live?symbols=...": "BYO-key live overlay (send X-Finnhub-Key and/or X-TwelveData-Key headers)",
            "GET /pack?ticker=...": "evidence pack (current run; stale fallback via evidence_index.json)",
            "GET /news?scope=market or /news?tickers=...": "RSS headlines (cached)",
            "POST /ai (or POST /)": "AI assistant",
          },
        });
      }
      if (path === "/market") return await handleMarket();
      if (path === "/live") return await handleLive();
      if (path === "/pack") return await handlePack();
      if (path === "/news") return await handleNews();
      return errJson("Not found", 404, { path });
    }

    if (request.method === "POST") {
      if (path === "/ai" || path === "/" || path === "") return await handleAi();
      return errJson("Not found", 404, { path });
    }

    return new Response("Method not allowed", { status: 405, headers: cors });
  },
};