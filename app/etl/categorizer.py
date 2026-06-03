"""
Rule-based transaction categoriser for Indian personal finance.
Applies regex patterns to description text to assign category + subcategory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Category:
    category:    str
    subcategory: str | None = None


# Ordered — first match wins
_RULES: list[tuple[re.Pattern, Category]] = [
    # ── ATM / Cash — checked FIRST so location names don't trigger income rules ─
    (re.compile(r"atm\s+wd|cash\s+withdrawal|atm\s+cash|atm\s+withdrawal|atw", re.I),
     Category("cash", None)),

    # ── Income ────────────────────────────────────────────────────────────────
    # Employer-specific NEFT/salary credits
    (re.compile(r"neft.*grey\s*orange|grey\s*orange.*neft"
                r"|greyorange|goi\s*sal", re.I),
     Category("income", "salary")),
    (re.compile(r"bharat\s*electronics|cemtex|cmp\s+bharat"
                r"|^bel$|bel\s+ch|bel\s+chq|by\s+bel\s+ch", re.I),
     Category("income", "salary")),
    (re.compile(r"salary|sal\s*credit|payroll|neft.*sal|sal.*neft|\bctc\b", re.I),
     Category("income", "salary")),
    (re.compile(r"dividend|div\s*pay|int(erest)?\s*(credit|paid|earned)", re.I),
     Category("income", "interest_dividend")),
    # SBI FD internal interest transfers (INT TRF FRM <acct> TO <acct>)
    (re.compile(r"int\s+trf\s+frm\s+\d+\s+to\s+\d+", re.I),
     Category("income", "interest_dividend")),
    (re.compile(r"refund|cashback|reversal", re.I),
     Category("income", "refund")),

    # ── Taxes (income tax, advance tax, TDS) ─────────────────────────────────
    (re.compile(r"central\s+board.*direct|cbdt|income\s+tax\s+dept|advance\s+tax"
                r"|tds\s+payment|challan.*tax|itns\s*280", re.I),
     Category("taxes", None)),

    # ── SBI Internet Banking (INB MBS) — most specific first ─────────────────
    # e-MOD = SBI auto-sweep multi-option deposit (FD from excess balance)
    (re.compile(r"inb\s*mbs.*e-?mod|e-?mod", re.I),
     Category("investment", "fd_rd")),
    # PPF via internet banking — "pof deposit" is SBI's encoded label for PPF deposits
    (re.compile(r"inb\s*mbs.*\bppf\b|\bppf\b.*inb\s*mbs|inb\s*mbs.*\bpof\s+dep", re.I),
     Category("investment", "ppf_nps")),
    # All other INB MBS are inter-account transfers (incl. ones labelled "for emi" —
    # those fund a joint EMI from another account; the actual EMI debit is separate)
    (re.compile(r"inb\s*mbs\b", re.I),
     Category("transfer", None)),

    # ── Own-account SBI→HDFC transfer (funds home loan EMI account) ──────────
    # "REPAYMENTISPLTDACCOUNT-SWETANK" appears on SBI/HDFC statements as a self-transfer
    (re.compile(r"repaymentispltdaccount", re.I),
     Category("transfer", None)),

    # ── EMI / Loan repayment ──────────────────────────────────────────────────
    (re.compile(r"\bemi\b|equated\s+monthly|loan\s+repayment|\bhl\s+emi\b|car\s*loan|personal\s*loan"
                r"|home\s*loan|housing\s*loan|mortgage"
                r"|^AA\d{7}$"           # Union Bank loan account ref pattern
                r"|^DIRECT\s+DR$"       # SBI direct debit for loan
                r"|ACHD.*HDFCBANK|ACH\s*D.*HDFC\s*BANK"  # HDFC home loan ACH mandate
                r"|LDESK.*HDFCBANK"     # HDFC loan desk payment
                r"|chdf\w+.*hdfc\s*ltd|hdfc\s*ltd.*chdf"  # HDFC loan prepayment NEFT format
                r"|[a-z0-9]+/hdfcltd|hdfcltd/[a-z0-9]",  # HDFC Ltd direct payment (e.g. YHDF.../HDFCLTD)
                re.I),
     Category("emi", "loan_prepayment")),

    # ── Zerodha tradebook: "BUY 15 × GRASIM @ 2848.00" / "BUY 313 × SILVER @ 324.25" ─
    # Must come BEFORE generic investment rules so stock buys don't fall to 'other'
    (re.compile(r"\b(?:buy|sell)\s+[\d,.]+\s*[×x]\s+\w+\s+@", re.I),
     Category("investment", "stocks")),

    # ── MF purchases that contain "ICICI PRU" in fund name — must come BEFORE insurance ─
    # Zerodha Coin trade confirmations: "BUY x units of ICICI PRUDENTIAL … FUND"
    (re.compile(r"\bbuy\b.*\bunits\s+of\b.*\bfund\b"
                r"|\bsell\b.*\bunits\s+of\b.*\bfund\b"
                r"|icici\s+prudential.*(?:fund|nav|units)"
                r"|(?:fund|nav|units).*icici\s+prudential", re.I),
     Category("investment", "mutual_fund")),

    # ── Insurance ─────────────────────────────────────────────────────────────
    # Term insurance via NACH mandate (e.g. "ACHDr NACH00000000022899 Policybazaar")
    (re.compile(r"policybazaar|policy\s*bazaar"
                r"|nach\d+.*(?:life|ins|polic|aia|pru|term)", re.I),
     Category("insurance", "term_insurance")),
    (re.compile(r"lic|life\s+ins|term\s+ins|health\s+ins|star\s+health|max\s+life"
                r"|hdfc\s+life|icici\s+pru|bajaj\s+allianz|tata\s+aia|sbi\s+life"
                r"|kotak\s+life|reliance\s+life|new\s+india|national\s+insurance", re.I),
     Category("insurance", None)),

    # ── Investments ───────────────────────────────────────────────────────────
    (re.compile(r"sip|systematic\s+inv|mutual\s+fund|mf\s+pur|coin|groww|kuvera"
                r"|paytm\s+money|zerodha|axis\s+mf|hdfc\s+mf|sbi\s+mf|icici\s+mf"
                r"|nippon|mirae|dsp|franklinfund|quant\s+mf|edelweiss\s+mf"
                r"|iccl\s*zer|icclzr"    # ICCL Zerodha clearing — MF SIP via NSE/BSE
                r"|indian\s+clea",       # ICCL/NSE clearing via ACH mandate
                re.I),
     Category("investment", "mutual_fund")),
    (re.compile(r"nse|bse|equity|share\s+(purchase|buy|sold)|stock\s+purchase"
                r"|demat|zerodha\s*trade|kite", re.I),
     Category("investment", "equity")),
    (re.compile(r"ppf\s+dep|ppf\s+contribution|public\s+provident|nps\s+contribution"
                r"|nps\s+tier|atal\s+pension"
                r"|mobft.*ppf|etxn.*ppf|ppf.*mobft|online\s+ppf", re.I),
     Category("investment", "ppf_nps")),
    (re.compile(r"fixed\s+dep|fd\s+open|fd\s+book|recurring\s+dep|rd\s+inst|e-?mod\b", re.I),
     Category("investment", "fd_rd")),

    # ── Child expenses ────────────────────────────────────────────────────────
    (re.compile(r"school\s+fee|tuition|coaching|daycare|creche|nursery|kindergarten"
                r"|playschool|kiddy|child\s+care|campus\s+fee|college\s+fee"
                r"|education\s+fee|sswings|nopaperf|nopaper\s*form", re.I),
     Category("child_expenses", "education_care")),

    # ── Food & dining ─────────────────────────────────────────────────────────
    (re.compile(r"swiggy|zomato|dominos|mcdonald|pizza|kfc|subway|burger\s+king"
                r"|restaurant|cafe|barbeque|dineout|eazydiner", re.I),
     Category("food", "dining_out")),
    (re.compile(r"bigbasket|big\s+basket|amazon\s+fresh|dmart|d-mart|reliance\s+smart"
                r"|more\s+supermarket|grocery|zepto|blinkit|dunzo|swiggy\s+instamart"
                r"|nature[''s]*\s+basket|licious|fresh\s+to\s+home"
                r"|\bsafal\b|haldiram|bikaner\s*misthan|bikaner\s*sweets"
                r"|doonbox",             # local/regional grocery & food delivery
                re.I),
     Category("food", "groceries")),

    # ── Transport ─────────────────────────────────────────────────────────────
    (re.compile(r"uber|ola\s+cab|rapido|auto|cab|taxi|metro|dmrc|nmmc|bmtc"
                r"|irctc|railway|flight|indigo|air\s+india|spicejet|vistara"
                r"|makemytrip|goibibo|booking\.com|yatra|cleartrip|redbus", re.I),
     Category("transport", "travel_commute")),
    (re.compile(r"petrol|diesel|hp\s+fuel|bharat\s+petroleum|indian\s+oil|shell"
                r"|fuel\s+station|iocl|bpcl|hpcl", re.I),
     Category("transport", "fuel")),
    (re.compile(r"fastag|toll|nhpc|nhai", re.I),
     Category("transport", "toll")),

    # ── Utilities ─────────────────────────────────────────────────────────────
    (re.compile(r"electricity|bses|tata\s+power|adani\s+elec|msedcl|bescom"
                r"|kseb|tneb|wbsedcl|cesc\s+ltd", re.I),
     Category("utilities", "electricity")),
    (re.compile(r"water\s+bill|jal\s+board|bwssb|mcgm\s+water", re.I),
     Category("utilities", "water")),
    (re.compile(r"gas\s+bill|piped\s+gas|mahanagar\s+gas|indraprastha\s+gas|igl|mgl"
                r"|gujarat\s+gas|adani\s+gas"
                r"|\bindane\b|krishna\s*indane|hp\s*gas\b|bharat\s*gas|ujjwala", re.I),
     Category("utilities", "gas")),
    (re.compile(r"broadband|internet|jio\s*fiber|airtel\s*fiber|act\s*fibernet"
                r"|tata\s*play|hathway|excitel", re.I),
     Category("utilities", "internet")),
    (re.compile(r"mobile\s*recharge|prepaid|postpaid|jio|airtel|vi\s+|vodafone\s+"
                r"|bsnl|idea\s+cellular", re.I),
     Category("utilities", "mobile")),
    (re.compile(r"dish\s*tv|tata\s*sky|tata\s*play\s*recharge|d2h|sun\s*direct"
                r"|airtel\s*digital\s*tv|cable\s*tv", re.I),
     Category("utilities", "cable_tv")),

    # ── Housing / Rent ────────────────────────────────────────────────────────
    (re.compile(r"rent\s+pay|house\s+rent|pg\s+rent|rental|maintenance\s+charge"
                r"|society\s+maintenance|housing\s+society"
                r"|dlfutil|dlf.*util|dlf.*maintenance|dlf.*service", re.I),
     Category("housing", "rent_maintenance")),

    # ── Healthcare ────────────────────────────────────────────────────────────
    (re.compile(r"pharmacy|medplus|apollo\s+pharmacy|1mg|pharmeasy|netmeds"
                r"|hospital|clinic|doctor|consultation|diagnostics|apollo"
                r"|fortis|max\s+hospital|aiims|medanta|narayana"
                r"|medicos|medicose|medico\b|medcare|medspa", re.I),
     Category("medical", None)),

    # ── Shopping ─────────────────────────────────────────────────────────────
    (re.compile(r"amazon|flipkart|myntra|ajio|nykaa|meesho|snapdeal|tata\s*cliq"
                r"|reliance\s*digital|croma|vijay\s*sales|ikea"
                r"|caratlane|carat\s*lane|tanishq|kalyan\s*jewel|malabar\s*gold"
                r"|royaloak|royal\s*oak|pepperfry|urban\s*ladder|home\s*centre"
                r"|lenskart", re.I),
     Category("shopping", "online")),

    # ── Entertainment / Subscriptions ────────────────────────────────────────
    (re.compile(r"netflix|amazon\s*prime|hotstar|disney|sony\s*liv|zee5|voot"
                r"|spotify|apple\s*music|youtube\s*premium|jiocinema"
                r"|google\s*one|googleindia|google\s*india|google\s*play", re.I),
     Category("entertainment", "subscriptions")),
    (re.compile(r"smaaash|fun\s*zone|bowling|escape\s*room|laser\s*tag|trampolin"
                r"|adventure\s*park|amusement", re.I),
     Category("entertainment", "recreation")),

    # ── Healthcare (extended) ────────────────────────────────────────────────
    (re.compile(r"apollo\s*cradle|motherhood\s*hosp|cloudnine|rainbow\s*hosp"
                r"|max\s*super|aiims\s*delhi|moolchand", re.I),
     Category("medical", None)),

    # ── Housing / Maintenance & Renovation ───────────────────────────────────
    (re.compile(r"tkelevator|tk\s*eleva.*lift|lift\s*maint|elevator\s*service"
                r"|building\s*maint|society\s*charge", re.I),
     Category("housing", "maintenance")),
    (re.compile(r"mohd\s*fa\b|wood.*furn|furniture.*wood|home.*renov|renov.*home"
                r"|interior.*design|carpenter|painter\b|plumber\b|electrician\b", re.I),
     Category("housing", "renovation")),

    # ── Family support — money sent to parents; counts as monthly expenditure ──
    # "IMPS/…/IBKL-xx752-papa idb/…" = recurring transfer to papa's IDBI account
    (re.compile(r"ibkl.*papa|papa.*idb", re.I),
     Category("family_support", None)),

    # ── Transfers (should be mostly excluded from expense analysis) ───────────
    # Own-account transfers: wife's account (AKANKSHA ARYA), self UPI IDs
    (re.compile(r"akanksha.*arya|arya.*akanksha", re.I),
     Category("transfer", None)),
    # ICICI credit card bill payment (ICICN + numeric ref) or INB to ICICI
    (re.compile(r"^icicn\d+|inb\s+icici\s+bank", re.I),
     Category("transfer", None)),
    (re.compile(r"imps|neft|rtgs|upi\s+transfer|self\s+transfer|transfer\s+to"
                r"|transfer\s+from|trf\s+to|trf\s+from|inter\s+bank"
                r"|ac\s+xfr.*sol|sol.*to\s+\d|swetank\s+gupta"
                r"|swetank.*sbin.*swetank\.en|swetank\.energy"
                r"|sweep\s+tfr|sweep\s+trf|sweep\s+from"  # SBI auto-sweep
                r"|lendenclub",                            # LendenClub P2P repayments
                re.I),
     Category("transfer", None)),

]


def categorise(description: str) -> Category:
    """Return Category for a transaction description. Falls back to 'other'."""
    for pattern, cat in _RULES:
        if pattern.search(description):
            return cat
    return Category("other", None)
