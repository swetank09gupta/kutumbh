import unittest
from datetime import date
from decimal import Decimal as D

from app.ledger.rules import (
    Movement, Trade, compute_isin_state, effective_trades, match_movements, settle_date,
)

ISIN = "INE000A01010"


def T(i, d, side, qty, price="100", source="contract_note"):
    return Trade(str(i), ISIN, d, side, D(qty), D(price), D(qty) * D(price), source)


def M(i, d, qin="0", qout="0", run=None, seq=None, f="s1"):
    return Movement(str(i), ISIN, d, D(qin), D(qout), D(run) if run is not None else None, "", seq, f)


class EffectiveTrades(unittest.TestCase):
    def test_same_totals_lower_rank_is_superseded_without_conflict(self):
        cn = T("cn", date(2026, 9, 1), "B", "10", source="contract_note")
        tb = [T("t1", date(2026, 9, 1), "B", "4", source="tradebook"),
              T("t2", date(2026, 9, 1), "B", "6", source="tradebook")]
        eff, sup, conf = effective_trades([cn, *tb])
        self.assertEqual([t.id for t in eff], ["cn"])
        self.assertEqual({t.id for t in sup}, {"t1", "t2"})
        self.assertEqual(conf, [])

    def test_different_totals_reported_as_conflict_best_source_kept(self):
        cn = T("cn", date(2026, 9, 1), "B", "10")
        tb = T("t1", date(2026, 9, 1), "B", "7", source="tradebook")
        eff, _, conf = effective_trades([cn, tb])
        self.assertEqual([t.id for t in eff], ["cn"])
        self.assertEqual(len(conf), 1)
        self.assertEqual((conf[0].kept_qty, conf[0].other_qty), (D(10), D(7)))

    def test_buy_and_sell_same_day_are_independent(self):
        eff, sup, _ = effective_trades([T("b", date(2026, 9, 1), "B", "5"),
                                        T("s", date(2026, 9, 1), "S", "5", source="tradebook")])
        self.assertEqual(len(eff), 2)
        self.assertEqual(sup, [])


class Matching(unittest.TestCase):
    def test_single_trade_matches_movement_next_day(self):
        r = match_movements([T(1, date(2026, 9, 1), "B", "10")],
                            [M("m", date(2026, 9, 2), qin="10")])
        self.assertEqual(r.matches, {"1": "m"})
        self.assertEqual((r.unmatched_trades, r.unmatched_movements), ([], []))

    def test_order_level_rows_sum_to_one_movement(self):
        r = match_movements([T(1, date(2026, 9, 1), "B", "4"), T(2, date(2026, 9, 1), "B", "6")],
                            [M("m", date(2026, 9, 2), qin="10")])
        self.assertEqual(set(r.matches), {"1", "2"})

    def test_two_days_netted_into_one_movement(self):
        r = match_movements([T(1, date(2026, 9, 1), "B", "4"), T(2, date(2026, 9, 2), "B", "6")],
                            [M("m", date(2026, 9, 3), qin="10")])
        self.assertEqual(set(r.matches), {"1", "2"})

    def test_trade_is_never_used_twice(self):
        r = match_movements([T(1, date(2026, 9, 1), "B", "10")],
                            [M("a", date(2026, 9, 2), qin="10"), M("b", date(2026, 9, 3), qin="10")])
        self.assertEqual(len(r.matches), 1)
        self.assertEqual(len(r.unmatched_movements), 1)

    def test_quantity_mismatch_stays_unmatched_on_both_sides(self):
        r = match_movements([T(1, date(2026, 9, 1), "B", "10")],
                            [M("m", date(2026, 9, 2), qin="9")])
        self.assertEqual(len(r.unmatched_trades), 1)
        self.assertEqual(len(r.unmatched_movements), 1)

    def test_outside_window_does_not_match(self):
        r = match_movements([T(1, date(2026, 8, 1), "B", "10")],
                            [M("m", date(2026, 9, 2), qin="10")])
        self.assertEqual(r.matches, {})

    def test_buy_does_not_match_sell_movement(self):
        r = match_movements([T(1, date(2026, 9, 1), "B", "10")],
                            [M("m", date(2026, 9, 2), qout="10")])
        self.assertEqual(r.matches, {})

    def test_fractional_mf_units(self):
        r = match_movements([T(1, date(2026, 9, 2), "B", "181.098")],
                            [M("m", date(2026, 9, 2), qin="181.098")])
        self.assertEqual(r.matches, {"1": "m"})


def B(y, m, d, qty):
    return (date(y, m, d), D(qty))


class Settlement(unittest.TestCase):
    def test_exchange_trade_settles_next_business_day(self):
        self.assertEqual(settle_date(T(1, date(2026, 8, 31), "B", "1")), date(2026, 9, 1))     # Mon -> Tue
        self.assertEqual(settle_date(T(1, date(2026, 8, 28), "B", "1")), date(2026, 8, 31))    # Fri -> Mon

    def test_printed_settlement_date_wins(self):
        t = Trade("1", ISIN, date(2026, 8, 3), "B", D(1), settle=date(2026, 8, 7))
        self.assertEqual(settle_date(t), date(2026, 8, 7))

    def test_coin_credits_same_day(self):
        self.assertEqual(settle_date(T(1, date(2026, 8, 31), "B", "1", source="coin")), date(2026, 8, 31))


class Intervals(unittest.TestCase):
    def test_trades_explain_the_change_between_statements(self):
        # Jul 31: 10 held. Bought 5 on Aug 4. Aug 31: 15 held.
        s = compute_isin_state(ISIN, [T(1, date(2026, 8, 4), "B", "5", "100")], [],
                               [B(2026, 7, 31, 10), B(2026, 8, 31, 15)])
        (iv,) = s.intervals
        self.assertTrue(iv.reconciled)
        self.assertEqual((iv.actual, iv.traded, iv.residual), (D(5), D(5), D(0)))

    def test_missing_trade_leaves_positive_residual(self):
        s = compute_isin_state(ISIN, [], [], [B(2026, 7, 31, 10), B(2026, 8, 31, 14)])
        (iv,) = s.intervals
        self.assertEqual(iv.residual, D(4))
        self.assertFalse(iv.reconciled)
        self.assertEqual(s.position.unknown_qty, D(14))          # 10 pre-history + 4 untraced

    def test_duplicate_trade_leaves_negative_residual(self):
        trades = [T(1, date(2026, 8, 4), "B", "5", "100"), T(2, date(2026, 8, 4), "B", "5", "100", source="coin")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 10), B(2026, 8, 31, 15)])
        self.assertEqual(s.intervals[0].residual, D(-5))
        self.assertIn("ledger_exceeds_statement", s.position.flags)
        self.assertEqual(s.position.qty, D(15))                  # statement wins

    def test_trade_settling_after_a_statement_belongs_to_the_next_interval(self):
        # Mon 31 Aug trade settles Tue 1 Sep: not part of the Aug interval
        s = compute_isin_state(ISIN, [T(1, date(2026, 8, 31), "B", "3", "100")], [],
                               [B(2026, 7, 31, 10), B(2026, 8, 31, 10), B(2026, 9, 30, 13)])
        self.assertEqual([iv.reconciled for iv in s.intervals], [True, True])

    def test_missing_line_in_statement_does_not_matter_when_balances_agree(self):
        # no movement lines at all, yet the trade is proven by the balance change
        s = compute_isin_state(ISIN, [T(1, date(2026, 8, 10), "B", "3", "100")], [],
                               [B(2026, 7, 31, 18), B(2026, 8, 31, 21)])
        self.assertTrue(s.intervals[0].reconciled)

    def test_unmatched_lines_are_surfaced_for_a_failed_interval(self):
        m = M("x", date(2026, 8, 12), qin="4", run="14", seq=0)
        s = compute_isin_state(ISIN, [], [m], [B(2026, 7, 31, 10), B(2026, 8, 31, 14)])
        self.assertEqual([x.id for x in s.intervals[0].unmatched_lines], ["x"])


class SettlementObservedFromLines(unittest.TestCase):
    def test_coin_unit_credited_next_day_belongs_to_the_next_interval(self):
        # Coin e-mail dated Tue 30 Nov; BSE credits the units Wed 1 Dec (observed on the line)
        trade = T(1, date(2021, 11, 30), "B", "2", "100", source="coin")
        line = M("m", date(2021, 12, 1), qin="2", run="12", seq=0)
        s = compute_isin_state(ISIN, [trade], [line],
                               [B(2021, 10, 31, 10), B(2021, 11, 30, 10), B(2021, 12, 31, 12)])
        self.assertEqual([iv.reconciled for iv in s.intervals], [True, True])

    def test_without_the_line_the_same_trade_would_be_misplaced(self):
        trade = T(1, date(2021, 11, 30), "B", "2", "100", source="coin")
        s = compute_isin_state(ISIN, [trade], [],
                               [B(2021, 10, 31, 10), B(2021, 11, 30, 10), B(2021, 12, 31, 12)])
        self.assertEqual([iv.reconciled for iv in s.intervals], [False, False])


class Tolerance(unittest.TestCase):
    def test_fund_units_tolerate_rounding_but_shares_do_not(self):
        fund, share = "INF179K01YS4", "INE000A01010"
        for isin, ok in ((fund, True), (share, False)):
            s = compute_isin_state(isin, [Trade("1", isin, date(2026, 8, 4), "B", D("3.868"), D(100))], [],
                                   [(date(2026, 7, 31), D(0)), (date(2026, 8, 31), D("3.886"))])
            self.assertEqual(s.intervals[0].reconciled, ok, isin)


class Positions(unittest.TestCase):
    def test_statement_plus_pending_trade(self):
        # Statement 31 Aug holds 100; bought 10 on 5 Sep (after statement) -> 110
        s = compute_isin_state(ISIN, [T(1, date(2026, 9, 5), "B", "10", "120")], [],
                               [B(2026, 8, 31, 100)])
        self.assertEqual(s.position.qty, D(110))
        self.assertEqual(len(s.pending), 1)
        self.assertEqual(s.position.known_qty, D(10))
        self.assertEqual(s.position.unknown_qty, D(100))     # 100 came with no traced price
        self.assertEqual(s.position.quality, "partial")
        self.assertIsNone(s.position.invested)

    def test_trade_settling_across_month_end_does_not_make_cost_partial(self):
        # bought 2 on Aug 31: the depository credited it by month-end, our T+1 rule settles it in September
        t = [T(1, date(2026, 8, 31), "B", "2", "100"), T(2, date(2026, 9, 10), "B", "3", "100")]
        s = compute_isin_state(ISIN, t, [], [B(2026, 7, 31, 0), B(2026, 8, 31, 2), B(2026, 9, 30, 5)])
        self.assertEqual([iv.residual for iv in s.intervals], [D(2), D(-2)])
        self.assertEqual(s.position.quality, "exact")
        self.assertEqual(s.position.invested, D(500))

    def test_units_with_no_trade_cost_the_published_sgb_issue_price(self):
        sgb = "IN0020210178"
        s = compute_isin_state(sgb, [], [], [(date(2026, 8, 31), D(25))])
        self.assertEqual(s.position.quality, "exact")
        self.assertEqual(s.position.invested, D(25) * D("4761"))
        self.assertEqual(s.position.first_buy_date, date(2021, 11, 2))

    def test_untraced_units_of_an_ordinary_fund_stay_partial(self):
        s = compute_isin_state(ISIN, [], [], [(date(2026, 8, 31), D(25))])
        self.assertEqual(s.position.quality, "partial")

    def test_fully_traced_position_is_exact_with_weighted_average(self):
        trades = [T(1, date(2026, 8, 3), "B", "10", "100"), T(2, date(2026, 8, 4), "B", "10", "120")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 0), B(2026, 8, 31, 20)])
        self.assertEqual(s.position.quality, "exact")
        self.assertEqual(s.position.avg_cost, D(110))
        self.assertEqual(s.position.invested, D(2200))

    def test_sell_reduces_cost_pro_rata(self):
        trades = [T(1, date(2026, 8, 3), "B", "10", "100"), T(2, date(2026, 8, 10), "S", "4", "150")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 0), B(2026, 8, 31, 6)])
        self.assertEqual(s.position.qty, D(6))
        self.assertEqual(s.position.known_cost, D(600))
        self.assertEqual(s.position.avg_cost, D(100))

    def test_fifo_sells_oldest_lots_first(self):
        # Zerodha Console uses FIFO: buy 10@100, buy 10@200, sell 10 -> remaining 10@200
        trades = [T(1, date(2026, 8, 3), "B", "10", "100"), T(2, date(2026, 8, 4), "B", "10", "200"),
                  T(3, date(2026, 8, 10), "S", "10", "250")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 0), B(2026, 8, 31, 10)])
        self.assertEqual(s.position.avg_cost, D(200))
        self.assertEqual(s.position.known_cost, D(2000))
        self.assertEqual(s.position.first_buy_date, date(2026, 8, 4))

    def test_partial_lot_consumption(self):
        trades = [T(1, date(2026, 8, 3), "B", "10", "100"), T(2, date(2026, 8, 4), "B", "10", "200"),
                  T(3, date(2026, 8, 10), "S", "15", "250")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 0), B(2026, 8, 31, 5)])
        self.assertEqual((s.position.known_qty, s.position.known_cost), (D(5), D(1000)))

    def test_pre_history_units_are_sold_first_so_traced_cost_survives(self):
        # 10 held before history (unknown cost, oldest) + buy 10@100, sell 10 -> the 10 traced units remain
        trades = [T(1, date(2026, 8, 3), "B", "10", "100"), T(2, date(2026, 8, 10), "S", "10", "150")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 10), B(2026, 8, 31, 10)])
        self.assertEqual((s.position.known_qty, s.position.unknown_qty), (D(10), D(0)))
        self.assertEqual(s.position.quality, "exact")
        self.assertEqual(s.position.avg_cost, D(100))

    def test_fully_sold_position_closes_to_zero(self):
        trades = [T(1, date(2026, 8, 3), "B", "10", "100"), T(2, date(2026, 8, 20), "S", "10", "130")]
        s = compute_isin_state(ISIN, trades, [], [B(2026, 7, 31, 0), B(2026, 8, 31, 0)])
        self.assertEqual(s.position.qty, D(0))
        self.assertTrue(s.intervals[0].reconciled)

    def test_no_statement_at_all_counts_every_trade(self):
        s = compute_isin_state(ISIN, [T(1, date(2026, 9, 5), "B", "10", "120")], [], [])
        self.assertEqual(s.position.qty, D(10))
        self.assertEqual(s.position.quality, "exact")

    def test_sell_with_no_known_buy_surfaces_as_untraced_inflow(self):
        # statements show 0 -> 0 but a sell of 5 settled in between: 5 must have arrived untraced
        s = compute_isin_state(ISIN, [T(1, date(2026, 8, 10), "S", "5", "100")], [],
                               [B(2026, 7, 31, 0), B(2026, 8, 31, 0)])
        self.assertEqual(s.intervals[0].residual, D(5))
        self.assertEqual(s.position.qty, D(0))


if __name__ == "__main__":
    unittest.main()


class PreHistory(unittest.TestCase):
    def test_trades_before_first_statement_exceeding_it_use_a_distinct_flag(self):
        s = compute_isin_state(ISIN, [T(1, date(2026, 7, 1), "B", "10", "100")], [], [B(2026, 7, 31, 4)])
        self.assertIn("pre_history_exceeds", s.position.flags)
        self.assertNotIn("ledger_exceeds_statement", s.position.flags)
        self.assertEqual(s.position.quality, "partial")
        self.assertEqual(s.position.qty, D(4))


class CrossDayFundDedupe(unittest.TestCase):
    def test_tradebook_order_and_coin_allotment_a_day_apart_count_once(self):
        coin = T("c", date(2026, 5, 5), "B", "181.098", "55.22", source="coin")
        tb = T("t", date(2026, 5, 4), "B", "181.098", "55.216", source="tradebook")
        eff, sup, _ = effective_trades([coin, tb])
        self.assertEqual([t.id for t in eff], ["c"])
        self.assertEqual([t.id for t in sup], ["t"])

    def test_tradebook_alone_is_kept_so_it_fills_a_missing_email(self):
        tb = T("t", date(2026, 2, 2), "B", "81.695", "122", source="tradebook")
        eff, sup, _ = effective_trades([tb])
        self.assertEqual(([t.id for t in eff], sup), (["t"], []))

    def test_whole_unit_quantities_are_not_matched_across_days(self):
        a = T("a", date(2026, 5, 5), "B", "10", "100", source="contract_note")
        b = T("b", date(2026, 5, 4), "B", "10", "100", source="tradebook")
        eff, _, _ = effective_trades([a, b])
        self.assertEqual(len(eff), 2)

    def test_two_sips_of_the_same_size_a_month_apart_are_both_kept(self):
        a = T("a", date(2026, 4, 2), "B", "84.498", "118", source="coin")
        b = T("b", date(2026, 5, 4), "B", "84.498", "118", source="tradebook")
        eff, _, _ = effective_trades([a, b])
        self.assertEqual(len(eff), 2)

    def test_each_better_trade_supersedes_at_most_one_duplicate(self):
        coin = T("c", date(2026, 5, 5), "B", "10.5", "100", source="coin")
        t1 = T("t1", date(2026, 5, 4), "B", "10.5", "100", source="tradebook")
        t2 = T("t2", date(2026, 5, 3), "B", "10.5", "100", source="tradebook")
        eff, sup, _ = effective_trades([coin, t1, t2])
        self.assertEqual(len(sup), 1)
        self.assertEqual(len(eff), 2)


def A(i, d, qin="0", qout="0"):
    return Movement(str(i), ISIN, d, D(qin), D(qout), None, "CDSL alert *1 00:00:00", None, "", alert=True)


class Alerts(unittest.TestCase):
    def test_alert_after_the_statement_with_no_trade_adds_units_with_unknown_cost(self):
        s = compute_isin_state(ISIN, [], [], [B(2026, 8, 31, 100)], [A("a", date(2026, 10, 5), qin="335.064")])
        self.assertEqual(s.position.qty, D("435.064"))
        self.assertEqual([a.id for a in s.alert_only], ["a"])
        self.assertEqual(s.position.quality, "partial")

    def test_alert_for_a_pending_trade_is_not_counted_twice(self):
        trade = T(1, date(2026, 10, 5), "B", "335.064", "40", source="coin")
        s = compute_isin_state(ISIN, [trade], [], [B(2026, 8, 31, 100)], [A("a", date(2026, 10, 5), qin="335.064")])
        self.assertEqual(s.position.qty, D("435.064"))
        self.assertEqual(s.alert_only, [])
        self.assertEqual(s.position.known_qty, D("335.064"))

    def test_alert_on_or_before_the_statement_date_is_ignored(self):
        s = compute_isin_state(ISIN, [], [], [B(2026, 8, 31, 100)], [A("a", date(2026, 8, 20), qin="5")])
        self.assertEqual(s.position.qty, D(100))

    def test_debit_alert_reduces_the_position(self):
        s = compute_isin_state(ISIN, [], [], [B(2026, 8, 31, 100)], [A("a", date(2026, 9, 10), qout="30")])
        self.assertEqual(s.position.qty, D(70))


if __name__ == "__main__":
    unittest.main()
