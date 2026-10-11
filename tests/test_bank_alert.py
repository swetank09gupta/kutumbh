import unittest
from datetime import date
from decimal import Decimal

from app.ingestion.parsers.bank_alert import parse_alert

HDFC = "alerts@hdfcbank.bank.in"
SBI = "noreply@alerts.sbi.bank.in"


class HdfcAlerts(unittest.TestCase):
    def test_upi_debit_has_full_payee_vpa_and_utr(self):
        a = parse_alert(HDFC, "HDFC BANK --> Dear Customer, Greetings from HDFC Bank! Rs.184.00 is debited from your "
                        "account ending 1632 towards VPA paytm.s1mqhoy@pty (BAL KISHOR) on 10-10-26. UPI "
                        "transaction reference no.: 131035014390. If you did not authorize")
        self.assertEqual((a.bank, a.kind, a.direction, a.amount, a.txn_date, a.account_last4),
                         ("HDFC", "upi", "debit", Decimal("184.00"), date(2026, 10, 10), "1632"))
        self.assertEqual((a.payee, a.vpa, a.ref), ("BAL KISHOR", "paytm.s1mqhoy@pty", "131035014390"))

    def test_upi_credit(self):
        a = parse_alert(HDFC, "Rs.600.00 has been successfully credited to your HDFC Bank account ending in 1632. "
                        "Transaction Details: a. Date: 04-10-26 b. Sender: Mahfuj Ali (VPA: 8057809419@nyes) "
                        "c. UPI Reference No.: 005561608170 Need Help?")
        self.assertEqual((a.direction, a.amount, a.payee, a.ref), ("credit", Decimal("600.00"), "Mahfuj Ali", "005561608170"))

    def test_ach_both_wordings_describe_one_event(self):
        a = parse_alert(HDFC, "Rs. INR 1,70,000.00 is deducted from your account ending XX1632 and added to ACH D- "
                        "HDFC BANK LTD-478914192 account on 05-OCT-2026. The available balance in your account is "
                        "Rs. INR 47,692.26. The balance")
        b = parse_alert(HDFC, "Rs.170000.00 has been debited from HDFC Bank Account Number XXXXXXXXXX1632 towards "
                        "HDFC BANK LTD/702024935 with UMRN HDFC2024018000002231 on 05-Oct-2026. Assuring")
        self.assertEqual((a.kind, a.amount, a.txn_date, a.balance_after), ("ach", Decimal("170000.00"), date(2026, 10, 5), Decimal("47692.26")))
        self.assertEqual((b.kind, b.amount, b.txn_date, b.account_last4), (a.kind, a.amount, a.txn_date, a.account_last4))

    def test_deposit(self):
        a = parse_alert(HDFC, "You have received a credit in your HDFC Bank account. Details of the transaction: "
                        "Amount received: INR 2,00,000.00 Account: XX1632 Date: 01-OCT-2026 Reference Details: NEFT "
                        "Cr-SBIN0004463-SWETANK GUPTA-Swetank HDFC-SBIN526274293555 Available Balance: INR 6,51,294.22 For")
        self.assertEqual((a.kind, a.direction, a.amount, a.balance_after), ("deposit", "credit", Decimal("200000.00"), Decimal("651294.22")))
        self.assertIn("SBIN526274293555", a.ref)

    def test_marketing_mail_is_not_an_alert(self):
        self.assertIsNone(parse_alert(HDFC, "Skip the Wait! Manage Your Home Loan Instantly"))


class SbiAlerts(unittest.TestCase):
    def test_transfer_credit_with_balance(self):
        a = parse_alert(SBI, "Your A/C XXXXX223316 Credited INR 1,50,000.00 on 28/08/26 -Deposit by transfer from "
                        "Mrs. AKANKSHAARYA. Avl Bal INR 20,34,456.88-SBI")
        self.assertEqual((a.bank, a.account_last4, a.direction, a.amount, a.txn_date, a.balance_after),
                         ("SBI", "3316", "credit", Decimal("150000.00"), date(2026, 8, 28), Decimal("2034456.88")))

    def test_transfer_debit_direction(self):
        a = parse_alert(SBI, "Your A/C XXXXX321476 Debited INR 753.00 on 02/06/26 -Transferred to Mr. SWETANKGUPTA. "
                        "Avl Balance INR 1,45,420.00-SBI")
        self.assertEqual((a.direction, a.amount, a.payee), ("debit", Decimal("753.00"), "Mr. SWETANKGUPTA"))

    def test_nach_and_upi_reversal(self):
        n = parse_alert(SBI, "Dear Customer, Your A/C XXXXX223316 has a debit by NACH of Rs 41,369.00 on 16/06/26. Avl Bal Rs 11,85,853.89.")
        u = parse_alert(SBI, "Your A/C XXXXX223316 has credit for UPI/REVERSAL/614221532643/2205 of Rs 360.00 on 18/06/26. Avl Bal Rs 11,86,213.89.")
        self.assertEqual((n.kind, n.direction, n.amount), ("nach", "debit", Decimal("41369.00")))
        self.assertEqual((u.kind, u.direction, u.payee), ("upi", "credit", "UPI/REVERSAL/614221532643/2205"))

    def test_neft_and_standing_instruction_carry_utr(self):
        n = parse_alert(SBI, "The details of the NEFT transaction originated by you are given below. A/c Debited: XX3316 "
                        "Date: 01/06/2026 UTR No.: SBIN526152155683 Beneficiary Name: Swetank HDFC Beneficiary A/c No.: "
                        "XX1632 Bank IFSC: HDFC0000280 Amount Remitted: INR 2,00,000.00")
        s = parse_alert(SBI, "Dear SWETANK GUPTA, SBI Ac X3316 debited for SI Fund Transfer of Rs.2,00,000.00 to Swetan "
                        "Ac X1632 on 01.10.26 Ref SBIN526274293555. Not done by you?")
        self.assertEqual((n.ref, n.amount, n.txn_date), ("SBIN526152155683", Decimal("200000.00"), date(2026, 6, 1)))
        self.assertEqual((s.ref, s.txn_date, s.account_last4), ("SBIN526274293555", date(2026, 10, 1), "3316"))

    def test_interest_certificate_and_tds_are_ignored(self):
        self.assertIsNone(parse_alert(SBI, "Dear Customer, TDS of INR 73.00 deducted for interest paid on A/C No XXXXX826925."))


if __name__ == "__main__":
    unittest.main()
