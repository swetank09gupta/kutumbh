# Working agreement for this repository

This is a family-finance system whose value is that **every page, card and person shows the same, correct numbers**. Most past regressions came from fixing one thing while another page silently changed. So:

0. **Rules first, then plan, then change.** For anything about how an instrument works (PPF, FD/MOD, MF, equity, SGB, loan, EPF, tax), read `docs/INSTRUMENTS.md`; if the rule is missing or stale, research the public/official source and write it there (with link and date) *before* touching code. Work comes from `docs/ROADMAP.md`; a new request goes into its Inbox and is triaged, not coded on the spot (a bug in shipped behaviour is the exception).
1. **Before changing anything**, read `docs/INVARIANTS.md` and the relevant row of `docs/DECISIONS.md` (and its ADR). A decision recorded there is not to be reversed silently.
2. **Run `scripts/check.sh --live` before and after.** Tests must pass, the live invariants must hold, and every moved figure must be one you meant to move.
3. **A behaviour change is not done until** the code, a test that fails without it, the invariant/decision rows, the ADR (or a "Revisited" note on the one it contradicts) and the docs that describe the old behaviour (`grep` for it) all agree — in the same commit.
4. **Changing a parser?** Bump that parser's `_PARSER_VERSION` (parse results are cached per parser; `_CACHE_SCHEMA` in `app/ingestion/parsers/base.py` only when the `ParseResult` shape changes: it invalidates every parser and a cold re-parse takes hours) and re-read the affected statements.
5. **Never invent data.** Recover missing history from evidence (statements, alerts, public NAV/rate tables, lender letters) or exclude it and say how much is missing.
6. **One definition per number.** Reuse the shared view/function (`v_cashflow`, `latest_holdings_deduped`, `build_positions`, `project_all_goals`); do not recompute in a page, panel or route.
7. **Do not remove a data surface** (an asset, an owner, a card) to make something else work; add an invariant if one was missing.
8. Check the UI for Family, Self and Spouse. Commit as Swetank Gupta only; no attribution lines. Docs stay short.
