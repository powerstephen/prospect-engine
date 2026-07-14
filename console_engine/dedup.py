"""
console_engine/dedup.py - Layer 1, step 2: CONSOLIDATE + DEDUP

Takes normalized rows (from ingest) across one or more sources, merges them,
and removes duplicates on three fronts:
  1. within the combined pool (same email or same real domain appearing twice)
  2. against the live DB (contacts already in Supabase)
  3. against Instantly (rows where in_instantly is true)

Dedup keys: exact email (lowercased) OR real corporate domain. Free-provider
domains (gmail etc) are NOT used as a domain key - only their exact email counts.

Runs on your machine using the same Supabase pattern as your other scripts:
reads SUPABASE_URL + SUPABASE_SERVICE_KEY from env.

Standalone test with a DB export CSV instead of live query:
  python -m console_engine.dedup --db-csv db_export.csv --sources listA.csv,listB.csv
"""
import os, sys, csv, re

from console_engine.ingest import ingest_file, clean_domain, FREE

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://neonmrgszujadgfidlbj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def _real_dom(email, website):
    """Return the corporate domain for dedup, or '' if only a free/no domain."""
    d = clean_domain(website)
    if d and d not in FREE:
        return d
    if email and '@' in email:
        ed = email.split('@')[-1]
        if ed not in FREE:
            return ed
    return ''

def load_db_keys_live():
    """Query Supabase for existing emails + domains + in_instantly emails/domains."""
    import httpx
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    emails, domains = set(), set()
    offset, page = 0, 1000
    with httpx.Client(timeout=30) as c:
        while True:
            r = c.get(f"{SUPABASE_URL}/rest/v1/contacts",
                      headers=headers,
                      params={"select": "email,website", "limit": str(page), "offset": str(offset)})
            batch = r.json()
            if isinstance(batch, dict):
                raise RuntimeError(f"Supabase error: {batch}")
            if not batch:
                break
            for row in batch:
                e = (row.get("email") or "").strip().lower()
                if e and e != "null" and "@" in e:
                    emails.add(e)
                d = _real_dom(e, row.get("website"))
                if d:
                    domains.add(d)
            if len(batch) < page:
                break
            offset += page
    return emails, domains

def load_db_keys_csv(path):
    """Load DB keys from an exported CSV (fallback / offline test)."""
    emails, domains = set(), set()
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            e = (row.get("email") or "").strip().lower()
            if e and e != "null" and "@" in e:
                emails.add(e)
            d = _real_dom(e, row.get("website") or row.get("domain"))
            if d:
                domains.add(d)
    return emails, domains

def dedup(all_rows, db_emails, db_domains):
    """Return (net_new, stats). all_rows = combined normalized rows."""
    seen_email, seen_dom = set(), set()
    net_new = []
    dropped_db = dropped_within = 0
    for r in all_rows:
        e = r["email"]
        d = _real_dom(e, r["website"])
        # against DB
        if (e and e in db_emails) or (d and d in db_domains):
            dropped_db += 1
            continue
        # within pool
        if e and e in seen_email:
            dropped_within += 1
            continue
        if d and d in seen_dom:
            dropped_within += 1
            continue
        if e: seen_email.add(e)
        if d: seen_dom.add(d)
        net_new.append(r)
    return net_new, {
        "input": len(all_rows),
        "dropped_already_in_db": dropped_db,
        "dropped_within_pool": dropped_within,
        "net_new": len(net_new),
    }

def main():
    args = sys.argv
    def opt(flag, default=None):
        return args[args.index(flag)+1] if flag in args else default

    sources = (opt("--sources") or "").split(",") if opt("--sources") else []
    sources = [s for s in sources if s.strip()]
    db_csv = opt("--db-csv")

    if not sources:
        print("usage: python -m console_engine.dedup --sources a.csv,b.csv [--db-csv db.csv]")
        return

    # ingest all sources
    all_rows = []
    for s in sources:
        rows, _, _ = ingest_file(s.strip(), source_tag=os.path.basename(s).strip())
        all_rows.extend(rows)
        print(f"ingested {len(rows)} from {s}")

    # DB keys - live if key present, else CSV
    if db_csv:
        db_emails, db_domains = load_db_keys_csv(db_csv)
        print(f"DB keys from CSV: {len(db_emails)} emails, {len(db_domains)} domains")
    elif SUPABASE_KEY:
        db_emails, db_domains = load_db_keys_live()
        print(f"DB keys live: {len(db_emails)} emails, {len(db_domains)} domains")
    else:
        db_emails, db_domains = set(), set()
        print("WARNING: no DB source (no --db-csv, no SUPABASE_SERVICE_KEY) - dedup only within pool")

    net_new, stats = dedup(all_rows, db_emails, db_domains)
    print(f"\n=== Dedup result ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # write net-new
    out = "console_netnew.csv"
    fields = ["company","first_name","last_name","email","phone","website","city","state","source_tag"]
    with open(out, "w", newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in net_new: w.writerow(r)
    print(f"\nnet-new written: {out}")

if __name__ == "__main__":
    main()
