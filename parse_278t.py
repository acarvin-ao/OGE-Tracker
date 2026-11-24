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
import os
import re
import sys
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
PARSER_VERSION = 3  # bump to force re-parsing after changing the logic

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

def page_texts(pdf_path: Path) -> list[tuple[str, bool]]:
    """Return (text, was_ocr) for each page."""
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len(text.strip()) >= 80:
                out.append((text, False))
                continue
            out.append((ocr_page(page), True))
    return out


_ocr_warned = False


def ocr_page(page) -> str:
    global _ocr_warned
    try:
        import pytesseract
        image = page.to_image(resolution=300).original
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

# The date column is always followed by the Yes/No "notification received
# over 30 days ago" column. Anchoring on that pair avoids confusing it with
# bond maturity dates inside descriptions.
ROW_RE = re.compile(
    r"^\s*(?P<num>\d{1,4})[.)]?\s+(?P<desc>.+?)\s+"
    r"(?P<type>[A-Za-z0-9'’:.()\[\]|-]{3,20}(?:\s*\((?:partial|full)\))?)\s+"
    r"(?P<date>\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})\s+"
    r"(?P<notif>[YVNynv][A-Za-z0-9]{0,3})\s+"
    r"(?P<amount>(?:over|0ver|\$|S).+?)\s*$",
    re.I,
)
HEADER_HINTS = ("DESCRIPTION", "NOTIFICATION", "DAYS AGO", "Page ", "Privacy Act",
                "OGE Form", "Endnotes", "Summary of Contents", "Filer's Information")


def classify_type(token: str) -> str | None:
    t = re.sub(r"[^a-z]", "", token.lower())
    if not t:
        return None
    if "exch" in t:
        return "exchange"
    if "rch" in t or "urcha" in t:
        return "buy"
    if t.startswith(("sal", "sa1", "5al")) or "sale" in t:
        return "sell"
    buy = SequenceMatcher(None, t, "purchase").ratio()
    sell = SequenceMatcher(None, t, "sale").ratio()
    exch = SequenceMatcher(None, t, "exchange").ratio()
    best = max(buy, sell, exch)
    if best < 0.5:
        return None
    return "buy" if best == buy else "sell" if best == sell else "exchange"


def parse_amount(text: str) -> tuple[int, int | None, bool]:
    """Returns (min, max, exact_band_match). max None means 'over'."""
    cleaned = re.sub(r"[\s\u00a0•·]", "", text).replace("O", "0").replace("o", "0")
    if re.match(r"(?i)0ver", cleaned):
        nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{3,}", cleaned)]
        floor = nums[0] + 1 if nums else 50_000_001
        band = min(BANDS, key=lambda b: abs(b[0] - floor))
        return band[0], None if band[1] is None else band[1], band[0] == floor
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{3,}", cleaned)]
    if not nums:
        return 0, 0, False
    low = nums[0]
    band = min(BANDS, key=lambda b: abs(b[0] - low))
    exact = band[0] == low and (len(nums) < 2 or band[1] == nums[1])
    return band[0], band[1], exact


def to_iso(date_text: str) -> str | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", re.sub(r"\s", "", date_text))
    if not m:
        return None
    mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yy < 100:
        yy += 2000
    try:
        return date(yy, mm, dd).isoformat()
    except ValueError:
        return None


def parse_pdf(pdf_path: Path) -> dict:
    rows, ocr_pages, empty_pages = [], 0, 0
    pages = page_texts(pdf_path)
    for page_no, (text, was_ocr) in enumerate(pages, start=1):
        ocr_pages += was_ocr
        empty_pages += not text.strip()
        last = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = ROW_RE.match(line)
            if m:
                low, high, exact = parse_amount(m.group("amount"))
                last = {
                    "row": int(m.group("num")),
                    "page": page_no,
                    "description": m.group("desc").strip(),
                    "type_raw": m.group("type"),
                    "type": classify_type(m.group("type")),
                    "date": to_iso(m.group("date")),
                    "amount_min": low,
                    "amount_max": high,
                    "amount_exact": exact,
                    "ocr": was_ocr,
                }
                rows.append(last)
                continue
            # A long description can wrap onto the next line. Attach short
            # follow-on lines to the row above, but never boilerplate.
            if (last and len(line) <= 60 and not any(h.lower() in line.lower() for h in HEADER_HINTS)
                    and not re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", line)
                    and last.get("_wraps", 0) < 2):
                last["description"] += " " + line
                last["_wraps"] = last.get("_wraps", 0) + 1
            else:
                last = None
    for r in rows:
        r.pop("_wraps", None)
    return {"rows": rows, "pages": len(pages), "ocr_pages": ocr_pages, "empty_pages": empty_pages}


# --------------------------------------------------------------------------
# Ticker matching
# --------------------------------------------------------------------------

SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COS", "COMPANY", "COMPANIES",
    "LTD", "LIMITED", "PLC", "LLC", "LP", "HOLDINGS", "HOLDING", "HLDGS", "HLDG", "GROUP", "GRP",
    "THE", "CL", "CLASS", "A", "B", "C", "COM", "COMMON", "STOCK", "SHS", "SHARES", "ORD", "NEW",
    "SA", "NV", "AG", "SE", "ADR", "ADS", "SPONSORED", "SPON", "DEL", "DE", "REG", "NPV", "PAR",
}
ALIASES = {"INTL": "INTERNATIONAL", "TECH": "TECHNOLOGIES", "TECHNOLOGY": "TECHNOLOGIES",
           "SYS": "SYSTEMS", "PHARMA": "PHARMACEUTICALS", "FINL": "FINANCIAL", "BK": "BANK",
           "SVCS": "SERVICES", "MTRS": "MOTORS", "&": "AND", "COMMUNICATIONS": "COMMUNICATIONS"}


def norm_tokens(name: str) -> list[str]:
    text = re.sub(r"\(.*?\)", " ", name.upper())
    # Share-class suffixes, including OCR mangling like "INC Cl A" -> "INCCIA"
    text = re.sub(r"INC\s*C[LI1]\s*[AB]\b", "INC", text)
    text = re.sub(r"\bC[LI1]\s*[AB]\b", " ", text)
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
        self.by_ticker = {}
        self.by_key = {}
        self.by_tokenset = {}
        self.by_first = {}
        for item in sec.values():
            ticker, title = item["ticker"].upper(), item["title"]
            self.by_ticker.setdefault(ticker, title)
            toks = norm_tokens(title)
            if not toks:
                continue
            key = " ".join(toks)
            self.by_key.setdefault(key, (ticker, title))
            self.by_tokenset.setdefault(" ".join(sorted(toks)), (ticker, title))
            self.by_first.setdefault(toks[0], []).append((key, ticker, title))

    def match(self, desc: str) -> tuple[str, str, float]:
        """Returns (ticker, company name, confidence 0-1)."""
        low = desc.lower()
        for o in self.overrides:
            if o["description"].lower() in low:
                return o["ticker"].upper(), o.get("company") or self.by_ticker.get(o["ticker"].upper(), desc), 1.0
        explicit = re.search(r"\(([A-Z]{1,5}(?:[.\-][A-Z])?)\)", desc)
        if explicit and explicit.group(1) in self.by_ticker:
            return explicit.group(1), self.by_ticker[explicit.group(1)], 1.0
        toks = norm_tokens(desc)
        if not toks:
            return "", "", 0.0
        key = " ".join(toks)
        if key in self.by_key:
            return self.by_key[key][0], self.by_key[key][1], 0.98
        tset = " ".join(sorted(toks))
        if tset in self.by_tokenset:
            return self.by_tokenset[tset][0], self.by_tokenset[tset][1], 0.95
        # Fuzzy: compare against companies sharing a first word (or, for
        # names like "LAUDER ESTEE", any word).
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
# Main
# --------------------------------------------------------------------------

def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ensure_pdf(f: dict) -> Path | None:
    local = Path(f.get("local_pdf") or "")
    if local.is_file():
        return local
    if not f.get("pdf_url"):
        return None
    try:  # the watcher may have recorded a filing whose download failed
        import oge_watch
        got = oge_watch.download_pdf(f)
        return Path(got) if got else None
    except Exception:
        return None


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

    out_filings, transactions, unmatched = [], [], Counter()
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

        cache_path = CACHE_DIR / (pdf.stem[:100] + ".json")
        digest = file_hash(pdf)
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else None
        if args.reparse or not cached or cached.get("hash") != digest or cached.get("version") != PARSER_VERSION:
            print(f"  parsing {pdf.name}")
            parsed = parse_pdf(pdf)
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
            if not r["amount_exact"]:
                reasons.append("amount adjusted to nearest band")
            if not r["date"]:
                reasons.append("unreadable date")
            elif posted and not (posted - timedelta(days=400) <= date.fromisoformat(r["date"]) <= posted):
                reasons.append("date outside the filing window")
            if ticker and conf < 0.95:
                reasons.append("ticker matched approximately")
            if not ticker and klass in ("stock", "option"):
                unmatched[r["description"]] += 1
            transactions.append({
                "id": f"{filing_id}-{r['page']}-{r['row']}",
                "filing": filing_id,
                "person": f["person"],
                "date": r["date"],
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

    people = []
    seen = set()
    for f in filings:
        if f["person"] not in seen:
            seen.add(f["person"])
            people.append({"name": f["person"], "role": f["role"]})

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
    print(f"\n{len(transactions)} transactions from {sum(1 for e in out_filings if e['rows'])} reports; "
          f"{matched} matched to tickers, {flagged} flagged for review, "
          f"{len(unmatched)} unmatched stock descriptions (see oge_data/unmatched.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
