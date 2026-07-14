"""
console_engine/segment.py - Layer 1, step 5: SEGMENT into campaign CSVs

Queries the DB for scored, sendable leads, optionally filtered by source/vertical
AND by US state (and optionally city), then writes campaign-ready CSVs:
  - slow_loaders     : psi_mobile_lcp >= 4.0
  - named            : has first_name
  - generic          : email but no first_name
  - needs_quickenrich: scored, no usable email
All filtered to in_instantly IS NOT TRUE.

State/city are parsed out of the 'location' field (which may be "Fort Myers, FL"
or just "FL"), so filtering works regardless of how location was stored.

Usage:
  python -m console_engine.segment --vertical hvac --state FL
  python -m console_engine.segment --vertical hvac --state TX --city Dallas
  python -m console_engine.segment --source console:hvac --state FL
"""
import os, sys, csv, re

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://neonmrgszujadgfidlbj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BASE_REPORT = "https://report.eversite.com/report/"

# US state abbreviations for parsing
STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
          "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
          "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
          "VA","WA","WV","WI","WY","DC"}

def parse_state(location):
    """Pull a 2-letter state out of a location string like 'Fort Myers, FL'."""
    if not location: return ""
    # look for a trailing 2-letter uppercase token
    toks = re.split(r'[,\s]+', location.strip().upper())
    for t in reversed(toks):
        if t in STATES:
            return t
    return ""

def parse_city(location):
    """Best-effort city = text before the state/comma."""
    if not location: return ""
    parts = location.split(',')
    return parts[0].strip() if parts else ""

def fetch_scored(source_filter):
    import httpx
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    rows, offset, page = [], 0, 1000
    with httpx.Client(timeout=30) as c:
        while True:
            params = {"select": "company,first_name,last_name,email,phone,website,psi_mobile_lcp,report_slug,in_instantly,status,source,location",
                      "status": "eq.scored", "limit": str(page), "offset": str(offset)}
            if source_filter:
                params["source"] = f"ilike.*{source_filter}*"
            r = c.get(f"{SUPABASE_URL}/rest/v1/contacts", headers=headers, params=params)
            batch = r.json()
            if isinstance(batch, dict): raise RuntimeError(f"Supabase error: {batch}")
            if not batch: break
            rows.extend(batch)
            if len(batch) < page: break
            offset += page
    return rows

def real_email(e):
    e=(e or '').strip().lower(); return '' if e in ('','null') else e
def has_name(r):
    fn=(r.get('first_name') or '').strip(); return fn and fn.lower()!='null'
def num(v):
    try: return float(v)
    except: return None

def write_csv(name, rows):
    fields=["company","first_name","email","phone","website","location","psi_mobile_lcp","report_url"]
    with open(name,"w",newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader()
        for r in rows:
            slug=(r.get('report_slug') or '').strip()
            r['report_url']=BASE_REPORT+slug if slug else ''
            w.writerow(r)

def main():
    args=sys.argv
    def opt(f,d=None): return args[args.index(f)+1] if f in args else d
    vertical=opt("--vertical"); source=opt("--source")
    state=(opt("--state") or "").upper().strip()
    city=(opt("--city") or "").lower().strip()
    sfilter = source or vertical
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY not set"); return

    rows = fetch_scored(sfilter)
    rows = [r for r in rows if r.get('in_instantly') is not True]

    # state / city filter
    before = len(rows)
    if state:
        rows = [r for r in rows if parse_state(r.get('location')) == state]
    if city:
        rows = [r for r in rows if city in (parse_city(r.get('location')) or '').lower()]

    slow, named, generic, needs_qe = [], [], [], []
    for r in rows:
        e=real_email(r.get('email'))
        if not e: needs_qe.append(r); continue
        lcp=num(r.get('psi_mobile_lcp'))
        if lcp is not None and lcp>=4.0: slow.append(r)
        (named if has_name(r) else generic).append(r)

    tag = f"{vertical or 'all'}" + (f"_{state}" if state else "") + (f"_{city}" if city else "")
    write_csv(f"segment_slow_{tag}.csv", slow)
    write_csv(f"segment_named_{tag}.csv", named)
    write_csv(f"segment_generic_{tag}.csv", generic)
    write_csv(f"segment_needsqe_{tag}.csv", needs_qe)

    print(f"=== Segments (source: {sfilter}, state: {state or 'ALL'}, city: {city or 'ALL'}) ===")
    print(f"  scored + not in Instantly: {before}")
    if state or city: print(f"  after location filter:     {len(rows)}")
    print(f"  slow_loaders (LCP>=4): {len(slow)}  -> segment_slow_{tag}.csv")
    print(f"  named:                 {len(named)}  -> segment_named_{tag}.csv")
    print(f"  generic:               {len(generic)}  -> segment_generic_{tag}.csv")
    print(f"  needs_quickenrich:     {len(needs_qe)}  -> segment_needsqe_{tag}.csv")

if __name__ == "__main__":
    main()
