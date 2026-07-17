"""
enrich_pipeline.py - the full per-company enrichment waterfall, in the
right cost order, in one pass:

  1. QuickEnrich dataset search   (fastest, cheapest, verified dataset)
  2. SerpAPI owner search + a real confirmation search (contact_finder.py)
  3. Site scrape / "email for domain" search / pattern guess
     (enrich.py's stages, contact_finder.py's own fallback)

Replaces running quickenrich_enrich.py and contact_finder.py separately on
manually-split city slices. Every company now gets the same waterfall,
cheapest and most-verified step first, so nothing sits stuck on the
expensive path just because of which city it landed in.

Usage:
  python3 db/enrich_pipeline.py --vertical roofers --limit 20             (DRY RUN)
  python3 db/enrich_pipeline.py --vertical roofers --limit 20 --live
  python3 db/enrich_pipeline.py --vertical roofers --live --limit 0       (0 = all pending)
  python3 db/enrich_pipeline.py --vertical roofers --city Austin --live --limit 0

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SERPAPI_KEY,
     QUICKENRICH_API_KEY, QUICKENRICH_BASE_URL
"""
import os
import sys
import time
import asyncio
import argparse

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.quickenrich_enrich import (
    quickenrich_domain_search, pick_best_employee, build_fields as qe_build_fields,
    clean_domain as qe_clean_domain,
)
from db.contact_finder import find_contact as serp_find_contact

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

SLEEP_QUICKENRICH = 0.25
SLEEP_SERP = 1.5


def sb_headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_pending(vertical, limit, city=None):
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website,location,email,first_name,last_name,phone",
            "source": "like.google_maps:" + vertical + "*",
            "website": "not.is.null",
            "in_instantly": "eq.false",
            "status": "not.in.(archived,timeout)",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        if city:
            params["location"] = "ilike." + city + "%"
        r = httpx.get(SUPABASE_URL + "/rest/v1/contacts", params=params,
                      headers=sb_headers(False), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    rows = [c for c in rows if not c.get("email") or not c.get("first_name")]
    if limit:
        rows = rows[:limit]
    return rows


def save(contact_id, fields):
    r = httpx.patch(SUPABASE_URL + "/rest/v1/contacts?id=eq." + str(contact_id),
                    json=fields, headers=sb_headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print("    SAVE FAILED " + str(contact_id) + ": " + str(r.status_code) + " " + r.text[:150])
        return False
    return True


async def enrich_one(contact, log):
    """Try QuickEnrich first. Only fall through to the slower SerpAPI
    waterfall if QuickEnrich has no usable result."""
    company = (contact.get("company") or "").strip()
    domain = qe_clean_domain(contact.get("website") or "")

    # Step 1: QuickEnrich, cheap and fast
    try:
        employees = quickenrich_domain_search(domain)
    except Exception as e:
        employees = []
        await log("    quickenrich error: " + str(e)[:150])

    if employees:
        best = pick_best_employee(employees)
        fields = qe_build_fields(contact, best, company)
        if fields:
            await log("    -> via QuickEnrich: " + str(fields))
            time.sleep(SLEEP_QUICKENRICH)
            return fields, "quickenrich"
    time.sleep(SLEEP_QUICKENRICH)

    # Step 2+3: fall through to the SerpAPI owner-search + confirmation +
    # site-scrape/search/guess chain (contact_finder.py's own internal
    # waterfall already does all of this)
    await log("    QuickEnrich empty, falling to search-based enrichment...")
    fields = await serp_find_contact(contact, log)
    return (fields or None), ("serp" if fields else None)


async def main_async(args):
    missing = [v for v in ("SUPABASE_SERVICE_KEY",) if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    pool = fetch_pending(args.vertical, args.limit, args.city)
    mode = "LIVE" if args.live else "DRY RUN"
    scope = (" city=" + args.city) if args.city else ""
    print("=== enrich_pipeline :: " + mode + " :: vertical=" + args.vertical + scope +
          " :: " + str(len(pool)) + " leads ===\n")

    stats = {"quickenrich": 0, "serp": 0, "none": 0, "errors": 0}

    async def log(msg):
        print(msg)

    for i, c in enumerate(pool, 1):
        company = (c.get("company") or "").strip()
        print("[" + str(i) + "/" + str(len(pool)) + "] " + company)
        try:
            fields, via = await enrich_one(c, log)
        except Exception as e:
            stats["errors"] += 1
            print("    ERROR: " + str(e)[:200])
            continue

        if not fields:
            stats["none"] += 1
            print("    -> nothing found anywhere")
            continue

        stats[via] += 1
        if args.live:
            save(c["id"], fields)

    print("\n=== Done: quickenrich=" + str(stats["quickenrich"]) +
          " | serp_fallback=" + str(stats["serp"]) +
          " | none=" + str(stats["none"]) +
          " | errors=" + str(stats["errors"]) + " ===")
    if not args.live:
        print("(dry run: nothing written, add --live to commit)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--city", default=None)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
