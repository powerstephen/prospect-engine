"""
QuickEnrich enrichment — finds emails, phones, LinkedIn for contacts.
Uses dataset-search endpoint to find owner/president/CEO contacts by company URL.
Falls back to employees/search if we have first_name + last_name.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

QUICKENRICH_API = "https://app.quickenrich.io/api"
QUICKENRICH_KEY = os.environ.get("QUICKENRICH_API_KEY", "")

OWNER_TITLES = "Owner, President, Founder, CEO, Co-Founder, Managing Owner, Principal Owner, Director"

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def clean_domain(website: str) -> str:
    """Extract clean domain from URL."""
    url = website.strip().lower()
    for prefix in ["https://www.", "http://www.", "https://", "http://", "www."]:
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/").split("/")[0]


async def enrich_contact(contact: dict, log_cb=None) -> dict:
    """
    Enrich a single contact using QuickEnrich.
    Returns dict of fields to write back.
    """
    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass

    import httpx

    website = (contact.get("website") or "").strip()
    company = contact.get("company", "?")

    if not website:
        await log(f"  ↳ {company} — no website, skipping")
        return {}

    domain = clean_domain(website)
    await log(f"  🔍 Enriching {company} ({domain})...")

    headers = {
        "Authorization": f"Bearer {QUICKENRICH_KEY}",
        "Content-Type": "application/json",
    }

    enriched = {}

    # ── Strategy 1: dataset-search by company URL + owner titles ──
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{QUICKENRICH_API}/employees/dataset-search",
                params={
                    "company_url": domain,
                    "title": OWNER_TITLES,
                    "page": 1,
                },
                headers=headers,
            )
            data = r.json()

        if data.get("success") and data.get("data"):
            contacts = data["data"]
            # Pick the best match — prefer Owner/President/Founder
            best = None
            priority = ["owner", "president", "founder", "ceo"]
            for p in priority:
                for ct in contacts:
                    if p in (ct.get("title") or "").lower():
                        best = ct
                        break
                if best:
                    break
            if not best:
                best = contacts[0]

            enriched = {
                "first_name":   best.get("first_name") or contact.get("first_name"),
                "last_name":    best.get("last_name") or contact.get("last_name"),
                "email":        best.get("email") if best.get("email") not in (None, "N/A", "") else contact.get("email"),
                "phone":        best.get("employee_phone") if best.get("employee_phone") not in (None, "N/A", "") else contact.get("phone"),
                "job_title":    best.get("title") or contact.get("job_title"),
                "linkedin_url": best.get("employee_linkedin") or contact.get("linkedin_url"),
            }

            # Also grab company-level data
            if best.get("revenue"):
                enriched["employee_count"] = best.get("employee_count") or contact.get("employee_count")

            await log(
                f"  ✓ Found: {enriched.get('first_name')} {enriched.get('last_name')} "
                f"| {enriched.get('job_title')} "
                f"| {enriched.get('email', '—')}"
            )
            return enriched

        else:
            await log(f"  ↳ No dataset results for {domain}")

    except Exception as e:
        await log(f"  ✗ Dataset search error: {e}")

    # ── Strategy 2: employees/search if we have name ──
    first = (contact.get("first_name") or "").strip()
    last  = (contact.get("last_name") or "").strip()

    if first and last:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"{QUICKENRICH_API}/employees/search",
                    params={
                        "company_url": domain,
                        "first_name": first,
                        "last_name": last,
                    },
                    headers=headers,
                )
                data = r.json()

            if data.get("success") and data.get("data"):
                ct = data["data"]
                enriched = {
                    "email":        ct.get("email") if ct.get("email") not in (None, "N/A", "") else None,
                    "phone":        ct.get("employee_phone") if ct.get("employee_phone") not in (None, "N/A", "") else None,
                    "linkedin_url": ct.get("employee_linkedin") or None,
                    "job_title":    ct.get("title") or contact.get("job_title"),
                }
                await log(f"  ✓ Found via name search: {ct.get('email', '—')}")
                return enriched
            else:
                await log(f"  ↳ No results for {first} {last} at {domain}")

        except Exception as e:
            await log(f"  ✗ Name search error: {e}")

    return {}


async def run_bulk_enrich(ids: list[int], log_cb=None) -> dict:
    """Enrich a list of contacts and write results back to Supabase."""

    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass

    if not QUICKENRICH_KEY:
        await log("✗ No QUICKENRICH_API_KEY configured")
        return {"enriched": 0, "errors": 0, "skipped": 0}

    import httpx
    from db.supabase_client import get_contacts

    all_contacts = get_contacts(limit=5000)
    id_set   = set(ids)
    contacts = [c for c in all_contacts if c["id"] in id_set]

    await log(f"Enriching {len(contacts)} contacts via QuickEnrich...")

    enriched_count = 0
    errors = 0
    skipped = 0

    sb_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for contact in contacts:
        result = await enrich_contact(contact, log_cb=log)

        if not result:
            skipped += 1
            continue

        # Remove None values — don't overwrite existing data with None
        payload = {k: v for k, v in result.items() if v is not None}

        if not payload:
            skipped += 1
            continue

        # Always mark as enriched when we find something
        payload["status"] = "enriched"

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.patch(
                    f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact['id']}",
                    json=payload,
                    headers=sb_headers,
                )
                if r.status_code in (200, 201, 204):
                    enriched_count += 1
                    await log(f"  ✓ Saved to Supabase")
                else:
                    await log(f"  ✗ Supabase write failed: {r.status_code}")
                    errors += 1
        except Exception as e:
            await log(f"  ✗ Save error: {e}")
            errors += 1

        # Rate limit — 500 req/min but be polite
        await asyncio.sleep(0.5)

    await log(f"\n✓ Enrichment complete — {enriched_count} enriched | {skipped} not found | {errors} errors")
    return {"enriched": enriched_count, "errors": errors, "skipped": skipped}
