import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.loan_letter import parse_letter

RECEIPT = ("H:ACCTS:PREPLET/17304030 Date :01-SEP-2026 MR GUPTA SWETANK Dear Sir/Madam, Ref : Loan Account No : 702024935 "
           "Type : RESIDENT HOME LOAN-VARIABLE RATE-MONTHLY REST We are in receipt of Rs. 900000.00 towards Part prepayment "
           "of your loan on 01/SEP/2026 Pay Mode Details Ref No Amount(Rs.) WEBRCT HDFC BANK LTD , 33659834 900000.00 "
           "The details are as follows : Amount (Rs.) PRINCIPAL PREPAYMENT : 900000.00 Total : 900000.00 Kindly note that "
           "the prepayment is being effected subject to the realisation of the cheque/demand draft. The principal amount "
           "outstanding against your loan after a principal prepayment of Rs.900000 as on 01-SEP-2026 will be Rs.11222991 , "
           "which would be repayable as under : REVISED EMI (Rs.) : 170000.00 @7.1 NUMBER OF EMIs : 84 DATE OF COMMENCEMENT")
ENQUIRY = RECEIPT.replace("We are in receipt of Rs. 900000.00 towards Part prepayment", "We refer to your enquiry on/dated expressing your intention to partly prepay")


class LoanLetter(unittest.TestCase):
    def test_receipt_gives_the_lenders_outstanding_principal(self):
        L = parse_letter(RECEIPT)
        self.assertEqual((L.loan_no, L.as_on, L.prepaid, L.principal_after), ("702024935", date(2026, 9, 1), D("900000"), D("11222991")))
        self.assertEqual((L.emi, L.rate, L.emis_left), (D("170000.00"), D("7.1"), 84))

    def test_enquiry_letters_are_quotes_not_events(self):
        self.assertIsNone(parse_letter(ENQUIRY))

    def test_monthly_rest_roll_forward_reproduces_the_lenders_next_figure(self):
        # 1-Sep after prepayment 11,222,991; the Sep EMI (170,000 @7.1%) -> the lender's 1-Oct figure before prepayment
        principal = D("11222991") - (D("170000") - D("11222991") * D("7.1") / 1200)
        self.assertAlmostEqual(float(principal), 10619394 + 500000, delta=5)


if __name__ == "__main__":
    unittest.main()
