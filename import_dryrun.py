import csv, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from db.supabase_client import get_contacts

def norm_site(u):
    if not u: return ""
    u = u.strip().lower()
    for p in ("https://","http://"):
        if u.startswith(p): u = u[len(p):]
    if u.startswith("www."): u = u[4:]
    return u.rstrip("/").split("/")[0]

def clean(v):
    if v is None: return ""
    v = str(v).strip()
    if v.lower() in ("","n/a","na") or "research needed" in v.lower(): return ""
    return v

def main():
    if len(sys.argv) < 2:
        print("Usage: python import_dryrun.py <csv>"); sys.exit(1)
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print("CSV rows read:", len(rows))
    existing, offset = [], 0
    while True:
        batch = get_contacts(limit=500, offset=offset)
        if not batch: break
        existing.extend(batch)
        if len(batch) < 500: break
        offset += 500
    active = [c for c in existing if (c.get("status") or "new") != "archived"]
    print("Existing pulled:", len(existing), "active:", len(active))
    by_site = {}
    for c in active:
        s = norm_site(c.get("website") or "")
        if s: by_site.setdefault(s, []).append(c)
    new_rows, match_rows, no_site = [], [], []
    for r in rows:
        s = norm_site(r.get("Website",""))
        if not s: no_site.append(r)
        elif s in by_site: match_rows.append((r, by_site[s][0]))
        else: new_rows.append(r)
    print("="*50)
    print("NEW (will import):       ", len(new_rows))
    print("MATCH (already active):  ", len(match_rows))
    print("NO WEBSITE (-> archive): ", len(no_site))
    print("="*50)
    print("\n--- NEW ---")
    for r in new_rows: print("  %-35s %s" % (r.get("Company","?"), norm_site(r.get("Website",""))))
    if match_rows:
        print("\n--- MATCHES (already in DB) ---")
        for r, ex in match_rows: print("  %-35s id=%s" % (r.get("Company","?"), ex.get("id")))
    if no_site:
        print("\n--- NO WEBSITE (archive) ---")
        for r in no_site: print("  %-35s %s" % (r.get("Company","?"), clean(r.get("Email",""))))
    print("\nDRY RUN complete - nothing written.")

if __name__ == "__main__": main()