"""Password candidates: formulas from the profile plus account-number passwords kept in the private config (synthetic numbers here)."""
import unittest
from unittest import mock

from app.ingestion.email_fetcher import _all_pdf_passwords

PROFILE = {"name": "Test User", "dob": "1990-02-03", "pan": "ABCDE1234F", "mobile": "9876543210",
           "union_bank_account_no": "123456789016026", "hdfc_loan_account_no": "702024935"}


class Candidates(unittest.TestCase):
    def pw(self, institution):
        from app.config import settings
        with mock.patch.object(type(settings), "get_owner_profile", lambda self, owner_id: dict(PROFILE)):
            return _all_pdf_passwords(institution, "user")

    def test_union_bank_gets_the_formula_and_the_account_number(self):
        p = self.pw("Union Bank")
        self.assertIn("TEST0302", p)                      # name4 + DDMM (the bank's rule from 2024)
        self.assertIn("123456789016026", p)               # the account number (the bank's rule in 2020-21)
        self.assertNotIn("702024935", p)

    def test_hdfc_gets_the_loan_account_number_for_its_loan_documents(self):
        p = self.pw("HDFC")
        self.assertIn("702024935", p)
        self.assertNotIn("123456789016026", p)

    def test_other_institutions_get_neither(self):
        p = self.pw("SBI")
        self.assertNotIn("123456789016026", p)
        self.assertNotIn("702024935", p)

    def test_no_duplicates(self):
        p = self.pw("Union Bank")
        self.assertEqual(len(p), len(set(p)))


class FromTheRealConfigFile(unittest.TestCase):
    """The mocked tests above would pass even if the settings accessor dropped the new keys, so read a real file."""
    def test_the_account_number_keys_survive_the_config_reader_and_reach_the_candidates(self):
        import os, tempfile
        from pathlib import Path
        from app.config import settings
        d = tempfile.mkdtemp()
        (Path(d) / "config").mkdir()
        (Path(d) / "config" / "accounts.yml").write_text(
            'owners:\n  - id: "user"\n    name: "Test User"\n    dob: "1990-02-03"\n    pan: "ABCDE1234F"\n    mobile: "9876543210"\n'
            '    union_bank_account_no: "123456789016026"\n    hdfc_loan_account_no: "702024935"\n')
        here = os.getcwd()
        os.chdir(d)
        try:
            prof = settings.get_owner_profile("user")
            self.assertEqual((prof["union_bank_account_no"], prof["hdfc_loan_account_no"]), ("123456789016026", "702024935"))
            self.assertIn("123456789016026", _all_pdf_passwords("Union Bank", "user"))
            self.assertIn("702024935", _all_pdf_passwords("HDFC", "user"))
        finally:
            os.chdir(here)


class Senders(unittest.TestCase):
    def test_union_bank_mail_from_its_2026_domain_is_recognised_and_searched_for(self):
        from app.ingestion.email_fetcher import KNOWN_SENDERS, _match_sender
        self.assertEqual(_match_sender("Union Bank of India <noreplyunionbank@ubi.bank.in >")["institution"], "Union Bank")
        self.assertIn("ubi.bank.in", KNOWN_SENDERS)          # the IMAP search is by domain: Gmail matches whole words only


if __name__ == "__main__":
    unittest.main()
