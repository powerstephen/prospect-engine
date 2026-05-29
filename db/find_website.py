"""
Website finder — looks up missing websites for contacts using Google Maps API.
Takes company name + location, searches Google Maps, writes website back to Supabase.
"""
import asyncio
import os
import sys
 
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
 
import httpx
 
SERPAPI_URL = "https://serpapi.com/search"
 
 
async def find_website_for_contact(contact: dict, api_key: str, log_cb=None) -> str | None:
    """Search Google Maps for a contact's website. Returns website URL or None."""
 
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass
 
    company  = (contact.get("company") or "").strip()
    location = (contact.get("location") or "").strip()
 
    if not company:
        await log(f"  ↳ No company name, skipping")
        return None
 
    query = f"{company} {location}".strip()
    await log(f"  Searching: {query}")
 
    try:
        params = {
            "api_key": api_key,
            "engine": "google_maps",
            "q": query,
            "type": "search",
            "hl": "en",
            "gl": "us",
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(SERPAPI_URL, params=params)
            r.raise_for_status()
            data = r.json()
 
        results = data.get("local_results", [])
        if not results:
            await log(f"  ↳ No results found")
            return None
 
        # Try to find the best match — prefer exact company name match
        for result in results[:3]:
            name    = result.get("title", "").lower()
            website = result.get("website", "")
            if website and company.lower()[:10] in name:
                # Clean up the website
                website = website.rstrip("/")
                if website.startswith("http://"):
                    website = website[7:]
                elif website.startswith("https://"):
                    website = website[8:]
                await log(f"  ✓ Found: {website}")
                return website
 
        # Fall back to first result with a website
        for result in results[:3]:
            website = result.get("website", "")
            if website:
                website = website.rstrip("/")
                if website.startswith("http://"):
                    website = website[7:]
                elif website.startswith("https://"):
                    website = website[8:]
                await log(f"  ✓ Found (fallback): {website}")
                return website
 
        await log(f"  ↳ No website in results")
        return None
 
    except Exception as e:
        await log(f"  ✗ Error: {e}")
        return None
 
 
async def run_find_websites(ids: list[int], api_key: str, log_cb=None) -> dict:
    """Find websites for a list of contact IDs and write back to Supabase."""
 
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass
 
    from db.supabase_client import get_contacts
    import httpx as _httpx
    import os as _os
 
    await log(f"Finding websites for {len(ids)} contacts...")
 
    all_contacts = get_contacts(limit=5000)
    id_set   = set(ids)
    contacts = [c for c in all_contacts if c["id"] in id_set and not c.get("website")]
    skipped  = len(ids) - len(contacts)
 
    if skipped:
        await log(f"  ({skipped} already have websites — skipping)")
 
    found  = 0
    missed = 0
 
    supabase_url = _os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = _os.environ.get("SUPABASE_SERVICE_KEY", "")
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
 
    for contact in contacts:
        website = await find_website_for_contact(contact, api_key, log_cb=log_cb)
 
        if website:
            # Write website back to Supabase
            try:
                async with _httpx.AsyncClient(timeout=10) as c:
                    r = await c.patch(
                        f"{supabase_url}/rest/v1/contacts?id=eq.{contact['id']}",
                        json={"website": website},
                        headers=headers,
                    )
                    r.raise_for_status()
                found += 1
            except Exception as e:
                await log(f"  ✗ Failed to save website: {e}")
                missed += 1
        else:
            missed += 1
 
        # Polite rate limit — SerpAPI has limits
        await asyncio.sleep(1)
 
    await log(f"\n✓ Done — {found} websites found, {missed} not found")
    return {"found": found, "missed": missed, "skipped": skipped}
 
