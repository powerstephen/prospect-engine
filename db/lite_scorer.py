"""
Lite Scorer v1
PSI-only speed measurement for fast campaign routing. Writes psi_mobile_lcp
per contact and nothing else. No Playwright, no page fetch, nothing that can
hang: one Google PageSpeed API call per lead, same pattern as
score_competitors.py which runs flawlessly.

Purpose: wave-1 routing (speed vs generic on the 8s threshold) without
waiting for the full v4 audit. The v4 scorer still runs later for the deep
evidence (site health, hard-to-hire signals); this script does not touch
status, so the v4 pool is unaffected.

Resumable: only fetches contacts where psi_mobile_lcp is null.

Usage:
  python db\\lite_scorer.py --vertical roofers --list        (pool size, no API calls)
  python db\\lite_scorer.py --vertical roofers --limit 10    (test batch)
  python db\\lite_scorer.py --vertical roofers               (all pending)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, and PSI_API_KEY (recommended;
     falls back to keyless PSI, which is heavily rate-limited)
"""
import os
import sys
import time
import argparse

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PSI_KEY = os.environ.get("PSI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")

SLEEP_BETWEEN_CALLS = 1.0
PSI_TIMEOUT = 60
JUNK_SITE_HINTS = ("rr.com", "comcastbiz", "centurylink", "bellsouth", "att.net", "verizon")


def headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_pending(vertical: str, limit: int = 0) -> list:
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website",
            "source": f"like.google_maps:{vertical}*",
            "psi_mobile_lcp": "is.null",
            "website": "not.is.null",
            "status": "not.in.(archived,timeout)",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params,
                      headers=headers(False), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    rows = [c for c in rows if c.get("website")
            and not any(j in c["website"].lower() for j in JUNK_SITE_HINTS)]
    if limit:
        rows = rows[:limit]
    return rows


def site_url(website: str) -> str:
    w = (website or "").strip()
    if not w.startswith("http"):
        w = "https://" + w
    return w


def psi_mobile_lcp(url: str):
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if PSI_KEY:
        params["key"] = PSI_KEY
    r = httpx.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                  params=params, timeout=PSI_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    audits = (d.get("lighthouseResult") or {}).get("audits") or {}
    lcp_ms = (audits.get("largest-contentful-paint") or {}).get("numericValue")
    return round(lcp_ms / 1000, 1) if lcp_ms is not None else None


def save(contact_id: int, lcp) -> bool:
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact_id}",
                    json={"psi_mobile_lcp": lcp}, headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SAVE FAILED {contact_id}: {r.status_code} {r.text[:150]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    pending = fetch_pending(args.vertical, args.limit)
    print(f"Lite scorer :: vertical={args.vertical} :: pending (psi_mobile_lcp null): {len(pending)}")
    if args.list:
        return
    if not PSI_KEY:
        print("NOTE: no PSI_API_KEY set, keyless PSI is rate-limited and will crawl.")

    stats = {"scored": 0, "errors": 0}
    for c in pending:
        url = site_url(c.get("website"))
        try:
            lcp = psi_mobile_lcp(url)
            if lcp is None:
                stats["errors"] += 1
                print(f"  NO LCP {c.get('company','?'):45.45s} {url}")
            elif save(c["id"], lcp):
                stats["scored"] += 1
                flag = "SPEED" if lcp >= 8 else "     "
                print(f"  {flag} {c.get('company','?'):45.45s} LCP {lcp:>5}s")
            else:
                stats["errors"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR {c.get('company','?'):45.45s} {str(e)[:90]}")
        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone. scored={stats['scored']} errors={stats['errors']}")
    print("Speed threshold preview: LCP >= 8s routes to the speed campaign.")


if __name__ == "__main__":
    main()
