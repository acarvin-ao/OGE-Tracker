#!/usr/bin/env python3
"""
oge_watch.py: watch the U.S. Office of Government Ethics (OGE) for new
financial disclosure filings by President Trump and his second-term cabinet
secretaries (current and former).

How it works
------------
OGE's "Officials' Individual Disclosures Search Collection" page is backed by
a public JSON API (a DataTables server-side endpoint):

    https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest

For each person in PEOPLE, the script queries that API, filters the results
to the right person, and compares them against what it saw last time. Any
new filing is:
  * printed to the console,
  * appended to oge_data/filings.csv,
  * downloaded as a PDF into oge_data/pdfs/<person>/ (when OGE posts it
    directly rather than requiring a Form 201 request),
  * optionally sent as a notification (see --notify-url).

Usage
-----
    python oge_watch.py                  # check once
    python oge_watch.py --loop 6         # check every 6 hours, forever
    python oge_watch.py --only-278t      # only care about transaction reports
    python oge_watch.py --no-download    # list filings without fetching PDFs
    python oge_watch.py --notify-url https://ntfy.sh/your-secret-topic
    python oge_watch.py --debug          # print one raw API row per person

The first run records everything already posted (it will mark it all as
"new" once); later runs only report filings that appeared since.

Requirements: Python 3.9+, standard library only.

Legal note
----------
These reports are released under the Ethics in Government Act. 5 U.S.C.
13107(c) makes it unlawful to obtain or use them for any unlawful purpose,
for any commercial purpose other than by news and communications media for
dissemination to the general public, for determining anyone's credit rating,
or for soliciting money. OGE's site asks visitors to affirm this before
browsing. Personal research and tracking are fine; reselling the data is not.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Who to watch
# --------------------------------------------------------------------------
# OGE lists names as "Last, First Middle". "search" is sent to OGE's name
# column filter (a substring match), and "first" is a regex applied to the
# part after the comma, to weed out other people who share the last name.
#
# Roster as of November 2026: the President plus the heads of the 15
# executive departments, including the four who have left. Edit freely.
PEOPLE = [
    {"label": "Donald J. Trump", "role": "President", "search": "Trump", "first": r"^Donald"},
    # Current department heads
    {"label": "Marco Rubio", "role": "Secretary of State", "search": "Rubio", "first": r"^Marco"},
    {"label": "Scott Bessent", "role": "Secretary of the Treasury", "search": "Bessent", "first": r"^(Scott|William)"},
    {"label": "Pete Hegseth", "role": "Secretary of Defense / War", "search": "Hegseth", "first": r"^Pete"},
    {"label": "Todd Blanche", "role": "Attorney General (from Aug 2026)", "search": "Blanche", "first": r"^Todd"},
    {"label": "Doug Burgum", "role": "Secretary of the Interior", "search": "Burgum", "first": r"^Doug"},
    {"label": "Brooke Rollins", "role": "Secretary of Agriculture", "search": "Rollins", "first": r"^Brooke"},
    {"label": "Howard Lutnick", "role": "Secretary of Commerce", "search": "Lutnick", "first": r"^Howard"},
    {"label": "Keith Sonderling", "role": "Secretary of Labor (from Oct 2026)", "search": "Sonderling", "first": r"^Keith"},
    {"label": "Robert F. Kennedy Jr.", "role": "Secretary of Health and Human Services", "search": "Kennedy", "first": r"^Robert"},
    {"label": "Scott Turner", "role": "Secretary of Housing and Urban Development", "search": "Turner", "first": r"^(Eric|Scott)"},
    {"label": "Sean Duffy", "role": "Secretary of Transportation", "search": "Duffy", "first": r"^Sean"},
    {"label": "Chris Wright", "role": "Secretary of Energy", "search": "Wright", "first": r"^Chris"},
    {"label": "Linda McMahon", "role": "Secretary of Education", "search": "McMahon", "first": r"^Linda"},
    {"label": "Doug Collins", "role": "Secretary of Veterans Affairs", "search": "Collins", "first": r"^Doug"},
    {"label": "Markwayne Mullin", "role": "Secretary of Homeland Security", "search": "Mullin", "first": r"^Markwayne"},
    # Former department heads (second term)
    {"label": "Kristi Noem", "role": "Former Secretary of Homeland Security", "search": "Noem", "first": r"^Kristi"},
    {"label": "Pam Bondi", "role": "Former Attorney General", "search": "Bondi", "first": r"^Pam"},
    {"label": "Lori Chavez-DeRemer", "role": "Former Secretary of Labor", "search": "DeRemer", "first": r"^Lori"},
    # Add anyone else the same way, e.g.:
    # {"label": "JD Vance", "role": "Vice President", "search": "Vance", "first": r"^(James|JD)"},
]

# Ignore filings dated before this (keeps first-term and pre-nomination
# documents out). Nominee reports for the second term were filed late 2024.
SINCE = "2024-11-01"

# --------------------------------------------------------------------------
# OGE API details
# --------------------------------------------------------------------------
OGE_HOST = "https://extapps2.oge.gov"
DEFAULT_API = OGE_HOST + "/201/Presiden.nsf/API.xsp/v3/rest"
INDEX_PAGE = "https://www.oge.gov/web/oge.nsf/Officials%20Individual%20Disclosures%20Search%20Collection?OpenForm"
COLUMNS = ["docDate", "title", "type", "name", "agency", "level"]
NAME_COL = 3
PAGE_SIZE = 100
USER_AGENT = "oge-watch/1.0 (personal research script)"
DATA_DIR = Path("oge_data")


def http_get(url: str, accept: str = "application/json", timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def discover_api_url() -> str:
    """OGE has bumped the API version before (v2 -> v3). Read the version the
    public search page currently uses, falling back to v3."""
    try:
        page = http_get(INDEX_PAGE, accept="text/html").decode("utf-8", "replace")
        found = re.findall(r"https://extapps2\.oge\.gov/201/Presiden\.nsf/API\.xsp/v(\d+)/rest", page)
        if found:
            version = max(int(v) for v in found)
            return f"{OGE_HOST}/201/Presiden.nsf/API.xsp/v{version}/rest"
    except Exception as err:  # network hiccup, page redesign, etc.
        print(f"  (couldn't auto-detect API version: {err}; using v3)")
    return DEFAULT_API


def build_query(api_url: str, name_search: str, start: int, length: int) -> str:
    # This mimics what the OGE search page's DataTables widget sends. The
    # global search box is ignored by the server; the per-column filter on
    # the name column is what actually works. GET only (POST returns 400).
    p: list[tuple[str, str]] = [("draw", "1")]
    for i, col in enumerate(COLUMNS):
        p += [
            (f"columns[{i}][data]", col),
            (f"columns[{i}][name]", ""),
            (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"),
            (f"columns[{i}][search][value]", name_search if i == NAME_COL else ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    p += [
        ("order[0][column]", "0"),
        ("order[0][dir]", "desc"),
        ("start", str(start)),
        ("length", str(length)),
        ("search[value]", ""),
        ("search[regex]", "false"),
    ]
    return api_url + "?" + urllib.parse.urlencode(p)


def clean(value) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_date(raw: str) -> str:
    raw = clean(raw)
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.split()[0], fmt).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            pass
    return raw


def normalize_row(row: dict) -> dict:
    raw_type = str(row.get("type") or "")
    type_text = clean(raw_type)
    href = re.search(r"""href=['"]([^'"]+)['"]""", raw_type)
    link = urllib.parse.urljoin(OGE_HOST + "/", html.unescape(href.group(1))) if href else ""
    needs_request = "request this document" in type_text.lower() or "201%20Request" in link or "201+Request" in link
    path = urllib.parse.unquote(link.split("?")[0])
    is_pdf = bool(link) and not needs_request and path.lower().endswith(".pdf")
    return {
        "date": normalize_date(row.get("docDate", "")),
        "name": clean(row.get("name")),
        "agency": clean(row.get("agency")),
        "position": clean(row.get("title")),
        "doc_type": re.sub(r"\s*\(Request this Document\)\s*", " ", type_text, flags=re.I).strip(),
        "is_278t": bool(re.search(r"278\s*-?\s*T|Transaction", type_text, re.I)),
        "pdf_url": link if is_pdf else "",
        "request_url": link if needs_request else "",
    }


def fetch_person(api_url: str, person: dict, debug: bool = False) -> list[dict]:
    rows, start = [], 0
    while True:
        payload = json.loads(http_get(build_query(api_url, person["search"], start, PAGE_SIZE)))
        batch = payload.get("data") or []
        if debug and start == 0 and batch:
            print("  raw row:", json.dumps(batch[0])[:600])
        rows.extend(batch)
        total = int(payload.get("recordsFiltered") or 0)
        start += PAGE_SIZE
        if not batch or start >= total:
            break
        time.sleep(1)  # be polite to OGE's server

    first_re = re.compile(person["first"], re.I)
    results = []
    for raw in rows:
        f = normalize_row(raw)
        last, _, first = f["name"].partition(",")
        # Whole-word match, so "Trump" doesn't catch "Trumpington" but
        # "DeRemer" still catches "Chavez-DeRemer".
        if not re.search(r"\b" + re.escape(person["search"]) + r"\b", last, re.I):
            continue
        if not first_re.search(first.strip()):
            continue
        if f["date"] and f["date"] < SINCE:
            continue
        f["person"] = person["label"]
        f["role"] = person["role"]
        results.append(f)
    return results


def filing_key(f: dict) -> str:
    return "|".join([f["person"], f["date"], f["doc_type"], f["pdf_url"] or f["request_url"]])


def safe_name(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", text).strip("_")


def download_pdf(f: dict) -> str:
    """Domino servers are picky about how spaces and "$FILE" are encoded,
    so try a few spellings of the same URL."""
    url = f["pdf_url"]
    candidates = list(dict.fromkeys([
        url,
        url.replace("/PAS+Index/", "/PAS%20Index/"),
        url.replace("/$FILE/", "/%24FILE/"),
        url.replace("/PAS+Index/", "/PAS%20Index/").replace("/$FILE/", "/%24FILE/"),
    ]))
    original = Path(urllib.parse.unquote(url.split("?")[0])).stem
    filename = safe_name(f"{f['date']}_{f['doc_type']}_{original}")[:150] + ".pdf"
    dest = DATA_DIR / "pdfs" / safe_name(f["person"]) / filename
    if dest.exists():
        return str(dest)
    for candidate in candidates:
        try:
            data = http_get(candidate, accept="application/pdf,*/*", timeout=120)
            if data[:5] == b"%PDF-":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                return str(dest)
        except urllib.error.URLError:
            continue
    return ""


def notify(url: str, message: str) -> None:
    """Plain-text POST. Works as-is with ntfy.sh topics; adapt for Slack,
    Discord, email, etc."""
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST",
                                     headers={"User-Agent": USER_AGENT, "Title": "New OGE filing(s)"})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as err:
        print(f"  (notification failed: {err})")


CSV_FIELDS = ["first_seen", "person", "role", "date", "doc_type", "is_278t",
              "name", "position", "agency", "pdf_url", "request_url", "local_pdf"]


def run_once(args) -> int:
    DATA_DIR.mkdir(exist_ok=True)
    state_path = DATA_DIR / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": []}
    seen = set(state["seen"])
    first_run = not seen

    api_url = args.api_url or discover_api_url()
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Checking OGE ({api_url})")

    new_filings, errors = [], []
    for person in PEOPLE:
        try:
            filings = fetch_person(api_url, person, debug=args.debug)
        except Exception as err:
            errors.append(person["label"])
            print(f"  ! {person['label']}: {err}")
            continue
        if args.only_278t:
            filings = [f for f in filings if f["is_278t"]]
        fresh = [f for f in filings if filing_key(f) not in seen]
        print(f"  {person['label']:<24} {len(filings):>3} filings since {SINCE}, {len(fresh)} new")
        new_filings.extend(fresh)
        time.sleep(1)

    new_filings.sort(key=lambda f: f["date"])
    csv_path = DATA_DIR / "filings.csv"
    write_header = not csv_path.exists()
    now = datetime.now().isoformat(timespec="seconds")
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for f in new_filings:
            f["first_seen"] = now
            f["local_pdf"] = download_pdf(f) if (f["pdf_url"] and not args.no_download) else ""
            writer.writerow(f)
            seen.add(filing_key(f))

    state["seen"] = sorted(seen)
    state["last_run"] = now
    state_path.write_text(json.dumps(state, indent=1))

    if new_filings:
        print(f"\n{len(new_filings)} new filing(s):")
        lines = []
        for f in new_filings:
            where = f["local_pdf"] or f["pdf_url"] or "available by Form 201 request: " + f["request_url"]
            line = f"  {f['date']}  {f['person']}: {f['doc_type']}"
            lines.append(line)
            print(line + f"\n      {where}")
        if args.notify_url and not first_run:
            notify(args.notify_url, "\n".join(lines))
    else:
        print("\nNo new filings.")
    if first_run:
        print("\n(First run: everything already posted was recorded as a baseline.)")
    return 1 if errors and len(errors) == len(PEOPLE) else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch OGE for new filings by Trump and his cabinet secretaries.")
    ap.add_argument("--loop", type=float, metavar="HOURS", help="keep running, checking every HOURS hours")
    ap.add_argument("--only-278t", action="store_true", help="only track periodic transaction reports (278-T)")
    ap.add_argument("--no-download", action="store_true", help="don't download PDFs")
    ap.add_argument("--notify-url", help="POST a plain-text alert here when something new appears (e.g. an ntfy.sh topic)")
    ap.add_argument("--api-url", help="override the OGE API URL instead of auto-detecting it")
    ap.add_argument("--debug", action="store_true", help="print a raw API row per person (for troubleshooting)")
    args = ap.parse_args()

    if not args.loop:
        sys.exit(run_once(args))
    while True:
        try:
            run_once(args)
        except Exception as err:
            print(f"Run failed: {err}")
        print(f"Next check in {args.loop:g} hour(s).\n")
        time.sleep(args.loop * 3600)


if __name__ == "__main__":
    main()
