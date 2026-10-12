# Invariants — what must stay true whatever changes

Each rule below has an owner (a test or a live check). If you change behaviour on purpose, change the rule, the test and the matching row in [DECISIONS.md](DECISIONS.md) in the same commit. If a rule fails, the change is wrong until proven otherwise.

Run everything with `scripts/check.sh` (tests on a scratch database; `--live` adds the live-data invariants and the figure diff). A pre-push hook runs it (`git config core.hooksPath .githooks`).

## Cross-page invariants (`app/invariants.py`)

They run after every sync (`/api/health/consistency` → `product_invariants`, severity *fail*) and in `tests/test_invariants_db.py` against a deliberately awkward household. Each has a mutation test that re-creates the bug and proves the rule catches it.

| ID | Rule | Origin |
|---|---|---|
| INV-01 | Every manual investment you hold (PPF, FD, …) is a row on the Investments page | PPF vanished from the page after a rewrite (2026-10) |
| INV-02 | The class rows add up to the total value; the allocation uses the same numbers | Allocation donut ignored PPF and deposits |
| INV-03 | Investments agree with net worth for FD, PPF, funds, equity+bonds | Bonds were re-priced "by design" and disagreed |
| INV-04 | Family = Self + Spouse, for value, invested and proceeds | Phantom owners inflated Family |
| INV-05 | The total return is the sum of the class returns | Total silently excluded mutual funds |
| INV-06 | Every current holding is listed at the holdings-view value | — |
| INV-07 | The loan in net worth is the lender's figure rolled forward, unless you typed a newer one | Loan was 1 Cr 28 L when HDFC said 1 Cr 5 L |
| INV-08 | No payload contains NaN or infinity | — |

## Behavioural decisions and their tests

| Decision | Enforced by |
|---|---|
| Nothing is invented: no assumed redemptions, no guessed costs | `tests/test_no_invented_data.py` |
| ISIN-tagged flows count whichever account they sit in (bank SIP mandates) | `test_invariants_db::test_a_bank_sip_debit_counts_as_a_purchase` |
| Missing cost is named with a coverage %, not hidden; one unknown fund never blanks a class | `test_invariants_db::test_the_awkward_cases_are_really_in_play`, `test_returns_core` |
| Under a year of holding: plain gain, not an annual rate | `tests/test_returns_core.py` |
| XIRR finds a rate for sign-changing flows and says `None` when none exists | `tests/test_xirr.py` |
| A trade settling over month-end is not an unknown lot | `tests/test_ledger_rules.py` |
| SGB units cost the issue price; ordinary untraced units stay "partial" | `tests/test_ledger_rules.py` |
| Derived trades price from NAV, confirmed by a bank debit, one debit confirms one line | `tests/test_derive.py` |
| A figure you typed later is never overwritten by an older lender letter | `test_invariants_db::test_a_figure_you_typed_later…` |
| Loan roll-forward reproduces the lender's next figure | `tests/test_loan.py` |
| Inflation: one automatic app-wide rate (10-year CPI average); an override can be set and cleared; goals ignore their own rate | `tests/test_settings.py`, `tests/test_inflation.py` |
| PPF uses the notified rate and the 5th-to-month-end rule | `tests/test_small_savings.py` |
| Bank alerts are evidence, never cashflow; each gets exactly one status | `tests/test_alert_recon.py`, `tests/test_bank_alert.py` |
| Expense / income definitions; tag authority order | `tests/test_definitions_db.py`, `tests/test_txn_layer.py` |
| Every API call the UI makes hits a route that exists, with that method | `tests/test_ui_api_contract.py` |
| Goal cards, projections and Grafana use one SIP figure | consistency check `goal_sip_stored_equals_projection` |
| Every page reads the same owner (Self / Spouse / Family) | UI `switchOwner`; browser check in the release routine below |

## Release routine for any change

1. Read this file and the DECISIONS row you are about to touch.
2. `scripts/check.sh --live` **before** the change (baseline) and **after** it. Every figure that moved must be one the change was meant to move; accept intended moves with `scripts/check.sh --accept`.
3. A behaviour change updates: the code, a test that fails without it, the invariant/decision rows, the ADR (or a "Revisited" note on the one it contradicts), and the docs that describe it (`grep` for the old behaviour).
4. Check the UI in the browser for Family, Self and Spouse (no console errors, no failed requests).
5. Never invent data to make a number appear; recover it from evidence or show what is missing.
