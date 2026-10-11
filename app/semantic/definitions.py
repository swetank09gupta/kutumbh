"""
Single definitions of income / expense / investment flow.

Every API route, card and Grafana tile that talks about spending or earning reads the SQL views
below instead of re-deriving its own filter. The rule (decided with the owner, Oct 2026):

  expense      debit that is not an own-account transfer, an investment, or a one-off loan prepayment
  prepayment   debit tagged emi/loan_prepayment — shown separately so one month's lump sum does not
               distort the trend
  income       credit tagged income (salary, interest/dividend, refund) or a refund from a merchant
  investment   anything tagged investment (buys and redemptions): capital moves, not spending/earning
  transfer     own-account moves, wallet top-ups, demat unit counts
  other_credit every remaining credit (deposits, gifts, unexplained): not income until you tag it

Only bank accounts feed cash flow. Zerodha/Coin/CDSL rows are trade cashflows and unit counts.
"""

REFUND_CATEGORIES = ("food", "shopping", "transport", "utilities", "housing", "medical",
                     "entertainment", "child_expenses", "insurance", "taxes")

_REFUNDS = ", ".join(f"'{c}'" for c in REFUND_CATEGORIES)

VIEW_SQL = [
    "DROP VIEW IF EXISTS v_cashflow_monthly",
    "DROP VIEW IF EXISTS v_cashflow CASCADE",
    f"""CREATE VIEW v_cashflow AS
SELECT t.id, a.owner_id, t.date, (date_trunc('month', t.date))::date AS month,
       t.amount, t.transaction_type, t.category, t.subcategory, t.description,
       CASE
         WHEN t.category = 'transfer'   THEN 'transfer'
         WHEN t.category = 'investment' THEN 'investment'
         WHEN t.transaction_type = 'debit' AND t.category = 'emi' AND t.subcategory = 'loan_prepayment' THEN 'prepayment'
         WHEN t.transaction_type = 'debit' THEN 'expense'
         WHEN t.category = 'income' OR t.category IN ({_REFUNDS}) THEN 'income'
         ELSE 'other_credit'
       END AS flow
FROM transactions t JOIN accounts a ON a.id = t.account_id
WHERE a.account_type = 'bank' AND a.is_active AND a.owner_id NOT LIKE '.%'""",
    """CREATE VIEW v_cashflow_monthly AS
SELECT owner_id, month,
       COALESCE(SUM(amount) FILTER (WHERE flow = 'income'), 0)       AS income,
       COALESCE(SUM(amount) FILTER (WHERE flow = 'expense'), 0)      AS expense,
       COALESCE(SUM(amount) FILTER (WHERE flow = 'prepayment'), 0)   AS prepayment,
       COALESCE(SUM(amount) FILTER (WHERE flow = 'investment' AND transaction_type = 'debit'), 0)  AS invested,
       COALESCE(SUM(amount) FILTER (WHERE flow = 'investment' AND transaction_type = 'credit'), 0) AS redeemed,
       COALESCE(SUM(amount) FILTER (WHERE flow = 'other_credit'), 0) AS other_credit
FROM v_cashflow GROUP BY owner_id, month""",
]
