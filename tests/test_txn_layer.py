import unittest
from datetime import date

from app.txn.dedupe import Row, duplicate_ids
from app.txn.normalize import is_own_payee, payee_key, share_utr
from app.txn.pipeline import is_authoritative
from app.models.schemas import PayeeMemory
from app.txn.transfers import TRow, pair_transfers


class PayeeKey(unittest.TestCase):
    def test_hdfc_and_sbi_formats(self):
        self.assertEqual(payee_key("UPI-MEGHRAJ CHHETRI-Q43734314@YBL-YESB0YBLUPI-123"), "MEGHRAJCHHETRI")
        self.assertEqual(payee_key("UPI/DR/103814026744/NSE ZERO/HDFC/zerodha.ns/DFe"), "NSEZERO|ZERODHANS")

    def test_sbi_truncated_names_are_told_apart_by_the_handle(self):
        a = payee_key("UPI/DR/1/Bank Acc/UBIN/1717210100/Gay")        # numeric handle: name only
        b = payee_key("UPI/DR/1/DRAVIT G/CITI/dravit.inf/tes")
        c = payee_key("UPI/DR/1/DRAVIT G/YESB/dravitgupta@ybl/tes")
        self.assertEqual(a, "BANKACC")
        self.assertNotEqual(b, c)

    def test_spacing_differences_give_one_key(self):
        self.assertEqual(payee_key("UPI-MEGHRAJCHHETRI-Q43@YBL-YESB"), payee_key("UPI-MEGHRAJ CHHETRI-Q43@YBL-YESB"))

    def test_masked_or_bare_narrations_have_no_key(self):
        self.assertIsNone(payee_key("UPI-XXXXXXX5482-SBIN0004463-1234"))
        self.assertIsNone(payee_key("IN22606125428144"))
        self.assertIsNone(payee_key(None))

    def test_imps_and_neft(self):
        self.assertEqual(payee_key("IMPS-623008893516-INNOFINSOLUTIONSPRIVATELIMITED-xx"), "INNOFINSOLUTIONSPRIVATELIMITED")
        self.assertEqual(payee_key("NEFT CR-UTIB0001394-INNOFIN SOLUTIONS PRIVATE LI"), "INNOFINSOLUTIONSPRIVATELI")


class Utr(unittest.TestCase):
    def test_same_payment_printed_three_ways(self):
        self.assertTrue(share_utr(("121775804732", "UPI-NHLSHR-x"), ("SB0 00001217758047", "UPI-NHLSHR-NHLGURUGRAM")))
        self.assertTrue(share_utr(("X 0000121859759822", "REPAYMENTISPLTDACCOUNT-SWETANK"), ("121859759822", "UPI-LENSKART")))

    def test_different_payments_do_not_share(self):
        self.assertFalse(share_utr(("623008893516", ""), ("623020986296", "")))
        self.assertFalse(share_utr(("", "no digits"), ("", "none either")))


def R(i, ref, raw, cat="other", amount=1500, d=date(2026, 4, 18), acct="a", typ="debit"):
    return Row(i, acct, d, amount, typ, ref, raw, cat)


class Dedupe(unittest.TestCase):
    def test_same_utr_in_two_statements_keeps_the_informative_row(self):
        rows = [R("1", "121775804732", "UPI-NHLSHR-NHLGURUGRAM.HYPG@YESPAY-YESB0YESUPI-121775804732"),
                R("2", "SB0 00001217758047", "UPI-NHLSHR-NHLGURUGRAM.HYPG@YESPAY-YE")]
        self.assertEqual(duplicate_ids(rows), {"2": "1"})

    def test_two_payments_sharing_a_qr_code_or_a_date_are_not_duplicates(self):
        # Real cases (HDFC 1632): two ₹80 debits to one shop share its QR code; two ₹3.26 NEFT credits share the date inside their
        # references. The printed balances differ, so both are kept.
        shop = [Row("1", "a", date(2024, 10, 28), 80, "debit", "0000430213941994", "UPI-THESNACKSTORY-PAYTMQR2810050501011", "other", 8513.60),
                Row("2", "a", date(2024, 10, 28), 80, "debit", "9", "KLOOMJ2J45M@PAYTM-YESB0PTMUPI-430213941 94-UPI UPI-THESNACKSTORY-PAYTMQR2810050501011", "other", 8433.60)]
        neft = [Row("3", "a", date(2025, 11, 12), 3.26, "credit", "ICICN22025111220428003", "NEFTCR-ICIC0099999-LENDENCLUB", "other", 42596.87),
                Row("4", "a", date(2025, 11, 12), 3.26, "credit", "ICICN22025111221619398", "NEFTCR-ICIC0099999-LENDENCLUB", "other", 42600.13)]
        self.assertEqual(duplicate_ids(shop), {})
        self.assertEqual(duplicate_ids(neft), {})

    def test_the_same_payment_with_the_same_balance_in_two_layouts_is_still_one_row(self):
        rows = [Row("1", "a", date(2026, 4, 18), 1500, "debit", "121775804732", "UPI-NHLSHR-A", "other", 900.0),
                Row("2", "a", date(2026, 4, 18), 1500, "debit", "SB0 00001217758047", "UPI-NHLSHR-B", "other", 900.0)]
        self.assertEqual(len(duplicate_ids(rows)), 1)

    def test_a_real_category_beats_a_generic_one(self):
        rows = [R("t", "121859759822", "REPAYMENTISPLTDACCOUNT-SWETANK", cat="transfer", amount=2699),
                R("s", "X 0000121859759822", "UPI-LENSKART-LENSKARTOFFLINE@AXL", cat="shopping", amount=2699)]
        self.assertEqual(duplicate_ids(rows), {"t": "s"})

    def test_three_renderings_of_one_payment_collapse_to_one(self):
        rows = [R("1", "403412345678", "UPI-ROYALOAK-X", "shopping", 50000),
                R("2", "403412345678", "UPI-ROYALOAK-Y", "shopping", 50000),
                R("3", "000403412345678", "XXXXXXXXX3316-REQPAY", "other", 50000)]
        d = duplicate_ids(rows)
        self.assertEqual(len(d), 2)
        self.assertNotIn(list(set("123") - set(d))[0], d)

    def test_two_payments_same_day_same_amount_are_both_kept(self):
        rows = [R("1", "103814026744", "UPI/DR/103814026744/NSE ZERO", "investment", 10000),
                R("2", "103814029496", "UPI/DR/103814029496/NSE ZERO", "investment", 10000)]
        self.assertEqual(duplicate_ids(rows), {})

    def test_different_account_or_date_is_not_a_duplicate(self):
        a = R("1", "121775804732", "x")
        self.assertEqual(duplicate_ids([a, R("2", "121775804732", "x", acct="b")]), {})
        self.assertEqual(duplicate_ids([a, R("2", "121775804732", "x", d=date(2026, 4, 19))]), {})


def T(i, acct, typ, amount, d=date(2026, 9, 5), cat="other", src=""):
    return TRow(i, acct, d, amount, typ, cat, src)


class Transfers(unittest.TestCase):
    def test_unique_pair_across_accounts_is_found(self):
        self.assertEqual(pair_transfers([T("d", "hdfc", "debit", 100000, cat="shopping"),
                                         T("c", "sbi", "credit", 100000)]), [("d", "c")])

    def test_one_day_apart_still_pairs(self):
        self.assertEqual(len(pair_transfers([T("d", "hdfc", "debit", 5000, date(2026, 9, 5)),
                                             T("c", "sbi", "credit", 5000, date(2026, 9, 6))])), 1)

    def test_ambiguous_matches_are_left_alone(self):
        rows = [T("d", "hdfc", "debit", 5000), T("c1", "sbi", "credit", 5000), T("c2", "axis", "credit", 5000)]
        self.assertEqual(pair_transfers(rows), [])

    def test_small_amounts_salary_and_investments_are_never_paired(self):
        self.assertEqual(pair_transfers([T("d", "a", "debit", 500), T("c", "b", "credit", 500)]), [])
        self.assertEqual(pair_transfers([T("d", "a", "debit", 90000), T("c", "b", "credit", 90000, cat="income")]), [])
        self.assertEqual(pair_transfers([T("d", "a", "debit", 90000, cat="investment"), T("c", "b", "credit", 90000)]), [])

    def test_same_account_and_far_dates_do_not_pair(self):
        self.assertEqual(pair_transfers([T("d", "a", "debit", 5000), T("c", "a", "credit", 5000)]), [])
        self.assertEqual(pair_transfers([T("d", "a", "debit", 5000, date(2026, 9, 1)), T("c", "b", "credit", 5000, date(2026, 9, 9))]), [])

    def test_already_tagged_pair_is_not_reported(self):
        self.assertEqual(pair_transfers([T("d", "a", "debit", 5000, cat="transfer"), T("c", "b", "credit", 5000, cat="transfer")]), [])

    def test_user_labelled_rows_are_protected(self):
        self.assertEqual(pair_transfers([T("d", "a", "debit", 5000, src="user"), T("c", "b", "credit", 5000)]), [])


class OwnPayee(unittest.TestCase):
    NAMES = ["Swetank Gupta", "Akanksha Arya"]

    def test_own_upi_in_both_bank_formats(self):
        self.assertTrue(is_own_payee("UPI-SWETANK GUPTA-SWETANK.ENERGY@OKSBI-SBIN0004463-1", self.NAMES))
        self.assertTrue(is_own_payee("UPI/CR/107121131967/SWETANK /CITI/swetank.en/tes", self.NAMES))
        self.assertTrue(is_own_payee("UPI-AKANKSHA ARYA-AKANKSHA@YBL-UTIB0000000-1", self.NAMES))

    def test_a_stranger_with_the_same_first_name_is_not_own(self):
        self.assertFalse(is_own_payee("UPI/DR/1/SWETANK /HDFC/s.kumar99@ybl/pay", self.NAMES))
        self.assertFalse(is_own_payee("UPI-MEGHRAJ CHHETRI-Q43@YBL-YESB", self.NAMES))
        self.assertFalse(is_own_payee(None, self.NAMES))


class Authority(unittest.TestCase):
    def test_only_a_user_label_is_applied_automatically(self):
        self.assertTrue(is_authoritative(PayeeMemory(payee_key="X", category="food", source="user")))
        self.assertFalse(is_authoritative(PayeeMemory(payee_key="X", category="food", source="llm", confidence=0.99)))
        self.assertFalse(is_authoritative(PayeeMemory(payee_key="X", category="other", source="user")))
        self.assertFalse(is_authoritative(None))


class MerchantRules(unittest.TestCase):
    def cat(self, d):
        from app.etl.categorizer import categorise
        c = categorise(d)
        return c.category, c.subcategory

    def test_known_merchants_from_the_review_list(self):
        self.assertEqual(self.cat("UPI/DR/1/FIRSTCRY/HDFC/brainbees@/First")[0], "child_expenses")
        self.assertEqual(self.cat("UPI/DR/1/GROFERS /utib/grofersind/Pay")[0], "food")
        self.assertEqual(self.cat("UPI/DR/1/Central /SBIN/incometax./MOPS")[0], "taxes")
        self.assertEqual(self.cat("UPI/DR/1/Urban Co/YESB/paytm-urba/Pay")[0], "housing")

    def test_regular_emi_is_the_ach_mandate_and_other_loan_payments_are_prepayments(self):
        self.assertEqual(self.cat("ACHD-HDFCBANKLTD-456408135"), ("emi", "emi_regular"))
        self.assertEqual(self.cat("ACH D- HDFC BANK LTD-475037270"), ("emi", "emi_regular"))
        self.assertEqual(self.cat("DHDFQ3G1UQEHQZ/HDFC LTD"), ("emi", "loan_prepayment"))
        self.assertEqual(self.cat("XXXXXXXXX3316-PAYMENTTOWARDSLOAN"), ("emi", "loan_prepayment"))
        self.assertNotEqual(self.cat("ACHD-HDFCBANKLTD-452996445 UPI-MEGHRAJ"), ("emi", "emi_regular"))

    def test_a_person_stays_unclassified(self):
        self.assertEqual(self.cat("UPI/DR/1/DEEPIKA /SBIN/deepika.ju/pho")[0], "other")


class FamilyPayee(unittest.TestCase):
    """Real narrations (names as in the owner's private config): a parent's cheque deposits funded most of a year's loan prepayments and
    were counted as 'other income'; family money in or out is a transfer."""
    NAMES = ["Pradeep Kumar Gupta"]

    def test_cheque_deposit_and_rtgs_both_match(self):
        from app.txn.normalize import is_family_payee
        self.assertTrue(is_family_payee("CHQ DEP - CTS CLG1 - RAMPURUP: PRADEEP KUMAR GUPTA DAYA GUPTA :IDBI BANK", self.NAMES))
        self.assertTrue(is_family_payee("RTGS CR-IBKL0001049-PRADEEP KUMAR GUPTA-SWETANK GUPTA-IBKLR9202606060007719", self.NAMES))
        self.assertTrue(is_family_payee("CHQDEP-CTSCLG1-RAMPURUP:PRADEEPKUMARGUPTADAYAGUPTA", self.NAMES))

    def test_a_stranger_with_a_shared_name_part_does_not(self):
        from app.txn.normalize import is_family_payee
        self.assertFalse(is_family_payee("UPI-PRADEEP KUMAR-9999@OKAXIS", self.NAMES))
        self.assertFalse(is_family_payee("UPI-SWETANK GUPTA-SWETANK@OKHDFC", self.NAMES))
        self.assertFalse(is_family_payee("anything", []))
        self.assertFalse(is_family_payee(None, self.NAMES))
        self.assertFalse(is_family_payee("PRADEEP KUMAR GUPTA", ["Al"]))                  # a name too short to be a safe key is ignored


if __name__ == "__main__":
    unittest.main()
