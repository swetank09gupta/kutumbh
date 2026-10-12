"""HDFC home-loan tax documents: the two layouts, and that nothing else is mistaken for them (synthetic numbers)."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.loan_certificate import parse_certificate

FINAL = """TYPE : 571- RESIDENT HOME LOAN-VARIABLE RATE-MONTHLY RES (M)
Certificate for interest - 01/04/2024 - 31/03/2025
Date : 25/04/2025 TO WHOMSOEVER IT MAY CONCERN
This is to certify that MR TEST USER (Loan account number 123456789) has/have been granted a HOUSING LOAN
of Rs. 2,00,00,000 @7.85% per annum in respect of the following property :
The total amount of EMIs payable from 01/04/2024 to 31/03/2025 is Rs. 18,20,000.00. The break-up of the amount into Principal and
Interest is given below :
PRINCIPAL COMPONENT Rs. 2,33,000.00
INTEREST COMPONENT Rs. 15,86,000.00
"""
FINAL_WITH_PRE_EMI = FINAL.replace("INTEREST COMPONENT Rs. 15,86,000.00", "INTEREST COMPONENT Rs. 2,78,247.00\nPRE-EMI INTEREST Rs. 77,781.00")
PROV = """TYPE : 571- RESIDENT HOME LOAN-VARIABLE RATE-MONTHLY RES
PROVISIONAL STATEMENT for claiming deduction under Sections 24(b) & 80C
of the INCOME-TAX ACT,1961 for the period 01/04/2025 to 31/03/2026
Date : 15/05/2025 TO WHOMSOEVER IT MAY CONCERN
of Rs. 2,00,00,000 @7.85% per annum* in respect of the following property :
The total amount of EMIs payable from 01/04/2025 to 31/03/2026 is Rs. 18,19,944.00. The break-up of the amount into Principal and
Interest is given below :
PRINCIPAL COMPONENT Rs. 4,19,179.00
INTEREST COMPONENT Rs. 14,00,765.00
2.Interest and Principal figures are subject to change in case of prepayment/s and/or change in repayment schedule.
"""


class Layouts(unittest.TestCase):
    def test_a_final_certificate(self):
        c = parse_certificate(FINAL)
        self.assertEqual((c.kind, c.fy, c.period_from, c.period_to, c.doc_date), ("final", "2024-25", date(2024, 4, 1), date(2025, 3, 31), date(2025, 4, 25)))
        self.assertEqual((c.principal, c.interest, c.pre_emi_interest, c.emi_total, c.rate), (D("233000.00"), D("1586000.00"), D(0), D("1820000.00"), D("7.85")))

    def test_pre_emi_interest_is_read_when_printed(self):
        c = parse_certificate(FINAL_WITH_PRE_EMI)
        self.assertEqual((c.interest, c.pre_emi_interest), (D("278247.00"), D("77781.00")))

    def test_a_provisional_statement(self):
        c = parse_certificate(PROV)
        self.assertEqual((c.kind, c.fy, c.principal, c.interest), ("provisional", "2025-26", D("419179.00"), D("1400765.00")))

    def test_other_documents_are_not_certificates(self):
        self.assertIsNone(parse_certificate("TDS certificate Form 16A Quarter 4 of 2023-24"))
        self.assertIsNone(parse_certificate("Certificate for interest - 01/04/2024 - 31/03/2025 (no amounts printed)"))
        self.assertIsNone(parse_certificate(""))


if __name__ == "__main__":
    unittest.main()
