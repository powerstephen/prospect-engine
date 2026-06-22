import csv, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from db.supabase_client import upsert_contacts

CSV = sys.argv[1] if len(sys.argv) > 1 else None
if not CSV:
    print("Usage: python import_run.py <csv>"); sys.exit(1)

# Contacts already in DB (skip) and no-website (archive)
SKIP_COMPANIES = {"alan's roofing inc.", "g & g roofing construction inc"}
NOWEB_COMPANIES = {"barfield contracting & associates, inc", "custom roofing & repairs", "anchor roofing services"}

def clean(v):
    if v is None: return ""
    v = str(v).strip()
    if v.lower() in ("","n/a","na","no website") or "research needed" in v.lower(): return ""
    return v

def norm_site(u):
    u = clean(u)
    if not u: return ""
    u = u.lower()
    for p in ("https://","http://"):
        if u.startswith(p): u = u[len(p):]
    if u.startswith("www."): u = u[4:]
    return "https://" + u.rstrip("/")

with open(CSV, encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

to_import, to_archive = [], []
for r in rows:
    comp = clean(r.get("Company",""))
    if not comp: continue
    cl = comp.lower()
    if cl in SKIP_COMPANIES:
        continue
    site = norm_site(r.get("Website",""))
    loc = ", ".join([x for x in [clean(r.get("City","")), clean(r.get("State",""))] if x])
    rec = {
        "company": comp,
        "first_name": clean(r.get("First Name","")),
        "last_name": clean(r.get("Last Name","")),
        "email": clean(r.get("Email","")),
        "phone": clean(r.get("Phone","")),
        "website": site,
        "location": loc,
        "industry": "Roofing",
        "source": clean(r.get("Source","")),
    }
    if cl in NOWEB_COMPANIES or not site:
        rec["status"] = "archived"
        to_archive.append(rec)
    else:
        rec["status"] = "new"
        to_import.append(rec)

print(f"To import (new):   {len(to_import)}")
print(f"To archive (noweb):{len(to_archive)}")
print(f"Skipped (matches): 2")
print()
print("DRY PREVIEW - first 3 import records:")
for r in to_import[:3]:
    print("  ", r["company"], "|", r["website"], "|", r["status"])
print()
ans = input("Type IMPORT to write these to the database: ").strip()
if ans == "IMPORT":
    n1 = upsert_contacts(to_import)
    n2 = upsert_contacts(to_archive)
    print(f"Imported {n1} new + {n2} archived.")
else:
    print("Aborted - nothing written.")