"""
SERP Market Scan v1
Market-first advertiser harvesting. Instead of asking "is contact X
advertising on one guessed keyword", searches every major metro in the
pool across multiple service keywords and collects EVERY advertiser seen.

Results land in the serp_advertisers table. Join back to contacts to set
serp_in_ads with real coverage, and count keyword appearances as a spend
proxy. Advertisers not in our database are future prospects with proven
marketing budgets.

Usage:
  python db\\serp_market_scan.py --list-metros        (show derived metros, no API calls)
  python db\\serp_market_scan.py --metros 2           (test: scan 2 metros)
  python db\\serp_market_scan.py                      (scan all derived metros)

Env required: SUPABASE_URL, SUPABASE_SERVICE_KEY, SERPAPI_KEY
"""
import os
import sys
import time
import argparse
from collections import Counter
from urllib.parse import urlparse

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

KEYWORDS = [
    "AC repair {city}",
    "emergency AC repair {city}",
    "air conditioning repair {city}",
    "HVAC contractor {city}",
    "AC installation {city}",
    "AC replacement {city}",
    "heat pump repair {city}",
]

MIN_CONTACTS_PER_METRO = 5
SLEEP_BETWEEN_CALLS = 1.5

STATE_NAMES = {"FL": "Florida", "TX": "Texas", "GA": "Georgia", "CA": "California",
               "AZ": "Arizona", "NC": "North Carolina", "SC": "South Carolina", "TN": "Tennessee"}


def headers(prefer_minimal=True):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer_minimal:
        h["Prefer"] = "return=minimal"
    return h


def normalise_domain(url_or_domain: str) -> str:
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


def derive_metros() -> list:
    """Cities with MIN_CONTACTS_PER_METRO+ contacts in the HVAC pool,
    with their state token for geo targeting."""
    params = {
        "select": "location",
        "source": "like.google_maps:hvac*",
        "in_instantly": "eq.false",
        "first_name": "is.null",
        "email": "neq.",
        "limit": "1000",
    }
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params, headers=headers(False), timeout=30)
    r.raise_for_status()
    counts = Counter()
    states = {}
    for row in r.json():
        loc = row.get("location") or ""
        parts = [p.strip() for p in loc.split(",")]
        city = parts[0] if parts and parts[0] else None
        if not city:
            continue
        city_key = city.title()
        counts[city_key] += 1
        if len(parts) > 1 and parts[1].strip():
            states[city_key] = parts[1].split()[0].strip().upper()
    metros = [(c, states.get(c, "FL"), n) for c, n in counts.most_common() if n >= MIN_CONTACTS_PER_METRO]
    return metros


def serp_location(city: str, state_token: str) -> str:
    state = STATE_NAMES.get(state_token, state_token)
    return f"{city}, {state}, United States"


def serp_search(query: str, location_str: str) -> dict:
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": "10",
        "gl": "us",
        "hl": "en",
        "location": location_str,
    }
    r = httpx.get("https://serpapi.com/search.json", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def extract_advertisers(serp: dict) -> list:
    """Every advertiser visible on the page: paid ads and Local Services."""
    out = []
    for ad in serp.get("ads") or []:
        out.append({
            "name": (ad.get("title") or ad.get("displayed_link") or "").strip(),
            "domain": normalise_domain(ad.get("link") or ad.get("displayed_link") or ""),
            "slot": "ads",
        })
    lsa_block = serp.get("local_services_ads") or {}
    lsa_items = lsa_block.get("ads") if isinstance(lsa_block, dict) else lsa_block
    for item in lsa_items or []:
        out.append({
            "name": (item.get("title") or item.get("name") or "").strip(),
            "domain": normalise_domain(item.get("link") or item.get("website") or ""),
            "slot": "lsa",
        })
    return [a for a in out if a["name"] or a["domain"]]


def save_advertiser(row: dict) -> bool:
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/serp_advertisers", json=row, headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SAVE FAILED: {r.status_code} {r.text[:200]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metros", type=int, default=0, help="limit to first N metros (0 = all)")
    ap.add_argument("--list-metros", action="store_true")
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if not args.list_metros and not SERPAPI_KEY:
        missing.append("SERPAPI_KEY")
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    metros = derive_metros()
    if args.list_metros:
        print(f"Derived metros ({MIN_CONTACTS_PER_METRO}+ contacts):")
        for city, state, n in metros:
            print(f"  {city}, {state}: {n} contacts")
        total = (len(metros) if not args.metros else min(args.metros, len(metros))) * len(KEYWORDS)
        print(f"\nFull scan = {len(metros)} metros x {len(KEYWORDS)} keywords = {len(metros) * len(KEYWORDS)} searches")
        return

    if args.metros:
        metros = metros[:args.metros]

    print(f"Market scan: {len(metros)} metros x {len(KEYWORDS)} keywords = {len(metros) * len(KEYWORDS)} searches")
    stats = {"searches": 0, "advertisers": 0, "errors": 0}

    for city, state, n in metros:
        loc = serp_location(city, state)
        for kw in KEYWORDS:
            query = kw.format(city=city)
            try:
                serp = serp_search(query, loc)
                ads = extract_advertisers(serp)
                for a in ads:
                    row = {
                        "metro": city,
                        "state": state,
                        "query": query,
                        "advertiser_name": a["name"],
                        "advertiser_domain": a["domain"],
                        "slot": a["slot"],
                    }
                    if save_advertiser(row):
                        stats["advertisers"] += 1
                stats["searches"] += 1
                print(f"  \"{query}\" -> {len(ads)} advertisers")
            except Exception as e:
                print(f"  ERROR \"{query}\": {e}")
                stats["errors"] += 1
            time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone. searches={stats['searches']} advertiser_rows={stats['advertisers']} errors={stats['errors']}")
    print("Next: run the join SQL to mark serp_in_ads on matching contacts.")


if __name__ == "__main__":
    main()
