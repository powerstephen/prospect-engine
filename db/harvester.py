"""
Harvester - combined pipeline:
1. Google Maps search for businesses
2. Unified enrich + score via db/pipeline.py (name + email via QuickEnrich
   and the hardened contact_finder waterfall, mobile load time via PSI)
3. Only saves fully enriched contacts to Supabase

This used to carry its own separate copy of the email-finding logic,
which had silently fallen out of sync with every fix applied to the
main pipeline tonight: it had no Wix/registrar/placeholder-domain
blocklist, a broken pattern-guess fallback that always returned
"info@domain" no matter what, and never attempted to find an owner
name at all. Every business this file ever saved went in nameless.

It now calls db/pipeline.py's run_lead(), the same function the
terminal tools use, so this file inherits every fix automatically
going forward instead of needing its own separate maintenance.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")

from db.pipeline import run_lead, save as pipeline_save


INDUSTRY_SYNONYMS = {
    "roofers": ("roof", "roofing", "roofer"),
    "hvac": ("hvac", "air condition", "ac repair", "heating", "furnace", "climate control"),
    "tree_removal": ("tree", "arborist", "stump"),
}


def _vertical_from_industry(industry: str) -> str:
    """The web UI takes a free-text industry string ('tree removal',
    'air conditioning repair', etc). Map it to one of the known
    verticals in db/sources.py via explicit synonym matching, rather
    than a raw substring guess that silently defaulted almost
    everything to the wrong vertical."""
    from db.sources import VERTICALS
    ind = (industry or "").strip().lower()
    for vertical, synonyms in INDUSTRY_SYNONYMS.items():
        if any(s in ind for s in synonyms):
            return vertical
    raise ValueError(
        "Could not map industry '" + industry + "' to a known vertical. "
        "Add a synonym for it to INDUSTRY_SYNONYMS in db/harvester.py "
        "rather than guessing, an unrecognised industry should not "
        "silently land in the wrong bucket."
    )


async def harvest(
    industry: str,
    location: str,
    limit: int = 20,
    log_cb=None,
) -> dict:
    """
    Full harvest pipeline: find businesses, then run each one through
    the unified enrich+score pipeline, then save.
    """
    import httpx

    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass

    try:
        vertical = _vertical_from_industry(industry)
    except ValueError as e:
        await log(str(e))
        return {"found": 0, "enriched": 0, "saved": 0, "skipped": 0}

    await log(f"Searching Google Maps: {industry} in {location}...")

    # -- Step 1: Google Maps search (unchanged, this part was fine) --
    businesses = []
    try:
        from scraper.engine import find_businesses
        businesses = await find_businesses(industry, location, limit, SERPAPI_KEY)
        await log(f"Found {len(businesses)} businesses")
    except Exception as e:
        await log(f"Search failed: {e}")
        return {"found": 0, "enriched": 0, "saved": 0, "skipped": 0}

    if not businesses:
        await log("No businesses found - try different search terms")
        return {"found": 0, "enriched": 0, "saved": 0, "skipped": 0}

    # -- Step 2: run every business through the ONE pipeline --
    enriched = 0
    saved = 0
    skipped = 0

    for i, biz in enumerate(businesses, 1):
        name = biz.get("name", "Unknown")
        await log(f"\n[{i}/{len(businesses)}] {name}")

        try:
            contact = await run_lead(biz, vertical, subsource="harvest", log=log)
        except Exception as e:
            await log(f"  Pipeline error: {e}")
            skipped += 1
            continue

        if not contact:
            skipped += 1
            continue

        if contact.get("email"):
            enriched += 1

        ok, detail = pipeline_save(None, contact)
        if ok:
            saved += 1
            await log("  Saved to Leads")
        else:
            skipped += 1
            await log("  Save failed: " + detail)

        await asyncio.sleep(0.3)

    await log(
        f"\n{'='*40}\n"
        f"Harvest complete!\n"
        f"  Found:    {len(businesses)} businesses\n"
        f"  Enriched: {enriched} emails found\n"
        f"  Saved:    {saved} added to Leads\n"
        f"  Skipped:  {skipped} (no website or nothing found)\n"
    )

    return {
        "found":    len(businesses),
        "enriched": enriched,
        "saved":    saved,
        "skipped":  skipped,
    }
