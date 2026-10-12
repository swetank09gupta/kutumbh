# Building Kutumb — Notes from the Trenches

A running log of what surprised me, what I'd do differently, and the bugs that took longest to find. Written so a future contributor (or future me) doesn't relearn the same lessons.

## Bank statements are not data formats — they're aesthetic choices

I started assuming "HDFC statement parser" was a unit of work. It is not. HDFC has at least three layouts in active circulation:

- **Layout A** — classic, headers on every page, tab-separated columns. Easy.
- **Layout B** — landscape PDF with the running-balance column rendered only on the *first* page of each statement; pages 2..N have the balance column blank. My early parser silently mis-read 30% of rows because it inferred `bal_col` from page 2's header positions. Fix: detect Layout B by page-1 headers, then propagate column boundaries to subsequent pages even when they're headerless. (See `parsers/hdfc.py:bal_col_lock`.)
- **Layout C** — wrapped narration across two text lines. Required a "narration reconstruction" pass that merges orphan lines back onto the parent transaction by checking date-column alignment.

Both Layout B and C bugs were caught only after the categoriser started classifying half my November transactions as "uncategorised". The lesson: **always have a dashboard panel that surfaces parser quality**, not just downstream numbers. I added a "Unknown category %" panel and a "rows with `source='llm'`" panel — both have caught at least three regressions since.

## "Just use an LLM for everything" was the wrong instinct

My first prototype piped every PDF through `llama3.2:3b` and asked for JSON. It worked. It also:

- took 60–90s per statement (vs <1.5s for a deterministic parser),
- hallucinated dates roughly 1 in 200 rows,
- couldn't be unit-tested.

I reversed the architecture: **deterministic parsers first, LLM as the fallback for unknown layouts**. The LLM now handles <5% of statements, but it's the safety net that means a new bank format doesn't break Sunday-morning sync.

## Idempotency keys are load-bearing

Early on I used a UUID PK for transactions and tried to dedup with "have I seen this exact narration before". This broke the day I re-imported two overlapping date ranges — the bank had subtly reformatted spaces in the narration, so the dedup missed and I had 4x duplicates in `transactions`.

Fixed by switching the natural key to `(account_id, posted_on, amount_paise, hash(normalize(narration)))` where `normalize` collapses whitespace, strips reference numbers that change between exports, and lowercases. Re-imports are now true no-ops. I wish I'd done this on day one.

## Grafana provisioning paid for itself in week two

I almost built a Next.js dashboard. The hour I spent learning Grafana's `provisioning/dashboards/*.yml` saved me at minimum two weekends of React work, and gave me alerting, templating, and CSV export I'd never have built myself. The downside — Grafana's editor is clunky for tightly-coupled multi-panel layouts — has been a non-issue at this scale.

## Timezone bugs hide for months

Postgres in the container defaults to UTC. The bank says "2024-03-31 23:55 IST". If you don't set `TZ=Asia/Kolkata` *and* `PGTZ=Asia/Kolkata` on the postgres container, end-of-month transactions land in the next month's "Cash Flow" panel. I lost a Sunday to this. The fix is one line in `docker-compose.yml`; the debugging took six hours.

## Coin contract notes have a dual-column layout

Zerodha contract notes from Coin (MF, not equity) render as two side-by-side tables when the trade has both purchase and redemption legs. pdfplumber returns the columns interleaved, which silently doubled some entries. Fix: detect the dual-column case by looking at `x0` clustering and split before row assembly.

## XIRR is sneaky

`numpy_financial.xirr` is deprecated. `scipy.optimize.brentq` over the NPV polynomial works but is fragile near tax-loss-harvesting cashflows that flip sign multiple times. [Superseded: the solver was later changed to bracketing, ADR-0026.] I wrote a small Newton–Raphson with a bisection fallback that's been stable across every cashflow stream I've thrown at it; <50 lines, no dependency. See `app/engine/portfolio_xirr.py`.

Validation matters: I keep 12 fixture cashflows in `tests/fixtures/xirr/` with known Excel XIRR values, and every change to the engine has to keep all 12 within ±1e-4.

## Postgres-only is plenty

I considered DuckDB for the analytics side and S3 for raw attachment storage. Neither was needed at this scale. Postgres at default settings serves every dashboard panel in <60ms over 9 years of data. The instinct to reach for "modern data stack" tooling for a personal project is mostly status-seeking.

## Things I'd do differently next time

- **Write the dedup natural key on day one.** Don't start with UUIDs and "we'll figure it out".
- **Snapshot every parsed PDF's raw text** alongside the parsed output. When a parser regresses, you want the input.
- **Build the "parser health" Grafana panel before the "net worth" one.** It's the leading indicator of every downstream bug.
- **Pin every Python lib by hash.** Two `pdfplumber` minor releases changed `extract_words()` behaviour and silently changed parser output.
- **Skip the LLM-first detour.** Start with the deterministic parser, add LLM as fallback after the first parser ships.

## What I'd still tell a friend building this

1. Your bank's PDF layout will change without notice. Build the fallback before you need it.
2. The categoriser is where you'll spend the most time post-launch. Make rule edits a 10-second loop, not a 10-minute one.
3. Privacy is a feature, not a constraint. The moment a hosted API enters the architecture, you've changed what the tool is.

## Round 2 — bugs the first happy-path didn't surface

The first user session immediately surfaced four "looks right on the dashboard, wrong in reality" issues that the unit tests never could have. Worth recording.

### Total portfolio XIRR was 55% — and obviously wrong

Zerodha monthly demat statements record share *counts*, not rupee amounts, in their transaction rows. The XIRR engine correctly excluded those rows (description prefix `DEMAT `), but the depository-snapshot account (CDSL) still contributed a terminal `+₹35.6L` inflow with no matching outflow. Mathematically the engine was being asked to find an XIRR for a series that contained only positive cashflows — it converged to a fictitious 55%. Fix in two layers: (a) mark any class with `invested = 0` as `excluded_from_total` with a reason, (b) bucket each transaction's cashflow to the holding-type of its ISIN rather than dumping the whole account into the majority bucket. After both fixes, total XIRR fell to a believable 12.5%.

### "On track" was a tautology

Every investment goal showed `on_track: true` with `gap ≈ ₹1.61`. Reading the code: `project_all_goals` passed `goal.monthly_sip_needed` (the SIP *needed* to hit the goal) as the `current_monthly_sip` parameter to the projection. The projection then computed "if you invest the needed SIP, you'll hit the target" — a circular truth. Fix: compute the family's *actual* last-6-month investing rate from real transactions + manual contributions, distribute proportionally to each goal's prescribed SIP, project against that. Five of six goals immediately flipped to `on_track: false`, which is the honest answer.

### PPF was double-counted across spouses

The retirement-corpus refresh pulled a single family-aggregated PPF figure and added it to every retirement goal. With two retirement goals (yours + spouse's) and one spouse's PPF on file, both goals received the same ₹19.37L bump. Fix: build per-owner net-worth snapshots, look up `ppf_for(g.owner_id)` per goal. Side benefit — this surfaced that the *user's* PPF (Union Bank) wasn't in the DB at all, because no Union Bank PPF e-statement is ever sent.

### PPF and SGB exist but never email a statement

For both: the position exists, the historical purchase doesn't. SGB issue records live with the RBI; PPF contributions live as transfer debits on the funding savings account. Solution was the same in shape but different in source: synthesize cashflows at the correct historical date with the right amount, write `ManualAssetContribution` rows so the XIRR engine treats them as first-class flows. SGB uses a hand-curated `_SGB_TRANCHES` table for ISINs in the dataset + a fallback to `maturity_year − 8 years`. PPF scans bank-savings debits for "PPF" patterns. Documented as [ADR-0015](adr/0015-synthetic-cashflows.md).

### Goal target_date was static, but life isn't

`retirement_age = 50` and `birth_date = 1992` should imply a retirement date of 2042. The goal row said 2050. The SIP projection paced for 24 years instead of 16. The fix: a small `_age_target_date(goal, db)` helper overrides the stored date with `owner.birth_date + retirement_age` for retirement goals, and `child.dob + 18|25|27` for child goals. Children live in `config/accounts.yml` (no DB schema needed). Required SIP for Your Retirement doubled overnight — uncomfortable but correct. Documented as [ADR-0016](adr/0016-age-based-goal-dates.md).

### Meta-lesson

The first build runs fine on a happy path. The bugs surface the instant a user (even the author) looks at a number and says "that can't be right." Build for that moment: surface the reasoning on the dashboard (what's included, what's excluded and why), and make every number cheap to trace back to its source rows.
