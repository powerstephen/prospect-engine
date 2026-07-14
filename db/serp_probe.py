import os, httpx, json

def probe(label, extra):
    params = {
        "engine": "google",
        "q": "AC repair Fort Myers",
        "api_key": os.environ["SERPAPI_KEY"],
        "location": "Fort Myers, Florida, United States",
        "gl": "us", "hl": "en",
    }
    params.update(extra)
    d = httpx.get("https://serpapi.com/search.json", params=params, timeout=60).json()
    keys = sorted(d.keys())
    print(f"\n{label}: {keys}")
    for k in ("ads", "local_services_ads", "shopping_results", "inline_ads"):
        v = d.get(k)
        if v:
            items = v if isinstance(v, list) else v.get("ads", [])
            print(f"  {k}: {len(items)} items, first: {json.dumps(items[0])[:200] if items else 'n/a'}")

probe("desktop default", {})
probe("mobile", {"device": "mobile"})
probe("no_cache", {"no_cache": "true"})
