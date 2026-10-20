"""
Minimal .xlsx reader on the standard library: sheet name -> rows of cell text (None for empty cells).

Zerodha Console exports tradebooks as .xlsx with a few preamble rows above the header. A statement file is just text and
numbers, so shared/inline strings and plain values are all that is needed; no spreadsheet package is installed.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_sheets(path: Path) -> dict[str, list[list[str | None]]]:
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        out: dict[str, list[list[str | None]]] = {}
        for sheet in wb.find(f"{_NS}sheets"):
            target = rels[sheet.get(f"{_REL}id")].lstrip("/")
            rows = []
            for row in ET.fromstring(z.read(target if target.startswith("xl/") else f"xl/{target}")).iter(f"{_NS}row"):
                cells: dict[int, str | None] = {}
                for c in row.findall(f"{_NS}c"):
                    v, t = c.find(f"{_NS}v"), c.get("t")
                    if t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{_NS}t"))
                    elif v is None:
                        continue
                    elif t == "s":
                        val = shared[int(v.text)]
                    else:
                        val = v.text
                    cells[_col(c.get("r"))] = val
                width = max(cells) + 1 if cells else 0
                rows.append([cells.get(i) for i in range(width)])
            out[sheet.get("name")] = rows
        return out
