import os, httpx, json
params = {
    "engine": "google",
    "q": "AC repair Fort Myers",
    "api_key": os.environ["SERPAPI_KEY"],
    "location": "Fort Myers, Florida, United States",
    "gl": "us", "hl": "en",
}
d = httpx.get("https://serpapi.com/search.json", params=params, timeout=60).json()
print(json.dumps(d.get("local_ads"), indent=1)[:3000])
