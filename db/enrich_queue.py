"""
enrich_queue.py - Waterfall enrichment queue for no_website contacts.

Processes status=no_website rows one at a time:
  1. Google Maps search "{company} {location}" via SerpAPI -> website + rating + reviews + phone
  2. Dedupe domain against existing contacts
  3. Write result immediately (crash-safe), route to enriched / archived

Usage:
  python -m db.enrich_queue            # DRY RUN (default) - shows matches, writes nothing
  python -m db.enrich_queue --live     # writes to Supabase
  python -m db.enrich_queue --live --limit 5
"""
import os, sys, asyncio, re, httpx
from db.enrich import find_emails_for_contact

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")
SERPAPI_URL  = "https://serpapi.com/search"

LIVE  = "--live" in sys.argv
LIMIT = 5
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])

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

GENERIC = {"llc","inc","corp","corporation","co","company","the","and","roofing","construction","services","service","contractors","contractor","exteriors","group","enterprises","decks","deck","home","homes","improvement","remodeling","builders","building"}

def _tokens(name):
    import re as _re
    n = name.lower()
    n = _re.sub(r"[^a-z0-9 ]", " ", n)
    return [w for w in n.split() if w and w not in GENERIC]

def name_similar(a, b):
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        # nothing distinctive left - fall back to requiring exact-ish full match
        return a.lower().strip() == b.lower().strip()
    overlap = ta & tb
    # require that the distinctive tokens strongly overlap both ways
    ratio = len(overlap) / max(len(ta), len(tb))
    return len(overlap) >= 1 and ratio >= 0.5
GENERIC = {"llc","inc","corp","corporation","co","company","the","and","roofing","construction","services","service","contractors","contractor","exteriors","group","enterprises","decks","deck","home","homes","improvement","remodeling","builders","building"}

def _tokens(name):
    import re as _re
    n = name.lower()
    n = _re.sub(r"[^a-z0-9 ]", " ", n)
    return [w for w in n.split() if w and w not in GENERIC]

def name_similar(a, b):
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        # nothing distinctive left - fall back to requiring exact-ish full match
        return a.lower().strip() == b.lower().strip()
    overlap = ta & tb
    # require that the distinctive tokens strongly overlap both ways
    ratio = len(overlap) / max(len(ta), len(tb))
    return len(overlap) >= 1 and ratio >= 0.5
def clean_url(u):
    u = (u or "").strip()
    u = u.split("?")[0].split("#")[0]
    return u
async def maps_search(company: str, location: str) -> dict:
    params = {
        "api_key": SERPAPI_KEY,
        "engine": "google_maps",
        "q": f"{company} {location}",
        "type": "search",
        "hl": "en",
        "gl": "us",
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
    results = data.get("local_results", [])
    if not results:
        pl = data.get("place_results")
        if pl:
            results = [pl]
    if not results:
        return {}
    p = results[0]
    return {
        "name": p.get("title", ""),
        "website": p.get("website", ""),
        "rating": p.get("rating") or None,
        "reviews": p.get("reviews") or None,
        "phone": p.get("phone", ""),
        "address": p.get("address", ""),
    }

async def domain_exists(client, domain: str, self_id: int) -> bool:
    if not domain:
        return False
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/contacts",
        headers=SB_HEADERS,
        params={"select": "id,website", "website": f"ilike.*{domain}*", "id": f"neq.{self_id}"},
    )
    return len(r.json()) > 0

async def update_row(client, row_id: int, fields: dict):
    h = dict(SB_HEADERS); h["Prefer"] = "return=minimal"
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/contacts",
        headers=h,
        params={"id": f"eq.{row_id}"},
        json=fields,
    )

async def main():
    if not SUPABASE_KEY or not SERPAPI_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY and SERPAPI_KEY env vars required")
        return
    mode = "LIVE" if LIVE else "DRY RUN (no writes)"
    print(f"=== enrich_queue :: {mode} :: limit {LIMIT} ===\n")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/contacts",
            headers=SB_HEADERS,
            params={"select": "id,company,location,website,email,first_name,last_name",
                    "status": "eq.no_website", "order": "id.asc", "limit": str(LIMIT)},
        )
        rows = r.json()
        print(f"Pulled {len(rows)} no_website rows\n")
        stats = {"enriched": 0, "archived_nodom": 0, "archived_dup": 0, "needs_review": 0}
        for i, row in enumerate(rows, 1):
            company = (row.get("company") or "").strip()
            location = (row.get("location") or "").strip()
            rid = row["id"]
            print(f"[{i}/{len(rows)}] {company} ({location})")
            try:
                m = await maps_search(company, location)
            except Exception as e:
                print(f"    ! maps error: {e}")
                continue
            website = (m.get("website") or "").strip()
            if not website:
                print(f"    -> no website found  =>  archived")
                stats["archived_nodom"] += 1
                if LIVE: await update_row(client, rid, {"status": "archived"})
                await asyncio.sleep(2.5); continue
            matched_name = m.get("name", "")
            if not name_similar(company, matched_name):
                print(f"    -> weak match: '{matched_name}' vs '{company}'  =>  needs_review")
                stats["needs_review"] = stats.get("needs_review", 0) + 1
                if LIVE: await update_row(client, rid, {"status": "needs_review"})
                await asyncio.sleep(2.5); continue
            dom = clean_domain(website)
            dup = await domain_exists(client, dom, rid)
            if dup:
                print(f"    -> {dom} already exists  =>  archived (dup)")
                stats["archived_dup"] += 1
                if LIVE: await update_row(client, rid, {"status": "archived"})
                await asyncio.sleep(2.5); continue
            fields = {"website": clean_url(website), "status": "enriched"}
            if m.get("rating"):  fields["google_rating"] = m["rating"]
            if m.get("reviews"): fields["review_count"]  = m["reviews"]
            if m.get("phone") and not row.get("phone"): fields["phone"] = m["phone"]
            email_contact = {"website": website, "company": company, "email": None, "first_name": row.get("first_name"), "last_name": row.get("last_name")}
            try:
                eres = await find_emails_for_contact(email_contact)
                if eres.get("email"):
                    fields["email"] = eres["email"]
            except Exception as ee:
                print(f"       (email stage error: {ee})")
            print(f"    -> matched: {m.get('name')}")
            print(f"       email: {fields.get('email','(none found)')}")
            print(f"       website: {website}  rating: {m.get('rating')} ({m.get('reviews')} reviews)")
            print(f"       => enriched" + ("" if LIVE else "  [dry run, not written]"))
            stats["enriched"] += 1
            if LIVE: await update_row(client, rid, fields)
            await asyncio.sleep(2.5)
        print(f"\n=== Done: {stats['enriched']} enriched, "
              f"{stats['archived_nodom']} no-website archived, "
              f"{stats['archived_dup']} dup archived, {stats['needs_review']} needs review ===")

if __name__ == "__main__":
    asyncio.run(main())
