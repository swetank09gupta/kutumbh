# ADR-0017: Market-aware recommendation engine

**Status:** Accepted
**Date:** 2026-06

## Context

The original `recommendations.py` issued portfolio-shape advice (emergency fund, EMI ratio, asset-allocation drift) but had no view of current market valuations or any specific guidance on **where** to deploy fresh money. The user explicitly asked the engine to recommend "where to invest next and when, based on the market at that point of time" — and to keep updating as both the market and the portfolio drift.

Two needs collided:

1. **Personalised advice.** Generic "increase equity SIPs" was already there but useless — the user couldn't act on it without picking funds and amounts themselves.
2. **Market context.** Recommending "buy midcap" while Nifty Midcap 150 P/E sits at 35 (vs 5-yr avg 28) is bad advice; the same recommendation a year later when P/E falls to 25 is fine. The engine had no source of truth for current valuations.

## Options

1. **Hard-code seasonal advice** ("buy small-cap in years X, Y, Z"). Fails the moment market behaviour shifts; not really data-driven.
2. **Defer to an external robo-advisor API.** Adds a paid dependency, violates the privacy-first stance (sending holdings off-host), and locks recommendations to whatever third-party rules.
3. **Build a thin market-signals layer + rule engine** on top, fetching public free data and combining it with the user's portfolio gaps.

## Decision

Option 3. Two new modules:

- **`market_signals.py`** fetches the daily NSE indices CSV (`archives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv`) for Nifty 50, Midcap 150, Smallcap 250, and Nifty 500 P/Es. USD/INR and Nasdaq 100 come from Yahoo Finance's public chart API. Everything is cached for 12 hours at `data/market_signals_cache.json`. Hard-coded 5-year rolling averages classify each index P/E as `cheap` (< 0.92×), `fair`, or `expensive` (> 1.10×) — boundaries chosen wide enough to provide natural hysteresis (an index has to swing ~8% to flip its label).
- **`_market_timing_recommendations`** in `recommendations.py` reads the user's holdings + the signals and emits "where + how much + when" recs for each portfolio gap:
  - Large-cap < 15% of equity → suggest Nifty 50 index funds + amount + timing ("lump-sum now" if Nifty 50 is cheap, "STP over 6-12 mo" if expensive).
  - International < 5% → US/global FOF shortlist + timing.
  - Midcap ≥ 25% → "redirect SIPs to flexi" with the current midcap premium % over Nifty 50 spelled out.
  - India 10y G-Sec yield ≥ 7.25% and household lacks long-duration debt MF → "Lock In G-Sec Yields" with debt-fund shortlist.

Fund shortlists are curated and live in `_FUND_SHORTLIST` — refreshed annually based on 5-yr rolling performance + expense ratio. The dashboard renders 2-3 names per category, not a single "buy this fund" pick.

## Consequences

- **Dynamic by construction.** When Nifty Midcap P/E falls from 35 to 25 (the "expensive" → "fair" transition), the same recommendation engine emits softer timing language on the next dashboard load — no code change needed.
- **Offline-resilient.** If NSE's CSV moves or the host is offline, the cache layer falls back to conservative defaults (`source: "fallback"`) and the engine keeps working.
- **No paid feed.** All sources are public NSE/Yahoo URLs that have been stable for years.
- **Shortlists go stale.** `_FUND_SHORTLIST` is intentionally curated, not algorithmic. Yearly review is the cost of trust — auto-ranking funds without an explicit performance methodology would mislead users.
- **Hysteresis on zones, not on recs.** The rec engine doesn't dampen flip-flopping itself; that's delegated to the memory layer (ADR-0018), which tracks persistence over time so the UI can show "this rec has been active for 5 months" rather than treating each refresh as fresh.
