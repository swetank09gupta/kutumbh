# Working agreement for this repository

This is a family-finance system whose value is that **every page, card and person shows the same, correct numbers**. Most past regressions came from fixing one thing while another page silently changed. So:

1. **Before changing anything**, read `docs/INVARIANTS.md` and the relevant row of `docs/DECISIONS.md` (and its ADR). A decision recorded there is not to be reversed silently.
2. **Run `scripts/check.sh --live` before and after.** Tests must pass, the live invariants must hold, and every moved figure must be one you meant to move.
3. **A behaviour change is not done until** the code, a test that fails without it, the invariant/decision rows, the ADR (or a "Revisited" note on the one it contradicts) and the docs that describe the old behaviour (`grep` for it) all agree — in the same commit.
4. **Never invent data.** Recover missing history from evidence (statements, alerts, public NAV/rate tables, lender letters) or exclude it and say how much is missing.
5. **One definition per number.** Reuse the shared view/function (`v_cashflow`, `latest_holdings_deduped`, `build_positions`, `project_all_goals`); do not recompute in a page, panel or route.
6. **Do not remove a data surface** (an asset, an owner, a card) to make something else work; add an invariant if one was missing.
7. Check the UI for Family, Self and Spouse. Commit as Swetank Gupta only; no attribution lines. Docs stay short.
