#!/usr/bin/env python3
"""
parse_278t.py: turn the OGE 278-T PDFs that oge_watch.py downloads into one
dataset of individual transactions, for the web page in docs/.

What it does
------------
1. Reads oge_data/filings.csv (written by oge_watch.py) and picks out the
   periodic transaction reports (278-T).
2. Extracts each PDF's text. Electronically filed reports have a text layer;
   scanned pages are run through OCR (Tesseract).
3. Finds the transaction rows. Every 278-T row has the same shape:
       1  Apple Inc. (AAPL)   Purchase   03/27/2025   No   $15,001 - $50,000
       #  description          type       date        >30d  amount range
4. Cleans up OCR noise, snaps amounts to the official OGE value bands,
   matches company names to stock tickers using the SEC's public company
   list, and flags anything it isn't confident about as "needs review".
5. Writes docs/data/transactions.json, which the web page reads.

Each PDF's results are cached in oge_data/parsed/, so only new PDFs are
processed on later runs (OCR is slow). Delete that folder to re-parse all.

Files you can edit
------------------
ticker_overrides.csv     description,ticker,company  (exact description
                         text from the filing, or a substring of it).
                         Use it to fix or fill in ticker matches.
sector_overrides.csv     ticker,sector  to correct a company's sector. Sectors
                         come from each company's SEC industry (SIC) code,
                         which is sometimes broad or dated.
oge_data/unmatched.csv   written by this script: descriptions it could not
                         match, most frequent first, as a to-do list for
                         ticker_overrides.csv.

Usage
-----
    python parse_278t.py
    python parse_278t.py --reparse        # ignore the cache

Requirements
------------
    pip install pdfplumber pytesseract
    plus the Tesseract program (Ubuntu: sudo apt-get install tesseract-ocr,
    macOS: brew install tesseract). Without Tesseract, scanned pages are
    skipped and reported.

The SEC asks automated clients to identify themselves. Set the environment
variable SEC_USER_AGENT to something like "Your Name you@example.com".
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber

DATA_DIR = Path("oge_data")
CACHE_DIR = DATA_DIR / "parsed"
OUT_PATH = Path("docs/data/transactions.json")
EVENTS_PATH = Path("docs/data/events.json")
OVERRIDES_PATH = Path("ticker_overrides.csv")
SEC_CACHE = DATA_DIR / "sec_company_tickers.json"
SEC_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SECTOR_CACHE = DATA_DIR / "sectors.json"
SECTOR_OVERRIDES_PATH = Path("sector_overrides.csv")
PARSER_VERSION = 5  # bump to force re-parsing after changing the logic

# The fixed value bands on OGE forms. OCR often garbles the digits, so
# parsed amounts are snapped to the nearest real band.
BANDS = [
    (1_001, 15_000), (15_001, 50_000), (50_001, 100_000), (100_001, 250_000),
    (250_001, 500_000), (500_001, 1_000_000), (1_000_001, 5_000_000),
    (5_000_001, 25_000_000), (25_000_001, 50_000_000), (50_000_001, None),
]

# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

def looks_like_transactions(text: str) -> bool:
    return len(re.findall(r"\$\s?\d", text)) >= 3 or len(re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", text)) >= 3


def page_rows(pdf_path: Path) -> tuple[list[tuple[str, bool]], int]:
    """Return (text, was_ocr) per page. A page is OCRed when it has no text
    layer, or when its text layer is too garbled to yield any rows (some
    scans come with poor built-in OCR)."""
    out, ocr_count = [], 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len(text.strip()) >= 80:
                found = len(parse_lines(text, 0, False))
                dates = len(re.findall(DATE, text))
                electronic = "electronically signed" in text.lower() or "integrity.gov" in text.lower()
                # Keep the text layer unless it looks like a transactions
                # page whose rows mostly failed to parse (a garbled scan).
                if electronic or not looks_like_transactions(text) or found >= dates * 0.6:
                    out.append((text, False))
                    continue
            ocr_text = ocr_page(page)
            ocr_count += 1
            if len(parse_lines(ocr_text, 0, True)) >= len(parse_lines(text, 0, False)):
                out.append((ocr_text, True))
            else:
                out.append((text, False))
    return out, ocr_count


_ocr_warned = False


def remove_rules(image):
    """Erase table grid lines, which otherwise wreck Tesseract's reading of
    the rows between them."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return image
    a = np.array(image.convert("L"))
    dark = a < 160
    a = a.copy()
    for r in np.where(dark.mean(axis=1) > 0.35)[0]:
        a[max(r - 1, 0):r + 2, :] = 255
    for c in np.where(dark.mean(axis=0) > 0.35)[0]:
        a[:, max(c - 1, 0):c + 2] = 255
    return Image.fromarray(a)


def ocr_page(page) -> str:
    global _ocr_warned
    try:
        import pytesseract
        image = remove_rules(page.to_image(resolution=300).original)
        # psm 6 = treat the page as one uniform block, which keeps table
        # rows on single lines.
        return pytesseract.image_to_string(image, config="--psm 6")
    except Exception as err:
        if not _ocr_warned:
            print(f"  ! OCR unavailable ({err}); scanned pages will be skipped")
            _ocr_warned = True
        return ""


# --------------------------------------------------------------------------
# Row parsing
# --------------------------------------------------------------------------

# A row is: [#] description type date [Yes/No] amount. The date followed by
# an amount is the anchor, which avoids confusing it with bond maturity dates
# inside descriptions. Everything around it is loose, because OCR turns
# column borders into stray characters like | [ ) !
DATE = r"\d{1,2}\s*/\s*\d{1,2}\s*[/1lI|]\s*\d{2,4}"
ROW_RE = re.compile(
    r"^\s*(?:(?P<num>\d{1,4})[.)]?\s+)?[|_\-\s]*(?P<desc>.+?)\s*[|_]?\s+"
    r"(?P<type>[A-Za-z0-9'’:.()\[\]■-]{3,20}(?:\s*\((?:partial|full)\))?)\s*[|]?\s+"
    r"(?P<date>" + DATE + r")[)!|\]:;,.]*\s+"
    r"(?:(?P<notif>yes|no|ye5|n0|na|nn|ves|vee|wee|y|n|[YVN][A-Za-z]{1,3})\b[|\]\s]*)?"
    r"[|\[\]\s]*(?P<amount>(?:over|0ver|[$S§¢]|\d{1,3}[,.]\d{3}).*?)\s*$",
    re.I,
)
NUM_ONLY_RE = re.compile(r"^\s*(\d{1,4})[.)]?\s+(?:YES|NO|Y|N)\b", re.I)
HEADER_HINTS = ("DESCRIPTION", "NOTIFICATION", "DAYS AGO", "Page ", "Privacy Act",
                "OGE Form", "Endnotes", "Summary of Contents", "Filer's Information")
MONEY_ONLY_RE = re.compile(r"^[\s$S§¢\d,.\-–•·|]+$")


def classify_type(token: str) -> str | None:
    t = re.sub(r"[^a-z]", "", token.lower())
    if not t:
        return None
    if "exch" in t:
        return "exchange"
    if ("rch" in t or "urcha" in t or "chas" in t or "un:h" in token.lower() or "rt:h" in token.lower()
            or re.match(r"[lid]?[op]?[ou][unr]?[cnt]?h", t)):
        return "buy"
    if re.fullmatch(r"[s5a][ae][l1t][eo]", t):
        return "sell"
    if t.startswith(("sal", "sa1", "5al")) or "sale" in t:
        return "sell"
    buy = SequenceMatcher(None, t, "purchase").ratio()
    sell = SequenceMatcher(None, t, "sale").ratio()
    exch = SequenceMatcher(None, t, "exchange").ratio()
    best = max(buy, sell, exch)
    if best < 0.5:
        return None
    return "buy" if best == buy else "sell" if best == sell else "exchange"


def parse_amount(text: str) -> tuple[int | None, int | None, bool]:
    """Returns (min, max, exact_band_match). min None means unreadable;
    max None means an open-ended "over" band."""
    t = re.sub(r"[\s\u00a0•·|\[\]]", "", text)
    t = re.sub(r"[S§¢]", "$", t)
    t = re.sub(r"(?<=\d)[oO](?=\d)|(?<=\d)[oO]$", "0", t)
    t = re.sub(r"(?<=\d)\.(?=\d{3})", ",", t)          # $100.001 -> $100,001
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", t)]
    if re.match(r"(?i)[0o]ver", t):
        floor = nums[0] + 1 if nums else 50_000_001
        band = min(BANDS, key=lambda b: abs(b[0] - floor))
        return band[0], band[1], band[0] == floor
    nums = [n for n in nums if n >= 1_000]
    if not nums:
        return None, None, False
    low = nums[0]
    # Compare on a log scale: OCR errors are usually a digit or two, and the
    # bands grow geometrically.
    band = min(BANDS, key=lambda b: abs(math.log(b[0]) - math.log(low)))
    exact = band[0] == low and (len(nums) < 2 or band[1] == nums[1])
    return band[0], band[1], exact


def to_iso(date_text: str, year_hint: int | None = None) -> str | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})[/1lI|](\d{2,4})$", re.sub(r"\s", "", date_text))
    if not m:
        return None
    mm, dd, yy = m.group(1), m.group(2), m.group(3)
    if int(mm) > 12 and len(mm) == 2:          # "41/28/2025": a stray leading digit
        mm = mm[1]
    year = int(yy) + 2000 if len(yy) == 2 else int(yy)
    if year_hint and not (year_hint - 2 <= year <= year_hint) and len(yy) == 4:
        # OCR misreads digits ("2076", "2028" for 2026); try likely fixes and
        # keep one that lands in the plausible window before the filing.
        confusions = {"7": "2", "8": "63", "0": "268", "5": "63", "3": "85", "9": "6", "1": "7", "4": "1"}
        digits = "20" + yy[2:]
        candidates = {digits}
        for i in (2, 3):
            for alt in confusions.get(digits[i], ""):
                candidates.add(digits[:i] + alt + digits[i + 1:])
        plausible = sorted((int(c) for c in candidates if year_hint - 1 <= int(c) <= year_hint), reverse=True)
        if plausible:
            year = plausible[0]
    try:
        return date(year, int(mm), int(dd)).isoformat()
    except ValueError:
        return None


def parse_lines(text: str, page_no: int, was_ocr: bool, year_hint: int | None = None) -> list[dict]:
    rows, last, pending_num = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = ROW_RE.match(line)
        if m and len(m.group("desc")) >= 2:
            low, high, exact = parse_amount(m.group("amount"))
            num = m.group("num") or pending_num
            last = {
                "row": int(num) if num else None,
                "page": page_no,
                "description": m.group("desc").strip(" |_-"),
                "type_raw": m.group("type"),
                "type": classify_type(m.group("type")),
                "date": to_iso(m.group("date"), year_hint),
                "amount_min": low,
                "amount_max": high,
                "amount_exact": exact,
                "ocr": was_ocr,
            }
            rows.append(last)
            pending_num = None
            continue
        # Some forms put "12  YES" on its own line above the description.
        n = NUM_ONLY_RE.match(line)
        if n and len(line) < 20:
            pending_num, last = n.group(1), None
            continue
        # A long description can wrap onto the next line. Attach short
        # follow-on lines to the row above, but never boilerplate or a
        # wrapped amount like "$5,000,000".
        if (last and len(line) <= 60 and not MONEY_ONLY_RE.match(line)
                and not any(h.lower() in line.lower() for h in HEADER_HINTS)
                and not re.search(DATE, line) and last.get("_wraps", 0) < 2):
            last["description"] += " " + line
            last["_wraps"] = last.get("_wraps", 0) + 1
        else:
            last = None
    for i, r in enumerate(rows, start=1):
        r.pop("_wraps", None)
        if r["row"] is None:
            r["row"] = 10_000 + i
    return rows


def parse_pdf(pdf_path: Path, year_hint: int | None = None) -> dict:
    pages, ocr_pages = page_rows(pdf_path)
    rows = []
    for page_no, (text, was_ocr) in enumerate(pages, start=1):
        rows += parse_lines(text, page_no, was_ocr, year_hint)
    return {"rows": rows, "pages": len(pages), "ocr_pages": ocr_pages,
            "empty_pages": sum(1 for t, _ in pages if not t.strip())}


# --------------------------------------------------------------------------
# Ticker matching
# --------------------------------------------------------------------------

SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COS", "COMPANY", "COMPANIES",
    "LTD", "LIMITED", "PLC", "LLC", "LP", "HOLDINGS", "HOLDING", "HLDGS", "HLDG", "GROUP", "GRP",
    "THE", "CL", "CLASS", "A", "B", "C", "F", "COM", "COMMON", "STOCK", "SHS", "SHARES", "ORD", "NEW",
    "SA", "NV", "AG", "SE", "ADR", "ADS", "SPONSORED", "SPON", "DEL", "DE", "REG", "NPV", "PAR",
    "REIT", "UNSOLICITED", "SOLICITED", "MASS", "IRELAND", "EUR", "USD", "SEE", "ENDNOTE",
}
ALIASES = {
    "INTL": "INTERNATIONAL", "TECH": "TECHNOLOGIES", "TECHNOLOGY": "TECHNOLOGIES",
    "SYS": "SYSTEMS", "PHARMA": "PHARMACEUTICALS", "FINL": "FINANCIAL", "BK": "BANK",
    "SVCS": "SERVICES", "SVC": "SERVICE", "MTRS": "MOTORS", "&": "AND", "LABS": "LABORATORIES",
    "MGMT": "MANAGEMENT", "NATL": "NATIONAL", "CHEM": "CHEMICAL", "ENTMT": "ENTERTAINMENT",
    "RES": "RESOURCES", "WHSL": "WHOLESALE", "CMNTYS": "COMMUNITIES", "PPTYS": "PROPERTIES",
    "PPTY": "PROPERTY", "MTG": "MORTGAGE", "TR": "TRUST", "CAP": "CAPITAL", "AMER": "AMERICA",
    "ASSOC": "ASSOCIATES", "ELEC": "ELECTRIC", "GR": "GROUP", "NAT": "NATURAL", "COMMUNICATNS": "COMMUNICATIONS",
}


def norm_tokens(name: str) -> list[str]:
    text = re.sub(r"\(.*?\)", " ", name.upper())
    text = re.sub(r"/[A-Z]{2,3}/?", " ", text)                     # SEC's "/CA/" state tags
    text = re.sub(r"\$[\d,.]+", " ", text)                           # stray amounts
    text = re.sub(r"\b0(?=[A-Z])|(?<=[A-Z]-)0\b", "O", text)        # OCR: "0 REILLY" -> "O REILLY"
    # Share-class suffixes, including OCR mangling like "INC Cl A" -> "INCCIA"
    text = re.sub(r"INC\s*C[LI1]\s*[AB]\b", "INC", text)
    text = re.sub(r"\bC[LI1]\s*[AB]\b", " ", text)
    # Suffixes glued on by OCR: "WALMARTINC" -> "WALMART INC"
    text = re.sub(r"(?<=[A-Z&]{3})(INC|CORP|PLC|LTD)\b", r" \1", text)
    text = text.replace("&", " & ")
    text = re.sub(r"[^A-Z0-9& ]", " ", text)
    toks = [ALIASES.get(t, t) for t in text.split()]
    return [t for t in toks if t not in SUFFIXES]


def asset_class(desc: str) -> str:
    d = desc.upper()
    if re.search(r"\b(CALL|PUT)\b", d):
        return "option"
    if (re.search(r"\d+(\.\d+)?\s?%", d) or re.search(r"\b(BD|BDS|BOND|BONDS|NOTE|NOTES|NTS|REV|GO|MUNI|DUE|MTN|DEB|SER)\b", d)
            or re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", d)):
        return "bond"
    if re.search(r"\b(ETF|FUND|INDEX|SPDR|ISHARES|VANGUARD|TRUST)\b", d):
        return "fund"
    return "stock"


class TickerMatcher:
    def __init__(self, sec: dict, overrides: list[dict]):
        self.overrides = overrides
        self.by_ticker, self.by_key, self.by_tokenset, self.by_joined, self.by_first = {}, {}, {}, {}, {}
        self.cik = {}
        self.keys = []
        for item in sec.values():
            ticker, title = item["ticker"].upper(), item["title"]
            self.by_ticker.setdefault(ticker, title)
            self.cik.setdefault(ticker, int(item.get("cik_str") or 0))
            toks = norm_tokens(title)
            if not toks:
                continue
            key = " ".join(toks)
            self.by_key.setdefault(key, (ticker, title))
            self.by_tokenset.setdefault(" ".join(sorted(toks)), (ticker, title))
            self.by_joined.setdefault("".join(toks), (ticker, title))
            self.by_first.setdefault(toks[0], []).append((key, ticker, title))
            self.keys.append((key, ticker, title))

    def match(self, desc: str) -> tuple[str, str, float]:
        """Returns (ticker, company name, confidence 0-1)."""
        low = desc.lower()
        for o in self.overrides:
            if o["description"].lower() in low:
                return o["ticker"].upper(), o.get("company") or self.by_ticker.get(o["ticker"].upper(), desc), 1.0
        explicit = re.search(r"\(([A-Z]{1,5}(?:[.\-][A-Z])?)\)", desc)
        if explicit:
            sym = explicit.group(1)
            for cand in (sym, sym.replace(".", "-"), sym[:-1] + "-" + sym[-1] if len(sym) > 3 else sym):
                if cand in self.by_ticker:
                    return cand, self.by_ticker[cand], 1.0
        toks = norm_tokens(desc)
        if not toks:
            return "", "", 0.0
        key = " ".join(toks)
        for table, conf, k in ((self.by_key, 0.98, key), (self.by_tokenset, 0.95, " ".join(sorted(toks))),
                               (self.by_joined, 0.95, "".join(toks))):
            if k in table:
                return table[k][0], table[k][1], conf
        # A bare ticker-like name, e.g. "IBM CORP" -> IBM
        if len(toks) == 1 and toks[0] in self.by_ticker and len(toks[0]) >= 3:
            return toks[0], self.by_ticker[toks[0]], 0.9
        # Truncated names, e.g. "FIDELITY NATL INFORMATIO"
        if len(key) >= 14:
            hits = [(t, title) for k, t, title in self.keys if k.startswith(key)]
            if len({t for t, _ in hits}) == 1:
                return hits[0][0], hits[0][1], 0.9
        # Fuzzy: compare against companies sharing one of the first two words.
        pool = []
        for t in toks[:2]:
            pool += self.by_first.get(t, [])
        best = ("", "", 0.0)
        for cand_key, ticker, title in pool:
            ratio = SequenceMatcher(None, key, cand_key).ratio()
            if ratio > best[2]:
                best = (ticker, title, ratio)
        return best if best[2] >= 0.86 else ("", "", best[2])


def load_sec() -> dict:
    fresh = SEC_CACHE.exists() and datetime.now().timestamp() - SEC_CACHE.stat().st_mtime < 7 * 86400
    if not fresh:
        ua = os.environ.get("SEC_USER_AGENT", "").strip()
        if not ua:
            print("  ! Set SEC_USER_AGENT (\"Name email\") so the SEC company list can be downloaded")
        try:
            req = urllib.request.Request(SEC_URL, headers={"User-Agent": ua or "oge-watch research script"})
            data = urllib.request.urlopen(req, timeout=60).read()
            json.loads(data)
            SEC_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SEC_CACHE.write_bytes(data)
        except Exception as err:
            print(f"  ! Couldn't refresh the SEC company list ({err})")
    if SEC_CACHE.exists():
        return json.loads(SEC_CACHE.read_text())
    print("  ! No SEC company list; tickers will only come from overrides and (TICKER) text")
    return {}


def load_overrides() -> list[dict]:
    if not OVERRIDES_PATH.exists():
        return []
    with OVERRIDES_PATH.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("description") or "").strip() and (r.get("ticker") or "").strip()]
    # Longest descriptions first, so specific overrides beat general ones.
    return sorted(rows, key=lambda r: -len(r["description"]))


# --------------------------------------------------------------------------
# Sectors
# --------------------------------------------------------------------------
# Every SEC filer has a Standard Industrial Classification (SIC) code. These
# ranges group them into sectors that line up with what federal departments
# regulate. First match wins, so specific ranges come before broad ones.
SIC_SECTORS = [
    ((3720, 3729), "Aerospace & defense"), ((3760, 3769), "Aerospace & defense"),
    ((3480, 3489), "Aerospace & defense"), ((3795, 3795), "Aerospace & defense"),
    ((3812, 3812), "Aerospace & defense"),
    ((1300, 1399), "Oil & gas"), ((2900, 2999), "Oil & gas"), ((4610, 4619), "Oil & gas"),
    ((5171, 5172), "Oil & gas"),
    ((1000, 1299), "Mining & metals"), ((1400, 1499), "Mining & metals"), ((3310, 3399), "Mining & metals"),
    ((4950, 4959), "Industrials & construction"),
    ((4900, 4999), "Utilities & power"),
    ((3710, 3716), "Autos & vehicles"), ((3740, 3799), "Autos & vehicles"), ((5010, 5015), "Autos & vehicles"),
    ((5500, 5599), "Autos & vehicles"),
    ((4000, 4799), "Transportation & logistics"),
    ((2830, 2836), "Pharma & biotech"), ((8731, 8731), "Pharma & biotech"), ((5122, 5122), "Pharma & biotech"),
    ((3840, 3851), "Medical devices"), ((5047, 5047), "Medical devices"),
    ((6324, 6324), "Health care services & insurance"), ((8000, 8099), "Health care services & insurance"),
    ((6000, 6199), "Banks & lending"),
    ((6798, 6798), "Real estate & homebuilding"), ((6500, 6599), "Real estate & homebuilding"),
    ((1520, 1531), "Real estate & homebuilding"),
    ((6200, 6799), "Investment & insurance"),
    ((3670, 3679), "Semiconductors & electronics"), ((3600, 3699), "Semiconductors & electronics"),
    ((3570, 3579), "Software & internet"), ((7370, 7379), "Software & internet"),
    ((4800, 4899), "Telecom & media"), ((2710, 2799), "Telecom & media"), ((7800, 7899), "Telecom & media"),
    ((0, 999), "Food, agriculture & tobacco"), ((2000, 2199), "Food, agriculture & tobacco"),
    ((5140, 5159), "Food, agriculture & tobacco"), ((5180, 5182), "Food, agriculture & tobacco"),
    ((5200, 5999), "Retail & consumer"), ((2200, 2399), "Retail & consumer"), ((3100, 3199), "Retail & consumer"),
    ((3630, 3639), "Retail & consumer"), ((3940, 3949), "Retail & consumer"), ((5000, 5199), "Retail & consumer"),
    ((2800, 2829), "Chemicals & materials"), ((2840, 2899), "Chemicals & materials"),
    ((2400, 2699), "Chemicals & materials"), ((3000, 3299), "Chemicals & materials"),
    ((8200, 8299), "Education"),
    ((1500, 1799), "Industrials & construction"), ((3400, 3599), "Industrials & construction"),
    ((3800, 3999), "Industrials & construction"), ((8700, 8749), "Industrials & construction"),
    ((7000, 8999), "Business & consumer services"),
]


def sector_for_sic(sic) -> str:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return "Unclassified"
    for (lo, hi), name in SIC_SECTORS:
        if lo <= code <= hi:
            return name
    return "Unclassified"


def load_sector_overrides() -> dict:
    if not SECTOR_OVERRIDES_PATH.exists():
        return {}
    with SECTOR_OVERRIDES_PATH.open(newline="", encoding="utf-8") as fh:
        return {r["ticker"].strip().upper(): r["sector"].strip()
                for r in csv.DictReader(fh) if (r.get("ticker") or "").strip() and (r.get("sector") or "").strip()}


def lookup_sectors(tickers: set[str], ciks: dict) -> dict:
    """Ticker -> {sic, sic_desc}. Looked up once per ticker from the SEC's
    free submissions API and cached in oge_data/sectors.json."""
    cache = json.loads(SECTOR_CACHE.read_text()) if SECTOR_CACHE.exists() else {}
    missing = sorted(t for t in tickers if t not in cache and ciks.get(t))
    if missing:
        print(f"  looking up industry codes for {len(missing)} new tickers", flush=True)
    ua = os.environ.get("SEC_USER_AGENT", "").strip() or "oge-watch research script"
    failures = 0
    for i, ticker in enumerate(missing):
        try:
            req = urllib.request.Request(SEC_SUBMISSIONS.format(cik=ciks[ticker]),
                                         headers={"User-Agent": ua, "Accept": "application/json"})
            info = json.loads(urllib.request.urlopen(req, timeout=30).read())
            cache[ticker] = {"sic": info.get("sic") or "", "sic_desc": info.get("sicDescription") or ""}
        except Exception as err:
            failures += 1
            if failures == 5:
                print(f"  ! SEC lookups failing ({err}); will retry next run")
            if failures >= 20:
                break
        time.sleep(0.15)  # the SEC allows 10 requests a second
        if i % 200 == 199:
            SECTOR_CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True))
    SECTOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SECTOR_CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True))
    return cache


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def norm_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ensure_pdf(f: dict) -> Path | None:
    """Find the filing's PDF, downloading it if the watcher's download failed."""
    if not f.get("pdf_url"):
        return None
    import oge_watch
    path = oge_watch.pdf_path(f)
    if path.is_file():
        return path
    got = oge_watch.download_pdf(f)
    return Path(got) if got else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse downloaded OGE 278-T PDFs into docs/data/transactions.json")
    ap.add_argument("--reparse", action="store_true", help="ignore the cache and parse every PDF again")
    args = ap.parse_args()

    filings_csv = DATA_DIR / "filings.csv"
    if not filings_csv.exists():
        print("No oge_data/filings.csv yet. Run oge_watch.py first.")
        return 1
    with filings_csv.open(newline="", encoding="utf-8") as fh:
        filings = list(csv.DictReader(fh))

    matcher = TickerMatcher(load_sec(), load_overrides())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    out_filings, transactions, unmatched, used_cache = [], [], Counter(), set()
    for f in sorted(filings, key=lambda r: (r["date"], r["person"])):
        filing_id = hashlib.sha1(f"{f['person']}|{f['date']}|{f['doc_type']}|{f['pdf_url'] or f['request_url']}".encode()).hexdigest()[:10]
        entry = {
            "id": filing_id, "person": f["person"], "role": f["role"], "posted": f["date"],
            "doc_type": f["doc_type"], "is_278t": f["is_278t"] == "True",
            "url": f["pdf_url"] or f["request_url"], "needs_request": bool(f["request_url"] and not f["pdf_url"]),
            "rows": 0, "ocr_pages": 0, "status": "not a transaction report",
        }
        out_filings.append(entry)
        if not entry["is_278t"]:
            continue
        pdf = ensure_pdf(f)
        if not pdf:
            entry["status"] = "available by request only" if entry["needs_request"] else "PDF not downloaded"
            continue

        digest = file_hash(pdf)
        cache_path = CACHE_DIR / f"{digest}.json"
        used_cache.add(cache_path.name)
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else None
        if args.reparse or not cached or cached.get("hash") != digest or cached.get("version") != PARSER_VERSION:
            print(f"  parsing {pdf.name}", flush=True)
            parsed = parse_pdf(pdf, year_hint=int(f["date"][:4]) if f["date"][:4].isdigit() else None)
            cached = {"hash": digest, "version": PARSER_VERSION, **parsed}
            cache_path.write_text(json.dumps(cached))
        entry.update(rows=len(cached["rows"]), ocr_pages=cached["ocr_pages"],
                     status="parsed" if cached["rows"] else "no rows found (may need OCR or a manual look)")

        posted = datetime.strptime(f["date"], "%Y-%m-%d").date() if re.match(r"\d{4}-\d{2}-\d{2}", f["date"]) else None
        for r in cached["rows"]:
            ticker, company, conf = matcher.match(r["description"])
            klass = asset_class(r["description"])
            reasons = []
            if r["ocr"]:
                reasons.append("read by OCR")
            if not r["type"]:
                reasons.append("unclear buy/sell")
            if r["amount_min"] is None:
                reasons.append("unreadable amount")
            elif not r["amount_exact"]:
                reasons.append("amount adjusted to nearest band")
            trade_date = r["date"]
            if trade_date and posted and not (posted - timedelta(days=730) <= date.fromisoformat(trade_date) <= posted):
                # A trade can't happen after its report was posted, and these
                # reports cover recent months. A date outside that window is an
                # OCR misreading (e.g. 2026 read as 2028) or a bond's maturity
                # date picked up by mistake, so it is dropped rather than shown.
                reasons.append(f"unreadable date (read as {trade_date})")
                trade_date = None
            elif not trade_date:
                reasons.append("unreadable date")
            if ticker and conf < 0.95:
                reasons.append("ticker matched approximately")
            if not ticker and klass in ("stock", "option"):
                unmatched[r["description"]] += 1
            transactions.append({
                "id": f"{filing_id}-{r['page']}-{r['row']}",
                "filing": filing_id,
                "person": f["person"],
                "date": trade_date,
                "posted": f["date"],
                "type": r["type"] or "unknown",
                "asset": r["description"],
                "ticker": ticker,
                "company": company or r["description"],
                "class": klass,
                "amount_min": r["amount_min"],
                "amount_max": r["amount_max"],
                "review": reasons,
                "page": r["page"],
                "row": r["row"],
            })

    # Amended or re-posted reports repeat trades from earlier ones. Keep each
    # trade once, from the most recently posted report that lists it.
    newest_first = sorted(transactions, key=lambda t: t["posted"], reverse=True)
    first_filing, kept = {}, []
    for t in newest_first:
        k = (t["person"], t["date"], norm_key(t["asset"]), t["type"], t["amount_min"])
        owner = first_filing.setdefault(k, t["filing"])
        if owner == t["filing"]:
            kept.append(t)
    dropped = len(transactions) - len(kept)
    transactions = sorted(kept, key=lambda t: (t["date"] or "", t["person"]))
    for e in out_filings:
        e["rows"] = sum(1 for t in transactions if t["filing"] == e["id"]) if e["rows"] else 0

    sectors = lookup_sectors({t["ticker"] for t in transactions if t["ticker"]}, matcher.cik)
    sector_overrides = load_sector_overrides()
    for t in transactions:
        if t["ticker"] in sector_overrides:
            t["sector"] = sector_overrides[t["ticker"]]
        elif t["class"] == "fund":
            t["sector"] = "Funds"
        elif t["ticker"] and t["ticker"] in sectors:
            t["sector"] = sector_for_sic(sectors[t["ticker"]]["sic"])
        elif t["class"] == "bond":
            t["sector"] = "Municipal & other bonds"
        else:
            t["sector"] = "Unclassified"

    for stale in CACHE_DIR.glob("*.json"):
        if stale.name not in used_cache:
            stale.unlink()

    # The roster in oge_watch.py is the source of truth for each official's
    # current role and agency, in the order listed there.
    import oge_watch
    roster = {p["label"]: p for p in oge_watch.PEOPLE}
    order = {p["label"]: i for i, p in enumerate(oge_watch.PEOPLE)}
    people, seen = [], set()
    # Everyone on the roster appears, even with no filings yet, so the page
    # can say so; then anyone found in filings who has since been removed.
    for p in oge_watch.PEOPLE:
        seen.add(p["label"])
        people.append({"name": p["label"], "role": p["role"], "agency": p.get("agency", "")})
    for f in sorted(filings, key=lambda f: order.get(f["person"], 10_000)):
        if f["person"] not in seen:
            seen.add(f["person"])
            r = roster.get(f["person"], {})
            people.append({"name": f["person"], "role": r.get("role", f["role"]),
                           "agency": r.get("agency", f.get("group", ""))})
    for e in out_filings:
        e["agency"] = roster.get(e["person"], {}).get("agency", "")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"),
        "people": people,
        "filings": out_filings,
        "transactions": transactions,
    }, separators=(",", ":")))
    if not EVENTS_PATH.exists():
        EVENTS_PATH.write_text(json.dumps({"events": []}, indent=1))

    with (DATA_DIR / "unmatched.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["count", "description"])
        for desc, n in unmatched.most_common():
            w.writerow([n, desc])

    flagged = sum(1 for t in transactions if t["review"])
    matched = sum(1 for t in transactions if t["ticker"])
    if dropped:
        print(f"\n(Skipped {dropped} trades repeated in amended or re-posted reports.)")
    print(f"\n{len(transactions)} transactions from {sum(1 for e in out_filings if e['rows'])} reports; "
          f"{matched} matched to tickers, {flagged} flagged for review, "
          f"{len(unmatched)} unmatched stock descriptions (see oge_data/unmatched.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
