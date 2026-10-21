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
| INV-09 | The Manual Assets page shows the same value as net worth for deposits and PPF | Deposit values were principal on one page and accrued on another |

## Behavioural decisions and their tests

| Decision | Enforced by |
|---|---|
| Nothing is invented: no assumed redemptions, no guessed costs | `tests/test_no_invented_data.py` |
| A reversal credit is a row of its own even when it shares date and reference with the debit it undoes; the first row of an HDFC statement without an opening balance is kept only when its printed amount is proven | `tests/test_bank_statement_gaps.py`, `test_identical_bank_rows_db` |
| ISIN-tagged flows count whichever account they sit in (bank SIP mandates) | `test_invariants_db::test_a_bank_sip_debit_counts_as_a_purchase` |
| The headline return states its coverage of everything held, and names what is left out and why | `test_invariants_db::test_the_headline_return_says_what_it_leaves_out` |
| Missing cost is named with a coverage %, not hidden; one unknown fund never blanks a class | `test_invariants_db::test_the_awkward_cases_are_really_in_play`, `test_returns_core` |
| Under a year of holding: plain gain, not an annual rate | `tests/test_returns_core.py` |
| XIRR finds a rate for sign-changing flows and says `None` when none exists | `tests/test_xirr.py` |
| A trade settling over month-end is not an unknown lot | `tests/test_ledger_rules.py` |
| SGB units cost the issue price; ordinary untraced units stay "partial" | `tests/test_ledger_rules.py` |
| Derived trades price from NAV, confirmed by a bank debit, one debit confirms one line | `tests/test_derive.py` |
| A figure you typed later is never overwritten by an older lender letter | `test_invariants_db::test_a_figure_you_typed_later…` |
| A cumulative deposit is worth its statement amount plus the interest in its maturity value, accrued; the same on net worth, Investments and Manual Assets | `tests/test_deposits.py`, `test_invariants_db::test_a_cumulative_deposit…`, INV-03, INV-09 |
| A deposit the newest statement no longer lists is closed: not in net worth, Manual Assets or Investments | `test_invariants_db::test_a_closed_deposit_is_worth_nothing_anywhere`, INV-01/09 |
| PPF contributions are rebuilt from the statements' balance series; interest-credit steps are not contributions | `tests/test_deposit_flows.py`, `test_invariants_db::test_ppf_contributions_are_rebuilt…` |
| Every deadline cites a registry rule that resolves, or the evidence it came from; unknown opening dates are listed as gaps | `tests/test_deadlines.py` (incl. mutation), consistency check `deadlines_cite_a_basis` |
| A Coin order is stored once however often its mail is re-read and whichever ISIN it was filed under | `tests/test_coin_duplicates_db.py` (fails without the fix) |
| The scheduler, the Sync button and reprocess run the same pipeline; one failing evidence source never stops the rest | `tests/test_automation.py` |
| An observed public rate never rewrites history: a change is a new dated entry, an unreadable page changes nothing | `tests/test_automation.py` |
| The loan roll applies the lender's letter EMI, not a lagging debit | `test_loan_report_db::test_the_roll_applies_the_letter_emi_not_a_lagging_debit` |
| A letter the roll cannot reproduce is reported with its size; the reset month is inferred only when it fits to the rupee | `test_loan_report_db` (differs / reset-month / missing-EMI cases) |
| The loan forecast needs every letter to fit repo + a constant spread, and is labelled | `test_loan_report_db::test_one_letter_off_the_pattern_switches_the_forecast_off` |
| The 80C advice reads the loan schedule, not its own principal estimate | `test_loan_report_db::test_the_eighty_c_advice_reads_the_schedule…` |
| SGB coupons are matched by arithmetic (amount to the paisa and calendar date); ISIN-tagged only for held bonds | `tests/test_sgb.py`, `test_sgb_evidence_db` (mutation-checked) |
| SGB units cost what the bank shows was paid, else the nominal price (flagged); unknown tranches are never guessed | `test_sgb`, `test_ledger_rules` |
| A Manual Asset SGB is identified by its name, valued once (idempotent note), and its coupons are return flows on both pages | `test_sgb_evidence_db` (hand-held cases) |
| A dividend credit must equal shares held before the ex-date × the per-share amount (to the paisa, or ×0.9 after TDS); a purchase on the ex-date is not entitled; ambiguity is reported | `test_dividends_tax_db` |
| A bank statement is stored only if its rows reconcile to the opening and closing balance it prints (Axis); an Axis deposit is judged against Axis statements, not SBI's | `tests/test_axis_layout.py` (mutation-checked) |
| Two identical lines of one Coin mail are two orders; re-reading (also after an ISIN correction, also without a folio) never adds a copy | `tests/test_coin_duplicates_db.py` (mutation-checked) |
| Deposit growth starts on the date the printed principal is true (statement date with accrued interest, open date without) | `tests/test_axis_layout.py::AxisDepositGrowthStartsAtTheOpenDate` |
| The long-running services restart by themselves; the bootstrap decides "fresh" by transactions | `tests/test_deploy_files.py` |
| A contract note with no ISIN takes its ticker's ISIN only when that is unambiguous | `tests/test_contract_note_2018.py` |
| Capital gains: FIFO, long-term after more than 12 months, rates and exemption by sale date, set-off rules, unknown cost never costed | `tests/test_tax_gains.py` (hand-calculated) |
| A tax threshold or cap is looked up for the financial year in question; with no verified value for that year no check is made | `test_dividends_tax_db` |
| Deposit part-withdrawals, top-ups and interest added to the principal are told apart | `tests/test_deposit_flows.py` |
| Each purchase takes its own bank debit even when two debits have the same amount | `tests/test_derive_db.py` |
| A PPF is worth its statement balance (interest is credited only on 31 March); its return is measured between 31-March balances and earns about the notified rate | `test_invariants_db::test_a_ppf_is_worth…`, `test_a_ppf_measured…`, `tests/test_deposit_flows.py`, `tests/test_deposits.py` |
| Each PPF year reconciles (opening + deposits + interest = closing); a deposit found only through its interest is flagged | `tests/test_deposit_flows.py`, `test_invariants_db::test_the_ppf_reconciliation_report…`, consistency `ppf_years_reconcile` |
| Tax deducted at source is added back to a deposit's interest (returns are pre-tax) | `test_invariants_db::test_tax_deducted_at_source…`, `tests/test_bank_alert.py` |
| Loan roll-forward reproduces the lender's next figure | `tests/test_loan.py` |
| Inflation: one automatic app-wide rate (10-year CPI average); an override can be set and cleared; goals ignore their own rate | `tests/test_settings.py`, `tests/test_inflation.py` |
| PPF uses the notified rate and the 5th-to-month-end rule | `tests/test_small_savings.py` |
| Bank alerts are evidence, never cashflow; each gets exactly one status | `tests/test_alert_recon.py`, `tests/test_bank_alert.py` |
| Every ledger trade with a cashflow link points at a cashflow row that exists (0 orphans after any re-key, re-read or duplicate removal); a contract note and its tradebook row dedupe to one trade and one cashflow | `tests/test_ledger_shared_cashflow_db.py` |
| Planning is exact where it can be: with volatility off the simulator equals the closed-form growth; more saving never lowers the odds; the same inputs give the same answer; retirement need is the present value of the spending; the SIP for p-confidence is exactly the SIP that reaches p | `tests/test_planning_engine.py` |
| A missing statement month counts as explained only when that month's alerts (copies excluded) add up to the break exactly; a partial sum stays a warning; a gap is accepted only by an exact (owner, account, month) entry the owner wrote | `test_identical_bank_rows_db` (`test_a_missing_month_is_explained…`, `test_alerts_that_do_not_add_up…`) |
| Expense / income definitions; tag authority order | `tests/test_definitions_db.py`, `tests/test_txn_layer.py` |
| Every API call the UI makes hits a route that exists, with that method | `tests/test_ui_api_contract.py` |
| Goal cards, projections and Grafana use one SIP figure | consistency check `goal_sip_stored_equals_projection` |
| Every page reads the same owner (Self / Spouse / Family) | UI `switchOwner`; browser check in the release routine below |
| An update run covers everything since the last successful run | `tests/test_automation.py` (catch-up window) |
| A job missed while the machine slept runs once on wake; an overdue update is run by the watchdog; a stale snapshot never turns a small deposit/goal accrual difference into a failure, and never excuses a large one | `tests/test_automation.py` (`MissedJobs`, `StaleSnapshotIsAWarningNotAFailure`) |
| A past loan year shows the lender's certified figures when one exists | `tests/test_loan_report_db.py` |
| A bonus/IPO trade exists only when a demat credit of the same quantity proves it | `tests/test_registrar_notices.py` |
| A dividend credit matches only shares our trades say were held (name+arithmetic only where none are recorded) | `tests/test_dividends_tax_db.py` |
| Every active tax year has a slab entry; probed rules were re-confirmed recently | consistency checks `tax_slabs_are_current`, `rules_are_current` |
| Identical reference-less bank rows are kept apart exactly when their running balances differ | `tests/test_identical_bank_rows_db.py` |
| Each bank account's printed balance chains day to day | consistency check `bank_balances_chain`, `tests/test_continuity.py` |
| Funds are valued at AMFI's NAV and shares at market; a fallback is reported | consistency check `prices_are_live`, `tests/test_amfi_navall.py` |
| Provisional (alert) spending leaves the total when the statement carries it, and never includes transfers, sweeps, SIPs or credits | `tests/test_provisional_db.py` |

## Release routine for any change

1. Read this file and the DECISIONS row you are about to touch.
2. `scripts/check.sh --live` **before** the change (baseline) and **after** it. Every figure that moved must be one the change was meant to move; accept intended moves with `scripts/check.sh --accept`.
3. A behaviour change updates: the code, a test that fails without it, the invariant/decision rows, the ADR (or a "Revisited" note on the one it contradicts), and the docs that describe it (`grep` for the old behaviour).
4. Check the UI in the browser for Family, Self and Spouse (no console errors, no failed requests).
5. Never invent data to make a number appear; recover it from evidence or show what is missing.
