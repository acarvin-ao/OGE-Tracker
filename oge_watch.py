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
# "agency" groups officials on the web page.
#
# Only Senate-confirmed officials (and nominees) are listed, because those
# are the reports OGE posts. Acting officials who aren't confirmed to any
# post usually file with their own agency instead, available by Form 201
# request. Roster as of November 2026. Entries marked "unverified" were not
# re-checked against a recent source; if a name is wrong the watcher simply
# finds no filings for it.
def P(label, role, agency, search, first):
    return {"label": label, "role": role, "agency": agency, "search": search, "first": first}


PEOPLE = [
    P("Donald J. Trump", "President", "White House", "Trump", r"^Donald"),

    # ---- Cabinet secretaries (current) ----
    P("Marco Rubio", "Secretary of State", "State", "Rubio", r"^Marco"),
    P("Scott Bessent", "Secretary of the Treasury", "Treasury", "Bessent", r"^(Scott|William)"),
    P("Pete Hegseth", "Secretary of Defense / War", "Defense", "Hegseth", r"^Pete"),
    P("Todd Blanche", "Attorney General (from Aug 2026); earlier Deputy Attorney General", "Justice", "Blanche", r"^Todd"),
    P("Doug Burgum", "Secretary of the Interior", "Interior", "Burgum", r"^Doug"),
    P("Brooke Rollins", "Secretary of Agriculture", "Agriculture", "Rollins", r"^Brooke"),
    P("Howard Lutnick", "Secretary of Commerce", "Commerce", "Lutnick", r"^Howard"),
    P("Keith Sonderling", "Secretary of Labor (from Oct 2026); earlier Deputy Secretary", "Labor", "Sonderling", r"^Keith"),
    P("Robert F. Kennedy Jr.", "Secretary of Health and Human Services", "Health and Human Services", "Kennedy", r"^Robert"),
    P("Scott Turner", "Secretary of Housing and Urban Development", "Housing and Urban Development", "Turner", r"^(Eric|Scott)"),
    P("Sean Duffy", "Secretary of Transportation", "Transportation", "Duffy", r"^Sean"),
    P("Chris Wright", "Secretary of Energy", "Energy", "Wright", r"^Chris"),
    P("Linda McMahon", "Secretary of Education", "Education", "McMahon", r"^Linda"),
    P("Doug Collins", "Secretary of Veterans Affairs", "Veterans Affairs", "Collins", r"^Doug"),
    P("Markwayne Mullin", "Secretary of Homeland Security", "Homeland Security", "Mullin", r"^Markwayne"),
    # ---- Cabinet secretaries (former, second term) ----
    P("Kristi Noem", "Former Secretary of Homeland Security", "Homeland Security", "Noem", r"^Kristi"),
    P("Pam Bondi", "Former Attorney General", "Justice", "Bondi", r"^Pam"),
    P("Lori Chavez-DeRemer", "Former Secretary of Labor", "Labor", "DeRemer", r"^Lori"),

    # ---- Defense ----
    P("Steve Feinberg", "Deputy Secretary of Defense / War", "Defense", "Feinberg", r"^Steph|^Steve"),
    P("Elbridge Colby", "Under Secretary for Policy", "Defense", "Colby", r"^Elbridge"),
    P("Michael Duffey", "Under Secretary for Acquisition and Sustainment", "Defense", "Duffey", r"^Michael"),
    P("Emil Michael", "Under Secretary for Research and Engineering", "Defense", "Michael", r"^Emil"),
    P("Anthony Tata", "Under Secretary for Personnel and Readiness", "Defense", "Tata", r"^Anthony"),
    P("Bradley Hansell", "Under Secretary for Intelligence and Security", "Defense", "Hansell", r"^Brad"),
    P("Jules Hurst", "Under Secretary (Comptroller) / CFO", "Defense", "Hurst", r"^Jules"),

    # ---- Justice ----
    P("Stanley Woodward", "Associate Attorney General", "Justice", "Woodward", r"^Stanley"),
    P("D. John Sauer", "Solicitor General (unverified)", "Justice", "Sauer", r"^(Dean|D\.? ?John|John)"),
    P("Kash Patel", "Director, FBI", "Justice", "Patel", r"^Kash"),
    P("Robert Cekada", "Director, ATF", "Justice", "Cekada", r"^Robert"),
    P("Terrance Cole", "Administrator, DEA (unverified)", "Justice", "Cole", r"^Terr"),
    P("John Eisenberg", "Assistant Attorney General, National Security Division (unverified)", "Justice", "Eisenberg", r"^John"),
    P("Adam Candeub", "Nominee, Assistant Attorney General for Antitrust", "Justice", "Candeub", r"^Adam"),
    P("Gail Slater", "Former Assistant Attorney General for Antitrust", "Justice", "Slater", r"^(Abigail|Gail)"),

    # ---- Treasury ----
    P("Francis Brooke", "Deputy Secretary of the Treasury", "Treasury", "Brooke", r"^Francis"),
    P("Erin Browne", "Under Secretary for International Affairs", "Treasury", "Browne", r"^Erin"),
    P("Jonathan McKernan", "Under Secretary for Domestic Finance", "Treasury", "McKernan", r"^Jonathan"),
    P("John Hurley", "Under Secretary for Terrorism and Financial Intelligence", "Treasury", "Hurley", r"^John"),

    # ---- Energy ----
    P("James Danly", "Deputy Secretary of Energy", "Energy", "Danly", r"^James"),
    P("Kyle Haustveit", "Under Secretary of Energy", "Energy", "Haustveit", r"^Kyle"),
    P("Dario Gil", "Under Secretary for Science", "Energy", "Gil", r"^Dar"),
    P("Brandon Williams", "Under Secretary for Nuclear Security / NNSA Administrator (unverified)", "Energy", "Williams", r"^Brandon"),

    # ---- Commerce ----
    P("Paul Dabbar", "Deputy Secretary of Commerce", "Commerce", "Dabbar", r"^Paul"),
    P("Jeffrey Kessler", "Under Secretary for Industry and Security (export controls)", "Commerce", "Kessler", r"^Jeff"),
    P("William Kimmitt", "Under Secretary for International Trade (unverified)", "Commerce", "Kimmitt", r"^William"),
    P("Neil Jacobs", "Under Secretary for Oceans and Atmosphere / NOAA (unverified)", "Commerce", "Jacobs", r"^Neil"),
    P("John Squires", "Under Secretary for Intellectual Property / USPTO (unverified)", "Commerce", "Squires", r"^John"),

    # ---- Homeland Security ----
    P("Troy Edgar", "Deputy Secretary of Homeland Security", "Homeland Security", "Edgar", r"^Troy"),
    P("Pedro Allende", "Under Secretary for Science and Technology", "Homeland Security", "Allende", r"^Pedro"),
    P("Matthew Kozma", "Under Secretary for Intelligence and Analysis", "Homeland Security", "Kozma", r"^Matt"),
    P("Rodney Scott", "Commissioner, Customs and Border Protection", "Homeland Security", "Scott", r"^Rodney"),
    P("Joseph Edlow", "Director, Citizenship and Immigration Services", "Homeland Security", "Edlow", r"^Joseph"),
    P("Cameron Hamilton", "Administrator, FEMA", "Homeland Security", "Hamilton", r"^Cameron"),

    # ---- Office of Management and Budget ----
    P("Russell Vought", "Director, OMB", "OMB", "Vought", r"^Russ"),
    P("Hal Duncan", "Deputy Director, OMB", "OMB", "Duncan", r"^(Harold|Hal)"),
    P("Eric Ueland", "Deputy Director for Management, OMB", "OMB", "Ueland", r"^Eric"),
    P("Dan Bishop", "Former Deputy Director, OMB", "OMB", "Bishop", r"^(James|Dan)"),
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
        f["group"] = person.get("agency", "")   # roster grouping; "agency" is OGE's own field
        results.append(f)
    return results


def filing_key(f: dict) -> str:
    return "|".join([f["person"], f["date"], f["doc_type"], f["pdf_url"] or f["request_url"]])


def safe_name(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", text).strip("_")


def pdf_path(f: dict) -> Path:
    """Where a filing's PDF is stored."""
    original = Path(urllib.parse.unquote(f["pdf_url"].split("?")[0])).stem
    filename = safe_name(f"{f['date']}_{f['doc_type']}_{original}")[:150] + ".pdf"
    return DATA_DIR / "pdfs" / safe_name(f["person"]) / filename


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
    dest = pdf_path(f)
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


CSV_FIELDS = ["first_seen", "person", "role", "group", "date", "doc_type", "is_278t",
              "name", "position", "agency", "pdf_url", "request_url", "local_pdf"]


def migrate_csv(csv_path: Path) -> None:
    """Older versions wrote filings.csv without some columns. Rewrite it with
    the current columns so new rows line up, filling in what we can."""
    if not csv_path.exists():
        return
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames == CSV_FIELDS:
            return
        rows = list(reader)
    groups = {p["label"]: p.get("agency", "") for p in PEOPLE}
    for r in rows:
        r["group"] = r.get("group") or groups.get(r.get("person", ""), "")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
    migrate_csv(csv_path)
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
