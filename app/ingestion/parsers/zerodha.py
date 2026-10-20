"""
Zerodha Kite & Coin parser.

Handles two CSV types sent by Zerodha:
1. Holdings export  — Instrument | Qty | Avg. cost | LTP | Cur. val | P&L | Net chg. | Day chg.
2. Tradebook export — symbol | ISIN | trade_date | exchange | segment | series |
                      trade_type | auction | quantity | price | trade_id | order_id | …

And one PDF type:
3. Monthly Demat Transaction with Holding — Holdings section + Transactions section.
   Subject: "Monthly Demat Transaction with Holding"
   Password: PAN uppercase (or no password for some months).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd
from app.ingestion.parsers.base import (
    BaseParser, ParsedHolding, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)

DATE_FMTS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d-%b-%y"]

_ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')


class ZerodhaParser(BaseParser):
    _PARSER_VERSION = "+date2"      # contract-note trade date read year-first; only Zerodha files are re-read
    institution  = "Zerodha"
    account_type = "stocks"

    def parse(self, path: Path) -> ParseResult:
        if path.suffix.lower() == ".pdf":
            return self._parse_pdf(path)
        if path.suffix.lower() == ".xlsx":
            return self._parse_xlsx(path)
        return self._parse_csv(path)

    def _parse_xlsx(self, path: Path) -> ParseResult:
        """Console tradebook export: a few preamble rows ("Client ID", "Tradebook for Equity from … to …"), then the header
        row (Symbol, ISIN, Trade Date, …, Trade Type, Quantity, Price, Trade ID, …), then one row per executed order."""
        from app.ingestion.xlsx_reader import read_sheets
        try:
            sheets = read_sheets(path)
        except Exception as exc:
            logger.error("Zerodha xlsx read error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)
        for rows in sheets.values():
            head = next((i for i, r in enumerate(rows) if {"symbol", "trade date"} <= {str(c or "").strip().lower() for c in r}), None)
            if head is None:
                continue
            cols = [str(c or "").strip() for c in rows[head]]
            body = [(r + [None] * len(cols))[:len(cols)] for r in rows[head + 1:] if any(c not in (None, "") for c in r)]
            return self._parse_tradebook(pd.DataFrame(body, columns=cols), str(path))
        logger.warning("Zerodha xlsx %s: no tradebook header found", path.name)
        return ParseResult(institution=self.institution, account_type=self.account_type)

    def _parse_csv(self, path: Path) -> ParseResult:
        try:
            df = pd.read_csv(path, on_bad_lines="skip")
        except Exception as exc:
            logger.error("Zerodha CSV read error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)

        cols = [c.strip().lower() for c in df.columns]

        if "instrument" in cols and "qty" in cols:
            return self._parse_holdings(df, str(path))
        if "trade_date" in cols or "trade date" in " ".join(cols):
            return self._parse_tradebook(df, str(path))

        logger.warning("Zerodha CSV: unrecognised columns %s", df.columns.tolist())
        return ParseResult(institution=self.institution, account_type=self.account_type)

    # ── Holdings CSV ─────────────────────────────────────────────────────────
    def _parse_holdings(self, df: pd.DataFrame, source: str) -> ParseResult:
        df.columns = [c.strip() for c in df.columns]
        holdings: list[ParsedHolding] = []

        for _, row in df.iterrows():
            name = str(row.get("Instrument", row.get("instrument", ""))).strip()
            if not name or name.lower() == "instrument":
                continue

            units    = self._to_decimal(row.get("Qty.", row.get("Qty", row.get("qty", ""))))
            avg_cost = self._to_decimal(row.get("Avg. cost", row.get("Avg cost", "")))
            ltp      = self._to_decimal(row.get("LTP", row.get("ltp", "")))
            cur_val  = self._to_decimal(row.get("Cur. val", row.get("cur_val", "")))
            pnl      = self._to_decimal(row.get("P&L", row.get("p&l", "")))

            if cur_val is None:
                continue

            invested = (units * avg_cost) if units and avg_cost else None
            pnl_pct  = None
            if invested and invested != 0 and pnl is not None:
                pnl_pct = (pnl / invested) * Decimal("100")

            holdings.append(ParsedHolding(
                instrument_name=name,
                symbol=name,
                units=units or Decimal(0),
                avg_cost_nav=avg_cost,
                current_nav=ltp,
                current_value=cur_val,
                invested_value=invested,
                gain_loss=pnl,
                gain_loss_pct=pnl_pct,
                holding_type="equity",
            ))

        return ParseResult(
            institution=self.institution, account_type="stocks",
            holdings=holdings, source_file=source,
            complete_holdings_snapshot=bool(holdings),
        )

    # ── Tradebook CSV ─────────────────────────────────────────────────────────
    def _parse_tradebook(self, df: pd.DataFrame, source: str) -> ParseResult:
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        transactions: list[ParsedTransaction] = []

        for _, row in df.iterrows():
            date_val = self._parse_date(str(row.get("trade_date", "")), DATE_FMTS)
            if date_val is None:
                continue

            qty   = self._to_decimal(row.get("quantity"))
            price = self._to_decimal(row.get("price"))
            if qty is None or price is None:
                continue

            amount     = qty * price
            trade_type = str(row.get("trade_type", "buy")).lower()
            txn_type   = "debit" if trade_type == "buy" else "credit"
            symbol     = str(row.get("symbol", "")).strip()
            isin       = str(row.get("isin", "")).strip()
            trade_id   = str(row.get("trade_id", "")).strip()

            desc = f"{trade_type.upper()} {qty} × {symbol} @ {price}"

            transactions.append(ParsedTransaction(
                date=date_val, amount=amount, transaction_type=txn_type,
                description=desc, raw_description=desc,
                reference_number=trade_id or None,
                counterparty=symbol,
                isin=isin or None,
                quantity=qty, price=price, kind="tradebook",
            ))

        return ParseResult(
            institution=self.institution, account_type="stocks",
            transactions=transactions, source_file=source,
        )

    # ── PDF — Monthly Demat Transaction with Holding ─────────────────────────
    def _parse_pdf(self, path: Path) -> ParseResult:
        holdings:     list[ParsedHolding]     = []
        transactions: list[ParsedTransaction] = []

        parse_warnings: list[str] = []
        statement_date = None

        with self._open_pdf(path) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            # Weekly settlement/cash ledger statements — not holdings files, skip entirely.
            # "Statement of Accounts of Securities" = demat settlement
            # "Statement of Accounts of Funds" = cash ledger
            # "Daily Margin Statement" = margin available/required per segment; its "Sub Total" row once became a holding.
            if ("Statement of Accounts of Securities" in full_text
                    or "Statement of Accounts of Funds" in full_text
                    or "Daily Margin Statement" in full_text):
                logger.debug("Skipping weekly settlement statement: %s", path.name)
                return ParseResult(institution=self.institution, account_type=self.account_type,
                                   source_file=str(path))

            is_contract_note = bool(
                re.search(r'CONTRACT\s+(NOTE|CUM\s+BILL)', full_text, re.I)
                or re.search(r'TRADE\s+CONFIRMATION\s+NOTE', full_text, re.I)
            )

            is_monthly = (
                "Monthly Demat" in full_text
                or "TRANSACTION WITH HOLDING" in full_text
                or ("Holding" in full_text and "Transaction" in full_text and "ISIN" in full_text)
            )

            if is_contract_note:
                transactions = self._parse_contract_note(pdf, path)
            elif is_monthly:
                holdings, transactions, statement_date = self._parse_monthly_demat(pdf)
            else:
                holdings = self._parse_generic_pdf(pdf)

        # 2021 layout: "HOLDING AS ON dd/mm/yyyy" with whitespace-collapsed columns. Parse it
        # deterministically — the LLM fallback once read the ₹ value column as units.
        if not holdings and not is_contract_note:
            holdings = self._parse_old_layout_holdings(full_text)

        # If the PDF contains a holdings section but regex extracted nothing, try LLM.
        has_holdings_section = bool(
            re.search(r'Holdings\s+as\s+on', full_text, re.I)
            or re.search(r'ISIN\s+Code', full_text, re.I)
        )
        if not holdings and has_holdings_section and not is_contract_note:
            msg = (
                f"Zerodha PDF: 0 holdings parsed despite holdings section in text "
                f"({len(full_text)} chars). Falling back to LLM extraction."
            )
            logger.warning(msg)
            parse_warnings.append(msg)
            for rec in self._llm_extract("equity_holdings", full_text):
                try:
                    holdings.append(ParsedHolding(
                        instrument_name=str(rec.get("symbol") or rec.get("isin", "unknown")),
                        symbol=rec.get("symbol"),
                        isin=rec.get("isin"),
                        units=Decimal(str(rec["quantity"])),
                        current_value=Decimal(str(rec["current_value"])),
                        avg_cost_nav=Decimal(str(rec["avg_cost"])) if rec.get("avg_cost") else None,
                        current_nav=Decimal(str(rec["current_price"])) if rec.get("current_price") else None,
                        holding_type="mutual_fund" if str(rec.get("isin", "")).startswith("INF")
                                     else "equity",
                    ))
                except Exception as exc:
                    logger.warning("Zerodha LLM holding conversion failed: %s — %s", rec, exc)

        # Older layouts print "HOLDING AS ON 30/09/2021" (no ISO date). Without a statement
        # date the snapshot would be filed under today's date and masquerade as the current
        # position. Runs after the LLM fallback so holdings extracted that way are dated too.
        if statement_date is None and holdings:
            dm = re.search(r'Holdings?\s+as\s+on\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})',
                           full_text, re.I)
            if dm:
                statement_date = self._parse_date(dm.group(1), DATE_FMTS)

        return ParseResult(
            institution=self.institution, account_type="stocks",
            holdings=holdings, transactions=transactions,
            source_file=str(path),
            parse_warnings=parse_warnings,
            complete_holdings_snapshot=is_monthly and bool(holdings),
            statement_date=statement_date,
        )

    def _parse_monthly_demat(self, pdf) -> tuple[list[ParsedHolding], list[ParsedTransaction], Optional[date]]:
        """
        Monthly demat PDFs store all data as plain text, not tables.
        Transaction section: ISIN/Symbol header lines + dated transaction lines.
        Holdings section: starts after "Holdings as on YYYY-MM-DD:" marker.
        """
        holdings:     list[ParsedHolding]     = []
        transactions: list[ParsedTransaction] = []
        as_on: Optional[date] = None   # "Holdings as on" date = the statement's true date

        ISIN_HDR_RE  = re.compile(r'ISIN:\s*([A-Z]{2}[A-Z0-9]{9}[0-9])\s+Symbol:\s*(\S+)', re.I)
        # Last 3 decimal numbers = buy_qty, sell_qty, balance
        TXN_LINE_RE  = re.compile(
            r'^(\d{4}-\d{2}-\d{2})\s+(.+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s*$'
        )
        HOLDINGS_RE  = re.compile(r'Holdings as on (\d{4}-\d{2}-\d{2})', re.I)
        OPENING_RE   = re.compile(r'^Opening\s+balance\s*:?\s*([\d,]+(?:\.\d+)?)', re.I)
        # ISIN + company name (non-greedy) + 9 decimal numbers.
        # Requires explicit decimal point in each number so that integer-like
        # tokens in bond names (e.g. "GOI SGB 30898 2029") are not parsed
        # as data columns.
        HOLDING_RE   = re.compile(
            r'^([A-Z]{2}[A-Z0-9]{9}[0-9])\s+'
            r'(.+?)\s+'
            r'(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+'
            r'(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)'
        )
        SKIP_HDR_RE  = re.compile(
            r'^(ISIN\s+Code|Company\s+Name|Curr\.|Free\s+Bal|Pldg|Earmark|'
            r'Date\s+Transaction|Buy/Cr|Statement\s+of|Page\s+\d)', re.I
        )

        all_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        current_isin   = None
        current_symbol = None
        line_seq       = 0
        prev_balance: Optional[Decimal] = None   # running balance before the next line of this ISIN
        in_holdings    = False
        holdings_lines: list[str] = []

        for line in all_text.splitlines():
            line = line.strip()
            if not line:
                continue

            hm = HOLDINGS_RE.search(line)
            if hm:
                in_holdings  = True
                current_isin = None
                as_on = self._parse_date(hm.group(1), DATE_FMTS)
                continue

            if in_holdings:
                holdings_lines.append(line)
                continue

            im = ISIN_HDR_RE.search(line)
            if im:
                current_isin   = im.group(1)
                current_symbol = im.group(2)
                prev_balance   = None
                continue

            if current_isin:
                om = OPENING_RE.match(line)
                if om:
                    prev_balance = self._to_decimal(om.group(1))
                    continue
                tm = TXN_LINE_RE.match(line)
                if tm:
                    date_val = self._parse_date(tm.group(1), DATE_FMTS)
                    if date_val is None:
                        continue
                    buy_qty  = self._to_decimal(tm.group(3))
                    sell_qty = self._to_decimal(tm.group(4))
                    balance  = self._to_decimal(tm.group(5))

                    # The Balance column is the truth. Lines such as "Early Pay-in" list a
                    # debit and its reversal separately while the balance does not move, so
                    # the movement is the change in balance, not the Buy/Sell columns.
                    if prev_balance is not None and balance is not None:
                        delta = balance - prev_balance
                        prev_balance = balance
                        if delta == 0:
                            continue
                        qty, txn_type = abs(delta), ("credit" if delta > 0 else "debit")
                    else:
                        prev_balance = balance
                        if buy_qty and buy_qty > 0:
                            qty, txn_type = buy_qty, "credit"
                        elif sell_qty and sell_qty > 0:
                            qty, txn_type = sell_qty, "debit"
                        else:
                            continue

                    desc = f"DEMAT {txn_type.upper()} {qty} {current_symbol}"
                    transactions.append(ParsedTransaction(
                        date=date_val, amount=qty, transaction_type=txn_type,
                        description=desc, raw_description=tm.group(2).strip(),
                        counterparty=current_symbol, isin=current_isin,
                        quantity=qty, running_qty=balance, kind="demat", seq=line_seq,
                    ))
                    line_seq += 1

        # ── Merge continuation lines and parse holdings ───────────────────────
        merged: list[str] = []
        for line in holdings_lines:
            if SKIP_HDR_RE.match(line):
                continue
            if re.match(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]\s', line):
                merged.append(line)
            elif merged:
                merged[-1] = merged[-1] + " " + line

        seen_isins: set[str] = set()
        for line in merged:
            m = HOLDING_RE.match(line)
            if not m:
                continue
            isin         = m.group(1)
            company_name = m.group(2).strip()
            # groups 3–11: curr_bal, free_bal, pldg_bal, earmark_bal, demat, remat, lockin, rate, value
            nums         = [self._to_decimal(m.group(i)) for i in range(3, 12)]
            curr_bal, _free, _pldg, _earmark, _dmt, _rmt, _lock, rate, value = nums

            if not curr_bal or curr_bal <= 0:
                continue

            if isin in seen_isins:
                # Same ISIN parsed twice — page-break repetition in the PDF table.
                # This is a parser artifact, not two separate lots; skip the duplicate.
                logger.warning(
                    "Duplicate ISIN %s (%s) in holdings section — skipping repeated row",
                    isin, company_name,
                )
                continue
            seen_isins.add(isin)

            # ISIN prefix: INF = mutual fund/ETF, IN0 = govt bond, INE = equity
            if isin.startswith("INF"):
                htype = "mutual_fund"
            elif isin.startswith("IN0"):
                htype = "bonds"
            else:
                htype = "equity"

            holdings.append(ParsedHolding(
                instrument_name=company_name or isin,
                symbol=company_name or isin,
                isin=isin,
                units=curr_bal,
                avg_cost_nav=rate,
                current_nav=rate,
                current_value=value or Decimal(0),
                invested_value=None,
                holding_type=htype,
            ))

        return holdings, transactions, as_on

    _OLD_HOLDING_RE = re.compile(
        r'^([A-Z]{2}[A-Z0-9]{9}[0-9])\s+(.+?)\s+([\d,]+\.\d+)\s+(?:[\d,]+\.\d+\s+)*([\d,]+\.\d+)\s+([\d,]+\.\d+)\s*$'
    )

    def _parse_old_layout_holdings(self, full_text: str) -> list[ParsedHolding]:
        """
        ISIN  NAME  CurrBal [FreeBal PldgBal ... Demat]  Rate  Value
        Columns that are blank in the PDF collapse, so only the first number (units) and
        the last two (rate, value) are positional.
        """
        start = re.search(r'HOLDINGS?\s+AS\s+ON', full_text, re.I)
        if not start:
            return []
        out: list[ParsedHolding] = []
        for line in full_text[start.end():].splitlines():
            m = self._OLD_HOLDING_RE.match(line.strip())
            if not m:
                continue
            isin, name = m.group(1), m.group(2).strip()
            units, rate, value = (self._to_decimal(m.group(i)) for i in (3, 4, 5))
            if not units or units <= 0:
                continue
            htype = "mutual_fund" if isin.startswith("INF") else "bonds" if isin.startswith("IN0") else "equity"
            out.append(ParsedHolding(
                instrument_name=name, symbol=name, isin=isin, units=units,
                avg_cost_nav=rate, current_nav=rate, current_value=value or Decimal(0),
                invested_value=None, holding_type=htype,
            ))
        return out

    # ── PDF — Contract Note (daily trade confirmation) ────────────────────────
    def _contract_note_trade_date(self, all_text: str):
        """The note's trade date. The header prints "TRADE DATE 2020-06-23 23/06/2020 SETTLEMENT NO …" (year-first) on some layouts
        and "Trade Date: 23/06/2020" on others; the settlement date (T+2) must never be taken for it."""
        trade_date = None
        DATE_RE = re.compile(
            r'(?:Trade\s+Date|Date|Dated?)\s*[:\-]?\s*'
            r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})',
            re.I,
        )
        iso = re.search(r'TRADE\s+DATE\s*[:\-]?\s*(\d{4})-(\d{2})-(\d{2})', all_text[:3000], re.I)
        if iso:
            return self._parse_date(f"{iso[3]}/{iso[2]}/{iso[1]}", ["%d/%m/%Y"])
        for m in DATE_RE.finditer(all_text[:3000]):
            raw = m.group(1).replace("-", "/")
            d = self._parse_date(raw, ["%d/%m/%Y", "%d/%m/%y"])
            if d:
                return d
        return None

    def _parse_contract_note(self, pdf, path: Path) -> list[ParsedTransaction]:
        """
        Parse a Zerodha contract note PDF (sent per trading day from backoffice@zerodha.com).
        Extracts equity delivery/intraday trades with ₹ amounts for XIRR cash flows.
        Password = PAN (same as monthly demat).
        """
        all_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        trade_date = self._contract_note_trade_date(all_text)

        if not trade_date:
            logger.warning("Contract note: cannot extract trade date from %s", path.name)
            return []

        # ── Try pdfplumber table extraction (most reliable) ───────────────────
        transactions: list[ParsedTransaction] = []
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                transactions.extend(self._parse_contract_note_table(table, trade_date))

        # Deduplicate (same trade_no can appear across pages in multi-page PDFs)
        seen: set[tuple] = set()
        unique: list[ParsedTransaction] = []
        for t in transactions:
            key = (t.reference_number, str(t.date), float(t.amount), t.transaction_type)
            if key not in seen:
                seen.add(key)
                unique.append(t)

        # ── Plain-text layouts (2022 → mid-2024): pdfplumber finds no usable table ──
        if not unique:
            unique = self._parse_contract_note_text(all_text, trade_date)

        # Contract notes print "Settlement Date: YYYY-MM-DD" — exact, no T+1 guessing.
        sm = re.search(r'Settlement\s+Date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})', all_text, re.I)
        settle = self._parse_date(sm.group(1), DATE_FMTS) if sm else None
        if settle:
            for t in unique:
                t.settle_date = settle

        # ── LLM fallback if regex extracted nothing ───────────────────────────
        if not unique and len(all_text) > 300:
            logger.info("Contract note: trying LLM extraction for %s", path.name)
            for rec in self._llm_extract("equity_trades", all_text[:4000]):
                try:
                    qty   = self._to_decimal(str(rec.get("quantity", 0)))
                    price = self._to_decimal(str(rec.get("price", 0)))
                    if not qty or not price or qty <= 0 or price <= 0:
                        continue
                    bs        = str(rec.get("side", "B")).upper()
                    txn_type  = "debit" if bs in ("B", "BUY") else "credit"
                    trade_str = "BUY" if txn_type == "debit" else "SELL"
                    symbol    = str(rec.get("symbol", "")).strip()
                    isin_raw  = str(rec.get("isin", "")).strip()
                    isin      = isin_raw if _ISIN_RE.match(isin_raw) else None
                    amount    = qty * price
                    desc      = f"{trade_str} {qty} × {symbol} @ {price}"
                    unique.append(ParsedTransaction(
                        date=trade_date, amount=amount, transaction_type=txn_type,
                        description=desc, raw_description=desc,
                        counterparty=symbol or None, isin=isin,
                        quantity=qty, price=price, kind="contract_note",
                    ))
                except Exception as exc:
                    logger.warning("LLM contract note trade conversion failed: %s — %s", rec, exc)

        logger.info("Contract note %s: %d trades on %s", path.name, len(unique), trade_date)
        return unique

    # One trade per line:
    #   2022: <order#> <time> <trade#> <time> LICI - EQ / B NSE 2 801.00 801.00 (1602.00)   + ISIN on next line
    #   2024: <order#> <time> <trade#> <time> EMUDHRA-EQ/INE01QM01018 B NSE 3 875.00 875 (2625.00)
    #   2018: <order#> <time> <trade#> <time> BEL B 5 133.15 133.1500 (665.75)   (no exchange token, no ISIN: the section header
    #         says NSE-EQ; the ledger resolves the ISIN from the same symbol elsewhere)
    _CN_LINE_RE = re.compile(
        r'^(\d{10,})\s+\d{2}:\d{2}:\d{2}\s+(\d+)\s+\d{2}:\d{2}:\d{2}\s+'
        r'(?P<sec>.+?)\s+(?P<bs>[BS])\s+(?:(?:NSE|BSE)\s+)?'
        r'(?P<qty>[\d,]+(?:\.\d+)?)\s+(?P<gross>[\d,]+\.\d+)\s+(?P<net>[\d,]+(?:\.\d+)?)\s+'
        r'\(?(?P<total>[\d,]+\.\d+)\)?\s*$'
    )

    def _parse_contract_note_text(self, all_text: str, trade_date) -> list[ParsedTransaction]:
        lines = [ln.strip() for ln in all_text.splitlines()]
        out: list[ParsedTransaction] = []
        seen: set[tuple] = set()
        for i, ln in enumerate(lines):
            m = self._CN_LINE_RE.match(ln)
            if not m:
                continue
            sec = m.group("sec").strip()
            isin_m = re.search(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', sec)
            isin = isin_m.group(0) if isin_m else None
            if isin is None:
                for nxt in lines[i + 1:i + 3]:
                    if _ISIN_RE.match(nxt):
                        isin = nxt
                        break
            symbol = re.sub(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', '', sec)
            symbol = re.sub(r'\s*[-/]\s*(EQ|BE|SM|BL|N\d)\b.*$', '', symbol, flags=re.I)
            symbol = symbol.strip(' /-')
            qty = self._to_decimal(m.group("qty"))
            price = self._to_decimal(m.group("gross"))
            total = self._to_decimal(m.group("total"))
            if not qty or qty <= 0 or not price or not total:
                continue
            bs = m.group("bs")
            trade_no = m.group(2)
            key = (trade_no, bs, str(qty), str(price))
            if key in seen:
                continue
            seen.add(key)
            word = "BUY" if bs == "B" else "SELL"
            out.append(ParsedTransaction(
                date=trade_date, amount=total,
                transaction_type="debit" if bs == "B" else "credit",
                description=f"{word} {qty} × {symbol} @ {price}",
                raw_description=symbol, counterparty=symbol or None,
                reference_number=trade_no, isin=isin,
                quantity=qty, price=price, kind="contract_note",
            ))
        return out

    def _parse_contract_note_table(
        self, table: list, trade_date
    ) -> list[ParsedTransaction]:
        """Extract trades from one pdfplumber table extracted from a contract note page."""
        if not table or len(table) < 2:
            return []

        # pdfplumber produces a 2-row composite header for the dual-column layout
        # (Buy + Sell side by side). Merge row 0 + row 1 cell-wise so subheader text
        # like "Quantity", "WAP", "Total Buy Value..." becomes searchable.
        if len(table) >= 3 and any("BUY" in str(c or "").upper() for c in table[0]) \
                and any("QUANT" in str(c or "").upper() for c in table[1]):
            merged: list[str] = []
            for i in range(max(len(table[0]), len(table[1]))):
                top = str(table[0][i] or "").strip() if i < len(table[0]) else ""
                sub = str(table[1][i] or "").strip() if i < len(table[1]) else ""
                merged.append(f"{top} {sub}".strip().upper().replace("\n", " "))
            headers = merged
            data_rows = table[2:]
        else:
            headers = [str(c or "").strip().upper().replace("\n", " ") for c in table[0]]
            data_rows = table[1:]

        # ── Path A: dual-column Buy/Sell layout (no B/S column) ───────────────
        # Detected when both "Total Buy Value" and "Total Sell Value" headers exist.
        if any("TOTAL BUY" in h for h in headers) and any("TOTAL SELL" in h for h in headers):
            return self._parse_dual_column_table(headers, data_rows, trade_date)

        # ── Path B: single B/S column (legacy / per-trade detail) ─────────────
        bs_col = next(
            (i for i, h in enumerate(headers)
             if h in ("B/S", "B / S", "BUY/SELL", "TYPE") or h.strip() in ("B", "S")),
            None,
        )
        if bs_col is None:
            return []

        # Replace the original `table[0]`-only header detection with the merged
        # header set so the existing column-search code below still works.
        table = [headers] + data_rows

        qty_col   = next((i for i, h in enumerate(headers) if "QTY" in h or "QUANTITY" in h), None)
        gross_col = next(
            (i for i, h in enumerate(headers)
             if "GROSS RATE" in h or "TRADE PRICE" in h or "GROSS" in h or "PRICE" in h or "RATE" in h),
            None,
        )
        net_col   = next(
            (i for i, h in enumerate(headers)
             if "NET TOTAL" in h or "NET AMOUNT" in h or "AMOUNT" in h),
            None,
        )
        sym_col   = next(
            (i for i, h in enumerate(headers)
             if "CONTRACT" in h or "SECURITY" in h or "SYMBOL" in h
             or "SCRIP" in h or "INSTRUMENT" in h or "DESCRIPTION" in h),
            None,
        )
        isin_col      = next((i for i, h in enumerate(headers) if "ISIN" in h), None)
        trade_no_col  = next(
            (i for i, h in enumerate(headers)
             if "TRADE NO" in h or "TRADE NUM" in h or "ORDER NO" in h),
            None,
        )

        if qty_col is None or gross_col is None:
            return []

        def _cell(row: list, idx) -> str:
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx] or "").strip()

        transactions = []
        for row in table[1:]:
            if not row or all(not c for c in row):
                continue

            bs_val = _cell(row, bs_col).upper()
            if bs_val not in ("B", "S", "BUY", "SELL"):
                continue

            qty   = self._to_decimal(_cell(row, qty_col))
            price = self._to_decimal(_cell(row, gross_col))
            if not qty or not price or qty <= 0 or price <= 0:
                continue

            net_total = self._to_decimal(_cell(row, net_col)) if net_col is not None else None
            amount    = net_total if (net_total and net_total > 0) else (qty * price)

            bs        = bs_val[0]   # 'B' or 'S'
            txn_type  = "debit"  if bs == "B" else "credit"
            trade_str = "BUY"    if bs == "B" else "SELL"

            symbol = _cell(row, sym_col)
            # Remove exchange-series suffixes (EQ, BE, SM …) and trailing ISIN if embedded
            isin_in_sym = re.search(r'([A-Z]{2}[A-Z0-9]{9}[0-9])', symbol)
            if isin_in_sym:
                embedded_isin = isin_in_sym.group(1)
                symbol = symbol.replace(embedded_isin, "").strip()
            else:
                embedded_isin = None
            symbol = re.sub(r'\s+(EQ|BE|SM|BL|N1|N2|N3)\b.*', '', symbol, flags=re.I).strip()

            isin_raw = _cell(row, isin_col) if isin_col is not None else ""
            isin     = isin_raw   if _ISIN_RE.match(isin_raw)     else \
                       embedded_isin if embedded_isin             else None

            trade_no = _cell(row, trade_no_col) if trade_no_col is not None else None

            desc = f"{trade_str} {qty} × {symbol} @ {price}"
            transactions.append(ParsedTransaction(
                date=trade_date,
                amount=amount,
                transaction_type=txn_type,
                description=desc,
                raw_description=symbol,
                counterparty=symbol or None,
                reference_number=trade_no or None,
                isin=isin,
                quantity=qty, price=price, kind="contract_note",
            ))

        return transactions

    def _parse_dual_column_table(
        self, headers: list[str], data_rows: list[list], trade_date,
    ) -> list[ParsedTransaction]:
        """
        Parse the modern Zerodha contract-note layout where Buy and Sell quantities
        live in separate column groups within the same row, e.g.:

            ISIN | Symbol | Buy.Qty | Buy.WAP | … | Total Buy Value | Sell.Qty | … | Total Sell Value | Net Qty | Net Obligation

        Each non-zero Buy.Qty or Sell.Qty becomes its own transaction. The amount
        used is the brokerage-inclusive Total Buy/Sell Value column.
        """
        # Locate the relevant columns. "TOTAL BUY VALUE" appears once and ends the
        # buy block; everything after it (until "TOTAL SELL VALUE") is the sell block.
        try:
            buy_total_idx  = next(i for i, h in enumerate(headers) if "TOTAL BUY"  in h)
            sell_total_idx = next(i for i, h in enumerate(headers) if "TOTAL SELL" in h)
        except StopIteration:
            return []

        # Quantity columns: first "QUANT" header in each block.
        buy_qty_idx  = next(
            (i for i, h in enumerate(headers[:buy_total_idx]) if "QUANT" in h),
            None,
        )
        sell_qty_idx = next(
            (i for i, h in enumerate(headers[buy_total_idx + 1: sell_total_idx], start=buy_total_idx + 1)
             if "QUANT" in h),
            None,
        )

        # Price columns: first WAP/PRICE/RATE header in each block.
        def _price_col(start: int, end: int) -> Optional[int]:
            for i in range(start, end):
                h = headers[i]
                if "WAP" in h or "PRICE" in h or "RATE" in h:
                    return i
            return None

        buy_price_idx  = _price_col(0, buy_total_idx)
        sell_price_idx = _price_col(buy_total_idx + 1, sell_total_idx)

        isin_idx = next((i for i, h in enumerate(headers) if "ISIN" in h), 0)
        # Prefer the dedicated "SECURITY NAME / SYMBOL" column. Avoid headers
        # that also contain "ISIN" (the composite "Security Description ISIN"
        # group header), since those point at the ISIN column itself.
        sym_idx  = next(
            (i for i, h in enumerate(headers)
             if ("SECURITY NAME" in h or "SYMBOL" in h) and "ISIN" not in h),
            isin_idx + 1 if isin_idx + 1 < len(headers) else 0,
        )

        if buy_qty_idx is None and sell_qty_idx is None:
            return []

        def _cell(row: list, idx: Optional[int]) -> str:
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx] or "").strip()

        transactions: list[ParsedTransaction] = []
        for row in data_rows:
            if not row or all(not c for c in row):
                continue

            isin_raw = _cell(row, isin_idx)
            isin     = isin_raw if _ISIN_RE.match(isin_raw) else None
            symbol   = _cell(row, sym_idx) or (isin or "")

            # Buy side
            buy_qty   = self._to_decimal(_cell(row, buy_qty_idx))
            buy_amt   = self._to_decimal(_cell(row, buy_total_idx))
            buy_price = self._to_decimal(_cell(row, buy_price_idx))
            if buy_qty and buy_qty > 0 and (buy_amt or buy_price):
                amt = buy_amt if (buy_amt and buy_amt > 0) else (buy_qty * buy_price)
                transactions.append(ParsedTransaction(
                    date=trade_date,
                    amount=amt,
                    transaction_type="debit",
                    description=f"BUY {buy_qty} × {symbol} @ {buy_price or ''}".strip(),
                    raw_description=symbol,
                    counterparty=symbol or None,
                    isin=isin,
                    quantity=buy_qty, price=buy_price, kind="contract_note",
                ))

            # Sell side
            sell_qty   = self._to_decimal(_cell(row, sell_qty_idx))
            sell_amt   = self._to_decimal(_cell(row, sell_total_idx))
            sell_price = self._to_decimal(_cell(row, sell_price_idx))
            if sell_qty and sell_qty > 0 and (sell_amt or sell_price):
                amt = sell_amt if (sell_amt and sell_amt > 0) else (sell_qty * sell_price)
                transactions.append(ParsedTransaction(
                    date=trade_date,
                    amount=amt,
                    transaction_type="credit",
                    description=f"SELL {sell_qty} × {symbol} @ {sell_price or ''}".strip(),
                    raw_description=symbol,
                    counterparty=symbol or None,
                    isin=isin,
                    quantity=sell_qty, price=sell_price, kind="contract_note",
                ))

        return transactions

    def _holding_from_row(self, row: list, headers: list[str]) -> ParsedHolding | None:
        def col(*keywords):
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return None

        isin_i    = col("isin")
        name_i    = col("scrip", "name", "instrument", "security")
        closing_i = col("closing", "balance", "qty", "quantity")
        value_i   = col("value", "cur. val", "market")
        avg_i     = col("avg", "cost", "purchase")

        if isin_i is None or closing_i is None:
            return None

        max_needed = max(i for i in [isin_i, closing_i, name_i, value_i, avg_i] if i is not None)
        if len(row) <= max_needed:
            return None

        isin = str(row[isin_i] or "").strip()
        if not isin or not _ISIN_RE.match(isin):
            return None

        units = self._to_decimal(row[closing_i])
        if not units or units <= 0:
            return None

        name    = str(row[name_i] or "").strip() if name_i is not None else isin
        cur_val = self._to_decimal(row[value_i]) if value_i is not None else None
        avg     = self._to_decimal(row[avg_i])   if avg_i   is not None else None

        return ParsedHolding(
            instrument_name=name or isin,
            symbol=name or isin,
            isin=isin,
            units=units,
            avg_cost_nav=avg,
            current_nav=None,
            current_value=cur_val or Decimal(0),
            invested_value=(units * avg) if units and avg else None,
            holding_type="equity",
        )

    def _demat_txn_from_row(self, row: list, headers: list[str]) -> ParsedTransaction | None:
        def col(*keywords):
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return None

        date_i   = col("date")
        isin_i   = col("isin")
        name_i   = col("scrip", "name", "instrument", "security")
        credit_i = col("credit", "receipt")
        debit_i  = col("debit", "deliver")
        ref_i    = col("ref", "remark")

        if date_i is None or (credit_i is None and debit_i is None):
            return None
        if len(row) <= date_i:
            return None

        txn_date = self._parse_date(str(row[date_i] or "").strip(), DATE_FMTS)
        if txn_date is None:
            return None

        isin = str(row[isin_i] or "").strip() if isin_i is not None and len(row) > isin_i else ""
        name = str(row[name_i] or "").strip() if name_i is not None and len(row) > name_i else ""

        credit_qty = self._to_decimal(row[credit_i]) if credit_i is not None and len(row) > credit_i else None
        debit_qty  = self._to_decimal(row[debit_i])  if debit_i  is not None and len(row) > debit_i  else None

        if credit_qty and credit_qty > 0:
            qty, txn_type = credit_qty, "credit"
        elif debit_qty and debit_qty > 0:
            qty, txn_type = debit_qty, "debit"
        else:
            return None

        ref   = str(row[ref_i] or "").strip() if ref_i is not None and len(row) > ref_i else ""
        label = name or isin
        desc  = f"DEMAT {txn_type.upper()} {qty} {label}"

        return ParsedTransaction(
            date=txn_date,
            amount=qty,        # quantity of shares; no rupee value at demat-statement level
            transaction_type=txn_type,
            description=desc,
            raw_description=label,
            counterparty=label or None,
            reference_number=ref or None,
            isin=isin or None,
        )

    def _parse_generic_pdf(self, pdf) -> list[ParsedHolding]:
        """Fallback: extract holdings from any table with ≥4 columns."""
        holdings: list[ParsedHolding] = []
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                for row in (table or []):
                    if not row or len(row) < 4:
                        continue
                    name = str(row[0] or "").strip()
                    if not name or name.lower() in ("instrument", "stock", "isin", "scrip", ""):
                        continue
                    units   = self._to_decimal(row[1])
                    avg_nav = self._to_decimal(row[2])
                    cur_val = self._to_decimal(row[4]) if len(row) > 4 else None
                    pnl     = self._to_decimal(row[5]) if len(row) > 5 else None
                    if not cur_val:
                        continue
                    invested = (units * avg_nav) if units and avg_nav else None
                    holdings.append(ParsedHolding(
                        instrument_name=name, symbol=name,
                        units=units or Decimal(0),
                        avg_cost_nav=avg_nav, current_nav=None,
                        current_value=cur_val, invested_value=invested,
                        gain_loss=pnl, holding_type="equity",
                    ))
        return holdings
