import os, json, httpx

params = {
    "engine": "google",
    "q": "AC repair Tampa",
    "api_key": os.environ["SERPAPI_KEY"],
    "num": "10",
    "gl": "us",
    "hl": "en",
}
r = httpx.get("https://serpapi.com/search.json", params=params, timeout=60)
d = r.json()
print("TOP-LEVEL KEYS:", sorted(d.keys()))
for key in ("ads", "local_services_ads", "local_results", "organic_results"):
    block = d.get(key)
    print(f"\n=== {key}: {type(block).__name__} ===")
    print(json.dumps(block, indent=1)[:1500] if block else "MISSING")
