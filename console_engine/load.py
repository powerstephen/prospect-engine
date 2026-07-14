"""
console_engine/load.py - Layer 1, step 4: LOAD to DB

Inserts clean net-new rows into Supabase contacts as status='new' so the
background scorer picks them up. Uses ON CONFLICT DO NOTHING so the DB's
(company, location) unique constraint blocks any duplicate that slipped through.

Tags source as console:<vertical> (e.g. console:hvac) so this batch is traceable.

Runs on your machine: reads SUPABASE_URL + SUPABASE_SERVICE_KEY from env.

Usage:
  python -m console_engine.load --in console_clean.csv --vertical hvac          # DRY RUN
  python -m console_engine.load --in console_clean.csv --vertical hvac --live    # writes
"""
import os, sys, csv

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://neonmrgszujadgfidlbj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def load_rows(rows, vertical, live):
    import httpx
    src = f"console:{vertical}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json", "Prefer": "return=minimal,resolution=ignore-duplicates"}
    payload = []
    for r in rows:
        loc = ", ".join([p for p in [(r.get("city") or "").strip(), (r.get("state") or "").strip()] if p])
        payload.append({
            "company":  (r.get("company") or "").strip(),
            "email":    (r.get("email") or "").strip().lower(),
            "phone":    (r.get("phone") or "").strip(),
            "website":  (r.get("website") or "").strip(),
            "location": loc,
            "source":   src,
            "status":   "new",
        })
    if not live:
        print(f"DRY RUN - would insert {len(payload)} rows as source='{src}', status='new'")
        print("  sample:", payload[0] if payload else "(none)")
        print("  run again with --live to write")
        return
    # insert in chunks
    inserted = 0
    with httpx.Client(timeout=60) as c:
        for i in range(0, len(payload), 100):
            chunk = payload[i:i+100]
            r = c.post(f"{SUPABASE_URL}/rest/v1/contacts", headers=headers, json=chunk)
            if r.status_code in (200,201,204):
                inserted += len(chunk)
                print(f"  inserted chunk {i//100+1} ({len(chunk)} rows)")
            else:
                print(f"  chunk {i//100+1} status {r.status_code}: {r.text[:200]}")
    print(f"\nLIVE done. Attempted {len(payload)}, DB skipped any (company,location) dupes automatically.")

def main():
    args = sys.argv
    def opt(f,d=None): return args[args.index(f)+1] if f in args else d
    inp = opt("--in"); vertical = opt("--vertical","hvac"); live = "--live" in args
    if not inp:
        print("usage: python -m console_engine.load --in console_clean.csv --vertical hvac [--live]"); return
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY not set in this session"); return
    rows = list(csv.DictReader(open(inp, newline='', encoding='utf-8-sig')))
    print(f"loaded {len(rows)} clean rows from {inp}")
    load_rows(rows, vertical, live)

if __name__ == "__main__":
    main()
