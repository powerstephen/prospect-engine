"""
pipeline.py - the one function that turns a discovered business into a
fully enriched, scored, correctly-tagged contact row.

This does NOT reimplement enrichment or scoring, it calls the already-
hardened, already-tested code built tonight:
  - enrich_pipeline.enrich_one()   for name + email (QuickEnrich first,
    then contact_finder's owner-search-and-confirm waterfall)
  - a lite PSI call                for mobile load time (the metric
    actually driving campaigns right now, per Steve's own call: full
    ICP/dimensions scoring is parked, not part of the default path)

Every business, whether it came from the terminal scraper or the web
"Find Leads" button, goes through this ONE function. That is the fix
for tonight's actual root cause: five separate places each partially
reimplementing discovery/enrich/score, drifting apart, and one of them
(harvester.py) turned out to be a stale pre-fix snapshot with none of
today's hardening and no name-finding at all.

Usage as a library:
  from db.pipeline import run_lead
  contact = await run_lead(biz, vertical="tree_removal", subsource="harvest", log=log_cb)
  # contact is a dict ready to POST/PATCH to Supabase, or None if the
  # business had no website at all (nothing to enrich or score).

Usage as a CLI, for sweeping an EXISTING pool that has emails/names but
was never scored (e.g. tonight's 236 rescued harvest leads):
  python3 db/pipeline.py --vertical tree_removal --limit 20            (DRY RUN)
  python3 db/pipeline.py --vertical tree_removal --live --limit 0

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SERPAPI_KEY,
     QUICKENRICH_API_KEY, QUICKENRICH_BASE_URL, PSI_API_KEY
"""
import os
import sys
import time
import asyncio
import argparse
from datetime import datetime, timezone

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.sources import source_tag, vertical_filter
from db.enrich_pipeline import enrich_one
from db.quickenrich_enrich import clean_domain

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PSI_API_KEY  = os.environ.get("PSI_API_KEY", "") or os.environ.get("PSI_KEY", "")

PIPELINE_STAGES = ("discovered", "enriched", "scored", "loaded", "archived")


def sb_headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def site_url(website):
    w = (website or "").strip()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    return w


async def psi_mobile_lcp(url, timeout=100):
    """One PSI call for mobile LCP. Returns None on any failure, never
    raises, scoring is a nice-to-have, not something that should ever
    block a lead from being saved."""
    if not url:
        return None
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if PSI_API_KEY:
        params["key"] = PSI_API_KEY
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params)
            if r.status_code != 200:
                return None
            data = r.json()
        audits = (data.get("lighthouseResult") or {}).get("audits") or {}
        lcp_ms = (audits.get("largest-contentful-paint") or {}).get("numericValue")
        return round(lcp_ms / 1000, 1) if lcp_ms is not None else None
    except Exception:
        return None


def normalize_business(biz):
    """Discovery sources (the terminal Maps scraper, the web harvester's
    SerpAPI search) use slightly different key names. This is the one
    place that gets normalized, so run_lead() never has to guess."""
    return {
        "company": biz.get("company") or biz.get("name") or "",
        "website": biz.get("website") or "",
        "location": biz.get("location") or biz.get("address") or "",
        "phone": biz.get("phone") or None,
        "google_rating": biz.get("google_rating") or biz.get("rating") or None,
        "review_count": biz.get("review_count") or biz.get("reviews") or None,
    }


async def run_lead(biz, vertical, subsource="", log=None, existing_contact=None):
    """The one pipeline: normalize -> enrich (name+email) -> score
    (lite: PSI load time, carry through free rating/reviews) -> return
    a save-ready contact dict with a canonical source tag and pipeline
    stage. Does NOT save, callers decide when/whether to write, same
    dry-run-first pattern as every other script tonight.

    existing_contact: pass the current DB row (if updating one already
    in the pool) so enrich_one() correctly skips fields already filled.
    """
    async def _log(msg):
        if log:
            try:
                await log(msg)
            except Exception:
                pass

    source_tag(vertical, subsource)  # raises early on an unknown vertical,
                                      # before any enrichment/scoring is attempted

    biz = normalize_business(biz)
    if not biz["website"]:
        await _log("  -> no website, nothing to enrich or score")
        return None

    contact_for_enrich = existing_contact or {
        "company": biz["company"], "website": biz["website"], "location": biz["location"],
        "email": None, "first_name": None, "last_name": None,
    }

    await _log("  Enriching (name + email)...")
    enrich_fields, via = await enrich_one(contact_for_enrich, _log)
    stage = "discovered"
    if enrich_fields and "_held_candidate_name" not in enrich_fields:
        stage = "enriched"
        await _log("  -> enriched via " + str(via))
    elif enrich_fields:
        await _log("  -> name held for review (single-source), no email found")
        enrich_fields = {}

    await _log("  Scoring (mobile load time)...")
    load_time = await psi_mobile_lcp(site_url(biz["website"]))
    if load_time is not None:
        stage = "scored" if stage == "enriched" else stage
        await _log("  -> load time " + str(load_time) + "s")
    else:
        await _log("  -> could not measure load time")

    contact = {
        "company": biz["company"],
        "website": clean_domain(biz["website"]),
        "location": biz["location"],
        "phone": biz["phone"],
        "google_rating": biz["google_rating"],
        "review_count": biz["review_count"],
        "psi_mobile_lcp": load_time,
        "source": source_tag(vertical, subsource),
        "pipeline_stage": stage,
        "status": "new",
        "scored_at": datetime.now(timezone.utc).isoformat() if load_time is not None else None,
    }
    contact.update(enrich_fields or {})
    contact = {k: v for k, v in contact.items() if v is not None}
    return contact


def save(contact_id_or_none, contact):
    """Insert a new row, or PATCH an existing one if an id is given."""
    if contact_id_or_none:
        r = httpx.patch(SUPABASE_URL + "/rest/v1/contacts?id=eq." + str(contact_id_or_none),
                        json=contact, headers=sb_headers(), timeout=30)
    else:
        r = httpx.post(SUPABASE_URL + "/rest/v1/contacts", json=contact,
                       headers={**sb_headers(False), "Prefer": "resolution=merge-duplicates,return=minimal"},
                       timeout=30)
    if r.status_code not in (200, 201, 204):
        print("    SAVE FAILED: " + str(r.status_code) + " " + r.text[:200])
        return False
    return True


def fetch_pending(vertical, limit):
    """Existing rows in this vertical still missing email or a speed
    score, for sweeping an already-discovered pool (e.g. tonight's 236
    rescued tree-removal leads)."""
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website,location,phone,email,first_name,last_name,"
                      "google_rating,review_count,psi_mobile_lcp",
            "source": "like." + vertical_filter(vertical),
            "website": "not.is.null",
            "in_instantly": "eq.false",
            "status": "not.in.(archived,timeout)",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        r = httpx.get(SUPABASE_URL + "/rest/v1/contacts", params=params,
                      headers=sb_headers(False), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    rows = [c for c in rows if not c.get("email") or c.get("psi_mobile_lcp") is None]
    if limit:
        rows = rows[:limit]
    return rows


async def main_async(args):
    missing = [v for v in ("SUPABASE_SERVICE_KEY",) if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    pool = fetch_pending(args.vertical, args.limit)
    mode = "LIVE" if args.live else "DRY RUN"
    print("=== pipeline :: " + mode + " :: vertical=" + args.vertical + " :: " + str(len(pool)) + " leads ===\n")

    stats = {"enriched": 0, "scored_only": 0, "none": 0, "errors": 0}

    async def log(msg):
        print(msg)

    for i, c in enumerate(pool, 1):
        print("[" + str(i) + "/" + str(len(pool)) + "] " + (c.get("company") or ""))
        try:
            contact = await run_lead(c, args.vertical, log=log, existing_contact=c)
        except Exception as e:
            stats["errors"] += 1
            print("    ERROR: " + str(e)[:200])
            continue

        if not contact:
            stats["none"] += 1
            continue

        if contact.get("email") and contact["email"] != c.get("email"):
            stats["enriched"] += 1
        else:
            stats["scored_only"] += 1

        print("    -> " + str({k: v for k, v in contact.items() if k not in ("website",)}))
        if args.live:
            save(c["id"], contact)

    print("\n=== Done: enriched=" + str(stats["enriched"]) +
          " | scored_only=" + str(stats["scored_only"]) +
          " | none=" + str(stats["none"]) +
          " | errors=" + str(stats["errors"]) + " ===")
    if not args.live:
        print("(dry run: nothing written, add --live to commit)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", required=True)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
