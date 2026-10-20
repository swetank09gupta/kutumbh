import unittest
from datetime import date
from decimal import Decimal

from app.ingestion.parsers.bank_alert import parse_alert, parse_tds

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

    def test_a_tds_notice_is_not_a_transaction_but_is_read_as_tax_on_a_deposit(self):
        text = "Dear Customer, TDS of INR 1,277.00 deducted for interest paid on A/C No XXXXX826925. Please submit 15G/H"
        self.assertIsNone(parse_alert(SBI, text))
        self.assertEqual(parse_tds(SBI, text), ("6925", Decimal("1277.00")))
        self.assertIsNone(parse_tds(HDFC, text))


AXIS = "Axis Bank Alerts <alerts@axisbank.com>"


class AxisAlerts(unittest.TestCase):
    """Real wordings (May 2025, names removed). Axis prints no balance; the reference is the UPI/NEFT id."""
    def test_summary_wording_debit_upi(self):
        a = parse_alert(AXIS, "02-05-2025 Dear Customer, Here's the summary of your transaction: Amount Debited: INR 1318.00 "
                        "Account Number: XX3856 Date & Time: 02-05-25, 00:15:14 IST Transaction Info: UPI/P2M/548863353225/FLIPKART PAYMENTS "
                        "If this transaction was not initiated by you: To block UPI: SMS BLOCKUPI to +91 from your registered mobile number.")
        self.assertEqual((a.bank, a.kind, a.direction, a.amount, a.txn_date, a.account_last4, a.ref, a.payee, a.balance_after),
                         ("AXIS", "upi", "debit", Decimal("1318.00"), date(2025, 5, 2), "3856", "548863353225", "FLIPKART PAYMENTS", None))

    def test_summary_wording_credit(self):
        a = parse_alert(AXIS, "03-05-2025 Dear , Here's the summary of your transaction: Amount Credited: INR 661.00 Account Number: XX3856 "
                        "Date & Time: 03-05-25, 22:30:32 IST Transaction Info: UPI/P2A/100486302551/FLIPKART /HDFC BANK Feel free to connect with us")
        self.assertEqual((a.direction, a.amount, a.ref), ("credit", Decimal("661.00"), "100486302551"))

    def test_older_wording_neft(self):
        a = parse_alert(AXIS, "02-05-2025 Dear Customer, Thank you for banking with us. We wish to inform you that your A/c no. XX3856 has been "
                        "debited with INR 5000.00 on 02-05-2025 13:05:03 IST by NEFT/MB/AXOMB12253386885/A. To check your available balance")
        self.assertEqual((a.kind, a.direction, a.amount, a.txn_date, a.ref), ("neft", "debit", Decimal("5000.00"), date(2025, 5, 2), "AXOMB12253386885"))

    def test_logins_and_payment_advices_are_not_transactions(self):
        self.assertIsNone(parse_alert(AXIS, "Dear Customer, your attempt to log into Axis Mobile has failed."))
        self.assertIsNone(parse_alert("Straight2axis@axis.bank.in", "Payment Advice Amount Debited: INR 100.00 Account Number: XX1234 Date & Time: 02-05-25, 00:15:14 IST Transaction Info: UPI/P2M/548863353225/X"))


class SbiOlderDomainAlerts(unittest.TestCase):
    """Oct 2024 wordings from alerts.sbi.co.in (the account is the same one the statements call 8660)."""
    OLD = "SBI <donotreply.sbiatm@alerts.sbi.co.in>"

    def test_atm_cash_withdrawal(self):
        a = parse_alert(self.OLD, "TranSaction alert Email Document --> Transaction Alert Message Oct 02, 2024, 12:59 Dear Valued SBI Debit Card Holder, "
                        "The below transaction has been done using your SBI debit card. Description Value Bank AXS ATM ID DERH4908 Date & Time Oct 02, 2024, 12:59 "
                        "Transaction Number 427612031045 Amount (INR) 10000.00 Last 4 Digit of Account X3316 Transaction Type CASH WITHDRAWAL "
                        "Channel ATM Available Balance (INR) 231040.46 City GURGAON If not done by you")
        self.assertEqual((a.bank, a.kind, a.direction, a.amount, a.txn_date, a.account_last4, a.ref, a.balance_after),
                         ("SBI", "atm", "debit", Decimal("10000.00"), date(2024, 10, 2), "3316", "427612031045", Decimal("231040.46")))

    def test_a_card_purchase_names_the_card_not_the_account_and_is_skipped(self):
        self.assertIsNone(parse_alert(self.OLD, "Date & Time Oct 20, 2024, 11:54 Transaction Number 429306558542 Amount (INR) 14939.00 "
                                      "Last 4 Digit of Card X8571 Transaction Type PURCHASE Channel POS / ECOM"))

    def test_yono_neft_takes_the_day_it_was_sent_and_has_no_ref(self):
        text = ("Dear Customer, Thank you for banking with State Bank of India. NEFT transfer to XXk HDFC for Rs.50,000.00 from A/C no. XXXX3316 "
                "has been completed successfully.TID: UR39352518 . Download YONO at www.yonosbi.com.")
        a = parse_alert("SBI <donotreply.sbimb@alerts.sbi.co.in>", text, date(2024, 10, 21))
        self.assertEqual((a.kind, a.direction, a.amount, a.txn_date, a.account_last4, a.ref), ("neft", "debit", Decimal("50000.00"), date(2024, 10, 21), "3316", None))
        self.assertIsNone(parse_alert("SBI <donotreply.sbimb@alerts.sbi.co.in>", text))           # no body date and no sent date: nothing to record


if __name__ == "__main__":
    unittest.main()
