import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from app.ingestion.parsers.zerodha_coin import ZerodhaCoinParser

PURCHASE = """<html><body>Coin ver 3.0 Hi Swetank (MJ9491), Here are your latest mutual fund updates.
Purchase confirmation Fund Amount HDFC ELSS TAX SAVER - DIRECT PLAN - GROWTH OPTION
Folio no.: 17556444/19, NAV: ₹1580.32 Stamp Duty: ₹2.5 ₹49997.5 31.638 units
Best, Team Zerodha</body></html>"""

REDEMPTION = """<html><body>Hi Swetank Gupta (MJ9491) , Your mutual fund redemption has been processed successfully.
The amount will be credited to your primary bank account as per the settlement cycle of the funds.
The redemption details are as below: Fund Settl. type* Units Nav Amount Exit Load STT
SBI Technology Opportunities Fund Direct Idcw Payout T2 311.3640 121.1478 Rs. 37720.67 Rs. 0.00 Rs. 0.3800
*T refers to the day the order was processed.</body></html>"""

SELL_ORDER = """<html><body>Hi Swetank Gupta (MJ9491) , Your sell order was processed successfully.
Order details: Fund Quantity SBI Technology Opportunities Fund - Direct Plan 311.3640</body></html>"""

PLACED = "<html><body>Hi Swetank (MJ9491), Your order was placed successfully. NAV will be as per settlement.</body></html>"


def parse(html, name):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / name
        p.write_text(html, encoding="utf-8")
        return ZerodhaCoinParser().parse(p).transactions


class CoinFormats(unittest.TestCase):
    def test_purchase_confirmation_is_a_buy(self):
        (t,) = parse(PURCHASE, "Tue,_04_Nov_coin_1.html")
        self.assertEqual((t.transaction_type, t.quantity, t.price, t.amount, t.kind),
                         ("debit", D("31.638"), D("1580.32"), D("49997.5"), "coin"))
        self.assertEqual(t.reference_number, "17556444/19")
        self.assertEqual(t.date, date(2025, 11, 4))

    def test_redemption_processed_is_a_sell_with_units_nav_amount(self):
        (t,) = parse(REDEMPTION, "Mon,_21_Aug_coin_1.html")
        self.assertEqual((t.transaction_type, t.quantity, t.price, t.amount),
                         ("credit", D("311.3640"), D("121.1478"), D("37720.67")))
        self.assertEqual(t.counterparty, "SBI Technology Opportunities Fund Direct Idcw Payout")
        self.assertEqual(t.date, date(2023, 8, 21))

    def test_sell_order_processed_is_ignored_so_the_sale_is_not_counted_twice(self):
        self.assertEqual(parse(SELL_ORDER, "Fri,_18_Aug_coin_1.html"), [])

    def test_order_placed_notices_are_ignored(self):
        self.assertEqual(parse(PLACED, "Fri,_14_Oct_coin_1.html"), [])


if __name__ == "__main__":
    unittest.main()
