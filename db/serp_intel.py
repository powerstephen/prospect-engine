"""
SERP Intelligence v1
One search per contact ("AC repair {city}"), domain-matched against:
  - paid ads (serp_in_ads)
  - Local Services ads (serp_in_lsa)
  - map pack / local results (serp_in_map_pack)
  - page one organic (serp_on_page1)
Competitor names occupying those slots stored in serp_competitors (jsonb).

Claim standard (bulletproof rule):
  - POSITIVE claims ("your ad is running") require an exact domain match.
  - NEGATIVE findings ("not on page one") are stored for routing only,
    never used as an email hook.

Usage:
  python db\\serp_intel.py --dry-run --limit 10   (build queries only, no API calls)
  python db\\serp_intel.py --limit 20             (test batch)
  python db\\serp_intel.py --limit 300            (full pool)

Env required: SUPABASE_URL, SUPABASE_SERVICE_KEY, SERPAPI_KEY
"""
import os
import sys
import time
import json
import argparse
from urllib.parse import urlparse

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

VERTICAL_QUERY = {
    "hvac": "AC repair",
    "roofers": "roofer",
    "roofing": "roofer",
}
DEFAULT_VERTICAL_TERM = "AC repair"
SLEEP_BETWEEN_CALLS = 1.5


def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def normalise_domain(url_or_domain: str) -> str:
    """Reduce any URL or domain string to a bare comparable domain."""
    if not url_or_domain:
        return ""
    s = url_or_domain.strip().lower()
    if not s.startswith("http"):
        s = "https://" + s
    try:
        host = urlparse(s).netloc
    except Exception:
        host = s
    if host.startswith("www."):
        host = host[4:]
    return host.strip("/")


def vertical_term(source: str) -> str:
    src = (source or "").lower()
    for key, term in VERTICAL_QUERY.items():
        if key in src:
            return term
    return DEFAULT_VERTICAL_TERM


def city_from_location(location: str) -> str:
    """Best-effort city extraction. Expects formats like 'Tampa, FL' or
    'Tampa, FL 33604' or 'Tampa'. Takes the segment before the first comma."""
    if not location:
        return ""
    return location.split(",")[0].strip()


def build_query(contact: dict) -> str:
    city = city_from_location(contact.get("location") or "")
    if not city:
        return ""
    return f"{vertical_term(contact.get('source'))} {city}"


def fetch_contacts(limit: int, vertical: str = "hvac") -> list:
    params = {
        "select": "id,company,website,location,source,serp_checked_at",
        "source": f"like.google_maps:{vertical}*",
        "in_instantly": "eq.false",
        "first_name": "is.null",
        "email": "neq.",
        "serp_checked_at": "is.null",
        "website": "not.is.null",
        "limit": str(limit),
    }
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params, headers=headers(), timeout=30)
    r.raise_for_status()
    rows = r.json()
    junk = ("rr.com", "comcastbiz", "centurylink", "bellsouth", "att.net", "verizon")
    return [c for c in rows if c.get("website") and not any(j in c["website"].lower() for j in junk)]


STATE_NAMES = {"FL": "Florida", "TX": "Texas", "GA": "Georgia", "CA": "California",
               "AZ": "Arizona", "NC": "North Carolina", "SC": "South Carolina", "TN": "Tennessee"}


def serp_location(location: str) -> str:
    """Build a SerpAPI location string like 'Tampa, Florida, United States'
    so results reflect what a local searcher actually sees (ads and LSA
    are geo-dependent)."""
    if not location:
        return ""
    parts = [p.strip() for p in location.split(",")]
    city = parts[0] if parts else ""
    state = ""
    if len(parts) > 1:
        state_token = parts[1].split()[0].strip().upper() if parts[1].strip() else ""
        state = STATE_NAMES.get(state_token, parts[1].strip())
    pieces = [p for p in (city, state, "United States") if p]
    return ", ".join(pieces)


def serp_search(query: str, location_str: str = "") -> dict:
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": "10",
        "gl": "us",
        "hl": "en",
    }
    if location_str:
        params["location"] = location_str
    r = httpx.get("https://serpapi.com/search.json", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def extract_slots(serp: dict) -> dict:
    """Pull the four slot types out of a SerpAPI response, defensively."""
    slots = {"ads": [], "lsa": [], "map_pack": [], "organic": []}

    for ad in serp.get("ads") or []:
        slots["ads"].append({
            "name": ad.get("title") or ad.get("displayed_link") or "",
            "domain": normalise_domain(ad.get("link") or ad.get("displayed_link") or ""),
        })

    lsa_block = serp.get("local_services_ads") or {}
    lsa_items = lsa_block.get("ads") if isinstance(lsa_block, dict) else lsa_block
    for item in lsa_items or []:
        slots["lsa"].append({
            "name": item.get("title") or item.get("name") or "",
            "domain": normalise_domain(item.get("link") or item.get("website") or ""),
        })

    local_block = serp.get("local_results") or {}
    places = local_block.get("places") if isinstance(local_block, dict) else local_block
    for place in places or []:
        links = place.get("links") or {}
        site = links.get("website") or place.get("website") or place.get("link") or ""
        slots["map_pack"].append({
            "name": place.get("title") or "",
            "domain": normalise_domain(site),
        })

    for res in serp.get("organic_results") or []:
        slots["organic"].append({
            "name": res.get("title") or "",
            "domain": normalise_domain(res.get("link") or ""),
        })

    return slots


def match_contact(slots: dict, contact_domain: str) -> dict:
    """Exact-domain matching per the bulletproof standard."""
    def hit(items):
        return any(i["domain"] and i["domain"] == contact_domain for i in items)

    def competitors(items, cap=5):
        out = []
        for i in items:
            if i["domain"] and i["domain"] != contact_domain and i.get("name"):
                out.append({"name": i["name"], "domain": i["domain"]})
            if len(out) >= cap:
                break
        return out

    return {
        "serp_in_ads": hit(slots["ads"]),
        "serp_in_lsa": hit(slots["lsa"]),
        "serp_in_map_pack": hit(slots["map_pack"]),
        "serp_on_page1": hit(slots["organic"]),
        "serp_competitors": {
            "ads": competitors(slots["ads"]),
            "lsa": competitors(slots["lsa"]),
            "map_pack": competitors(slots["map_pack"], cap=3),
            "organic_top3": competitors(slots["organic"], cap=3),
        },
    }


def save(contact_id: int, fields: dict) -> bool:
    r = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact_id}",
        json=fields, headers=headers(), timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  SAVE FAILED {contact_id}: {r.status_code} {r.text[:200]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vertical", default="hvac")
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if not args.dry_run and not SERPAPI_KEY:
        missing.append("SERPAPI_KEY")
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    contacts = fetch_contacts(args.limit, args.vertical)
    if not contacts:
        print("No contacts pending SERP check.")
        return

    print(f"SERP intelligence run: {len(contacts)} contacts" + (" (DRY RUN)" if args.dry_run else ""))
    stats = {"checked": 0, "in_ads": 0, "in_lsa": 0, "in_map": 0, "on_page1": 0, "skipped": 0, "errors": 0}

    for c in contacts:
        query = build_query(c)
        if not query:
            print(f"  SKIP {c.get('company','?')}: no usable location ({c.get('location')!r})")
            stats["skipped"] += 1
            continue

        if args.dry_run:
            print(f"  {c.get('company','?'):45s} -> \"{query}\"  [{normalise_domain(c.get('website'))}]")
            continue

        try:
            serp = serp_search(query, serp_location(c.get("location") or ""))
            slots = extract_slots(serp)
            result = match_contact(slots, normalise_domain(c.get("website")))
            fields = dict(result)
            fields["serp_query"] = query
            fields["serp_checked_at"] = "now()"
            if save(c["id"], fields):
                stats["checked"] += 1
                stats["in_ads"] += int(result["serp_in_ads"])
                stats["in_lsa"] += int(result["serp_in_lsa"])
                stats["in_map"] += int(result["serp_in_map_pack"])
                stats["on_page1"] += int(result["serp_on_page1"])
                flags = [k for k in ("serp_in_ads", "serp_in_lsa", "serp_in_map_pack", "serp_on_page1") if result[k]]
                print(f"  {c.get('company','?'):45s} \"{query}\" -> {', '.join(flags) if flags else 'not found'}")
            else:
                stats["errors"] += 1
        except Exception as e:
            print(f"  ERROR {c.get('company','?')}: {e}")
            stats["errors"] += 1

        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone. checked={stats['checked']} ads={stats['in_ads']} lsa={stats['in_lsa']} "
          f"map={stats['in_map']} page1={stats['on_page1']} skipped={stats['skipped']} errors={stats['errors']}")


if __name__ == "__main__":
    main()
