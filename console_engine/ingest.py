"""
console_engine/ingest.py - Layer 1, step 1: INGEST + NORMALIZE

Reads any source CSV (contractor list, enriched export, scraper output) and
normalizes it to one standard shape, regardless of the source's column names.
Everything downstream (dedup, clean, load) consumes this standard shape.

Standard output columns:
  company, first_name, last_name, email, phone, website, city, state, source_tag

Usage (standalone test):
  python -m console_engine.ingest path/to/list.csv --source "florida_pdf"
"""
import csv, re, sys, os

# Map many possible source column names -> our standard field.
# Lowercased, stripped for matching.
COLUMN_ALIASES = {
    "company":   ["company", "company_name", "business", "business_name", "name", "org", "organization"],
    "first_name":["first_name", "firstname", "first", "fname", "contact_first"],
    "last_name": ["last_name", "lastname", "last", "lname", "contact_last"],
    "email":     ["email", "email_address", "e-mail", "contact_email", "work_email"],
    "phone":     ["phone", "phone_number", "telephone", "tel", "employee_phone", "company_phone", "mobile"],
    "website":   ["website", "web", "url", "site", "domain", "company_url", "searched_domain", "email_domain"],
    "city":      ["city", "town", "locality"],
    "state":     ["state", "region", "region_code", "province"],
}

def _norm_header(h):
    return re.sub(r'[^a-z0-9]', '', (h or '').strip().lower())

def _build_map(headers):
    """Map each standard field to the actual header in this file, if present."""
    norm_headers = { _norm_header(h): h for h in headers }
    field_map = {}
    for std, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            na = _norm_header(a)
            if na in norm_headers:
                field_map[std] = norm_headers[na]
                break
    return field_map

def clean_domain(v):
    v = (v or '').strip().lower()
    v = re.sub(r'^https?://', '', v)
    v = re.sub(r'^www\.', '', v)
    return v.split('/')[0].split('?')[0]

FREE = {"gmail.com","yahoo.com","hotmail.com","aol.com","outlook.com","icloud.com","me.com",
        "aim.com","att.net","comcast.net","bellsouth.net","sbcglobal.net","verizon.net",
        "ymail.com","live.com","msn.com"}

def ingest_file(path, source_tag):
    """Read one CSV, return list of normalized dict rows."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        fmap = _build_map(headers)
        rows = []
        for raw in reader:
            def get(std):
                col = fmap.get(std)
                return (raw.get(col) or '').strip() if col else ''
            email = get("email").lower()
            website = get("website").strip()
            # if website came from an email_domain/searched_domain, clean it;
            # if it's a free provider used as "domain", treat as no real website
            web_dom = clean_domain(website)
            if web_dom in FREE:
                web_dom = ''
            # if no website but email is corporate, derive website from email domain
            if not web_dom and email and '@' in email:
                edom = email.split('@')[-1]
                if edom not in FREE:
                    web_dom = edom
            rows.append({
                "company":    get("company"),
                "first_name": get("first_name"),
                "last_name":  get("last_name"),
                "email":      email,
                "phone":      get("phone"),
                "website":    web_dom,
                "city":       get("city"),
                "state":      get("state"),
                "source_tag": source_tag,
            })
        return rows, fmap, headers

def main():
    if len(sys.argv) < 2:
        print("usage: python -m console_engine.ingest <file.csv> [--source TAG]")
        return
    path = sys.argv[1]
    tag = "manual"
    if "--source" in sys.argv:
        tag = sys.argv[sys.argv.index("--source")+1]
    rows, fmap, headers = ingest_file(path, tag)
    print(f"File: {path}")
    print(f"Source headers detected: {headers}")
    print(f"Column mapping (standard -> source): {fmap}")
    print(f"Rows ingested: {len(rows)}")
    with_email = sum(1 for r in rows if r['email'])
    with_web = sum(1 for r in rows if r['website'])
    print(f"  with email: {with_email}")
    print(f"  with website: {with_web}")
    print("\nSample normalized rows:")
    for r in rows[:5]:
        print(f"  {r['company'][:25]:25} | {r['email'][:28]:28} | {r['website'][:22]:22} | {r['city']}")

if __name__ == "__main__":
    main()
