"""
maps_scraper.py - Google Maps city scraper for net-new lead discovery.

Walks a list of cities x a vertical, pulls every business with a website
from SerpAPI google_maps, dedupes domains against existing contacts, and
writes net-new rows to the contacts table as status=no_website-style leads
ready for the scorer.

Reuses the exact SerpAPI + Supabase patterns from db.enrich_queue.

Usage:
  python -m db.maps_scraper --vertical hvac                 # DRY RUN (default), default city list
  python -m db.maps_scraper --vertical hvac --live          # writes net-new rows
  python -m db.maps_scraper --vertical roofers --live --max-pages 3
  python -m db.maps_scraper --vertical hvac --cities "Tampa,Orlando,Naples"
"""
import os, sys, asyncio, re, httpx

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")
SERPAPI_URL  = "https://serpapi.com/search"

LIVE = "--live" in sys.argv

def _arg(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default

VERTICAL  = _arg("--vertical", "hvac").strip().lower()
MAX_PAGES = int(_arg("--max-pages", "5"))
CITIES_ARG = _arg("--cities", "")

# Search term per vertical. Keep the query natural - what a homeowner would type.
VERTICAL_QUERY = {
    "hvac":    "hvac contractors",
    "roofers": "roofing contractors",
}

# Junk filter: skip listings whose "website" is a platform page or a
# cheap-TLD lead-gen doorway (the fake family-surname roofing network
# pattern: .pro/.site/.online/.best/.homes spam caught in the 2026-07-08
# Texas dry run). Skips are logged and counted, nothing vanishes silently.
PLATFORM_DOMAINS = {
    "instagram.com", "facebook.com", "google.com", "yelp.com",
    "linktr.ee", "business.site", "godaddysites.com", "wixsite.com",
    "square.site", "linkedin.com", "youtube.com", "tiktok.com",
}
JUNK_TLDS = (".top", ".site", ".online", ".best", ".homes", ".pro", ".xyz", ".icu", ".click")

def is_junk_domain(dom: str):
    """Returns (junk: bool, reason: str)."""
    for p in PLATFORM_DOMAINS:
        if dom == p or dom.endswith("." + p):
            return True, f"platform ({p})"
    for tld in JUNK_TLDS:
        if dom.endswith(tld):
            return True, f"junk TLD ({tld})"
    return False, ""

# Default Florida metro list, ranked by contractor density.
DEFAULT_CITIES = [
    "Miami, FL", "Tampa, FL", "Orlando, FL", "Jacksonville, FL",
    "Fort Lauderdale, FL", "St. Petersburg, FL", "Hialeah, FL",
    "Port St. Lucie, FL", "Cape Coral, FL", "Fort Myers, FL",
    "Tallahassee, FL", "Sarasota, FL", "Naples, FL", "Lakeland, FL",
    "Pembroke Pines, FL", "Gainesville, FL", "Clearwater, FL",
    "West Palm Beach, FL", "Pompano Beach, FL", "Ocala, FL",
    "Boca Raton, FL", "Kissimmee, FL", "Bradenton, FL", "Melbourne, FL",
    "Daytona Beach, FL", "Deltona, FL", "Palm Bay, FL", "Punta Gorda, FL",
]

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

def clean_domain(website: str) -> str:
    url = (website or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = url.split("/")[0].split("?")[0]
    return url.strip()

def clean_url(u):
    u = (u or "").strip()
    u = u.split("?")[0].split("#")[0]
    return u

async def maps_page(query: str, start: int) -> list:
    """One page of google_maps results. Returns list of place dicts."""
    params = {
        "api_key": SERPAPI_KEY,
        "engine": "google_maps",
        "q": query,
        "type": "search",
        "hl": "en",
        "gl": "us",
        "start": str(start),
    }
    data = None
    for attempt in range(4):
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(SERPAPI_URL, params=params)
        if r.status_code == 429:
            wait = 10 * (2 ** attempt)
            print(f"    (429 rate limit, waiting {wait}s and retrying...)")
            await asyncio.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        break
    if data is None:
        raise RuntimeError("rate limited after retries")
    return data.get("local_results", []) or []

async def load_existing_domains(client) -> set:
    """Pull every existing contact website, normalized to root domain."""
    domains = set()
    offset = 0
    page = 1000
    while True:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/contacts",
            headers=SB_HEADERS,
            params={"select": "website", "limit": str(page), "offset": str(offset)},
        )
        batch = r.json()
        if isinstance(batch, dict):
            raise RuntimeError(f"Supabase error loading contacts: {batch}")
        if not batch:
            break
        for row in batch:
            d = clean_domain(row.get("website") or "")
            if d:
                domains.add(d)
        if len(batch) < page:
            break
        offset += page
    return domains

async def insert_lead(client, fields: dict):
    h = dict(SB_HEADERS); h["Prefer"] = "return=minimal"
    await client.post(
        f"{SUPABASE_URL}/rest/v1/contacts",
        headers=h,
        json=fields,
    )

async def main():
    if not SUPABASE_KEY or not SERPAPI_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY and SERPAPI_KEY env vars required")
        return
    if VERTICAL not in VERTICAL_QUERY:
        print(f"ERROR: unknown vertical '{VERTICAL}'. Known: {list(VERTICAL_QUERY)}")
        return

    cities = [c.strip() for c in CITIES_ARG.split(";") if c.strip()] if CITIES_ARG else DEFAULT_CITIES
    base_term = VERTICAL_QUERY[VERTICAL]
    mode = "LIVE" if LIVE else "DRY RUN (no writes)"
    print(f"=== maps_scraper :: {mode} :: vertical={VERTICAL} :: {len(cities)} cities :: max {MAX_PAGES} pages/city ===\n")

    async with httpx.AsyncClient(timeout=30) as client:
        print("Loading existing contact domains for dedup...")
        existing = await load_existing_domains(client)
        print(f"  {len(existing)} existing domains loaded\n")

        seen_this_run = set()
        stats = {"scanned": 0, "no_website": 0, "junk": 0, "dup_existing": 0, "dup_thisrun": 0, "new": 0}

        for ci, city in enumerate(cities, 1):
            query = f"{base_term} in {city}"
            print(f"[{ci}/{len(cities)}] {query}")
            for page in range(MAX_PAGES):
                start = page * 20
                try:
                    results = await maps_page(query, start)
                except Exception as e:
                    print(f"    ! page error: {e}")
                    break
                if not results:
                    break
                for p in results:
                    stats["scanned"] += 1
                    name = (p.get("title") or "").strip()
                    website = (p.get("website") or "").strip()
                    if not website:
                        stats["no_website"] += 1
                        continue
                    dom = clean_domain(website)
                    junk, why = is_junk_domain(dom)
                    if junk:
                        stats["junk"] += 1
                        print(f"    - SKIP {name}  [{dom}]  {why}")
                        continue
                    if dom in existing:
                        stats["dup_existing"] += 1
                        continue
                    if dom in seen_this_run:
                        stats["dup_thisrun"] += 1
                        continue
                    seen_this_run.add(dom)
                    stats["new"] += 1
                    fields = {
                        "company":  name,
                        "website":  clean_url(website),
                        "location": city,
                        "status":   "new",
                        "source":   f"google_maps:{VERTICAL}",
                    }
                    if p.get("rating"):  fields["google_rating"] = p["rating"]
                    if p.get("reviews"): fields["review_count"]  = p["reviews"]
                    if p.get("phone"):   fields["phone"]         = p["phone"]
                    print(f"    + NEW  {name}  [{dom}]  rating={p.get('rating')} ({p.get('reviews')})")
                    if LIVE:
                        await insert_lead(client, fields)
                await asyncio.sleep(2.5)

        print(f"\n=== Done ===")
        print(f"  scanned:        {stats['scanned']}")
        print(f"  no website:     {stats['no_website']}")
        print(f"  junk skipped:   {stats['junk']}")
        print(f"  dup (existing): {stats['dup_existing']}")
        print(f"  dup (this run): {stats['dup_thisrun']}")
        print(f"  NET NEW:        {stats['new']}" + ("" if LIVE else "  [dry run, nothing written]"))

if __name__ == "__main__":
    asyncio.run(main())
