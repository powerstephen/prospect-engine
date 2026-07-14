"""
console_engine/tag_instantly.py - THE DEDUP DRIFT KILLER

Re-syncs the in_instantly flag from a fresh Instantly export. Run this AFTER
every Instantly load, or whenever you want the flag to be accurate.

What it does:
  1. reads one or more Instantly export CSVs (any format - finds the email column)
  2. resets in_instantly = false across all contacts (clean slate)
  3. sets in_instantly = true for every contact matching by EMAIL or by DOMAIN

Matching on BOTH email and domain is what makes dedup reliable - the DB is
company-keyed, Instantly is person-keyed, same company under different emails.

Runs on your machine: needs SUPABASE_URL + SUPABASE_SERVICE_KEY in env.

Usage:
  # DRY RUN (shows what it would tag, changes nothing):
  python -m console_engine.tag_instantly export1.csv export2.csv

  # LIVE (actually updates the flag):
  python -m console_engine.tag_instantly export1.csv export2.csv --live
"""
import os, sys, csv, re

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://neonmrgszujadgfidlbj.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

FREE = {"gmail.com","yahoo.com","hotmail.com","aol.com","outlook.com","icloud.com","me.com",
        "aim.com","att.net","comcast.net","bellsouth.net","sbcglobal.net","verizon.net",
        "ymail.com","live.com","msn.com","broadstripe.net","cfl.rr.com"}
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

def extract_from_file(path):
    """Find every email in a file regardless of column layout."""
    emails=set()
    with open(path, encoding='utf-8', errors='ignore') as f:
        text=f.read()
    for m in EMAIL_RE.findall(text):
        e=m.lower().strip()
        # skip obvious junk
        if e.endswith(('.webp','.png','.jpg','.jpeg','.gif')): continue
        if e in ('you@company.com','user@domain.com','service@atom.com'): continue
        emails.add(e)
    return emails

def domains_from(emails):
    d=set()
    for e in emails:
        dom=e.split('@')[-1]
        if dom not in FREE:
            d.add(dom)
    return d

def run(files, live):
    import httpx
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY not set"); return

    all_emails=set()
    for f in files:
        e=extract_from_file(f)
        print(f"  {f}: {len(e)} emails")
        all_emails |= e
    all_domains = domains_from(all_emails)
    print(f"\nTotal unique emails from Instantly: {len(all_emails)}")
    print(f"Total unique corporate domains: {len(all_domains)}")

    if not live:
        print("\nDRY RUN - nothing changed. Add --live to apply.")
        print("Would: reset in_instantly=false everywhere, then set true for the above emails+domains.")
        return

    headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}","Content-Type":"application/json"}
    with httpx.Client(timeout=60) as c:
        # 1. reset all to false
        print("\nResetting in_instantly=false across all contacts...")
        r=c.patch(f"{SUPABASE_URL}/rest/v1/contacts",
                  headers={**headers,"Prefer":"return=minimal"},
                  params={"in_instantly":"neq.false"},  # only those not already false
                  json={"in_instantly":False})
        print(f"  reset status: {r.status_code}")

        # 2. tag by email in batches (PostgREST 'in' filter)
        def tag_email_batch(batch):
            quoted=",".join('"'+e.replace('"','')+'"' for e in batch)
            r=c.patch(f"{SUPABASE_URL}/rest/v1/contacts",
                      headers={**headers,"Prefer":"return=minimal"},
                      params={"email":f"in.({quoted})"},
                      json={"in_instantly":True})
            return r.status_code
        emails=list(all_emails); tagged_e=0
        for i in range(0,len(emails),100):
            b=emails[i:i+100]; st=tag_email_batch(b)
            if st in (200,204): tagged_e+=len(b)
            print(f"  email batch {i//100+1}: {st}")

        # 3. tag by domain (website ilike) - one at a time (ilike can't batch)
        tagged_d=0
        for dom in all_domains:
            r=c.patch(f"{SUPABASE_URL}/rest/v1/contacts",
                      headers={**headers,"Prefer":"return=minimal"},
                      params={"website":f"ilike.*{dom}*"},
                      json={"in_instantly":True})
            if r.status_code in (200,204): tagged_d+=1
        print(f"\nTagged by email: ~{tagged_e} matched-attempts")
        print(f"Tagged by domain: {tagged_d} domains processed")
        print("\nDONE. in_instantly is now synced to this Instantly export.")

def main():
    args=[a for a in sys.argv[1:] if not a.startswith("--")]
    live="--live" in sys.argv
    if not args:
        print("usage: python -m console_engine.tag_instantly export1.csv [export2.csv ...] [--live]")
        return
    run(args, live)

if __name__=="__main__":
    main()
