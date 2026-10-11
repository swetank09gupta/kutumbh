import unittest
from datetime import date
from decimal import Decimal as D

from app.ledger.store import holding_type_for, movement_key, trade_key

BASE = dict(isin="INE047A01021", symbol="GRASIM", ref="53252601", trade_date=date(2026, 10, 8),
            side="B", qty=D("5"), price=D("2890.00"), amount=D("14450.00"))


class NaturalKeys(unittest.TestCase):
    def test_same_trade_same_key_regardless_of_decimal_scale(self):
        a = trade_key("user", "contract_note", **BASE)
        b = trade_key("user", "contract_note", **{**BASE, "qty": D("5.000"), "price": D("2890.0")})
        self.assertEqual(a, b)

    def test_owner_is_part_of_the_key(self):
        self.assertNotEqual(trade_key("user", "contract_note", **BASE), trade_key("wife", "contract_note", **BASE))

    def test_order_level_trades_with_different_trade_numbers_stay_distinct(self):
        self.assertNotEqual(trade_key("user", "contract_note", **BASE),
                            trade_key("user", "contract_note", **{**BASE, "ref": "53252602"}))

    def test_tradebook_is_keyed_by_trade_id_alone(self):
        a = trade_key("user", "tradebook", **{**BASE, "ref": "T1"})
        b = trade_key("user", "tradebook", **{**BASE, "ref": "T1", "qty": D("9")})
        self.assertEqual(a, b)

    def test_sources_never_collide(self):
        keys = {trade_key("user", k, **BASE) for k in ("contract_note", "coin", "tradebook")}
        self.assertEqual(len(keys), 3)

    def test_movement_key_ignores_description_noise_but_not_quantities(self):
        d = date(2026, 8, 10)
        a = movement_key("user", "INE1", d, D("2"), D("0"), D("13"), "NSE Payout 1208160066954841 110000")
        b = movement_key("user", "INE1", d, D("2.000"), D("0.0"), D("13.000"), "NSE  Payout-1208160066954841 110000")
        c = movement_key("user", "INE1", d, D("3"), D("0"), D("13"), "NSE Payout 1208160066954841 110000")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class HoldingType(unittest.TestCase):
    def test_isin_prefix_decides_type(self):
        self.assertEqual(holding_type_for("INF109K016L0"), "mutual_fund")
        self.assertEqual(holding_type_for("IN0020220110"), "bonds")
        self.assertEqual(holding_type_for("INE047A01021"), "equity")
        self.assertIsNone(holding_type_for(None))


if __name__ == "__main__":
    unittest.main()
