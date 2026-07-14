"""
Competitor Scorer v1
Runs Google PageSpeed (PSI) across every unique competitor domain harvested by
SERP intelligence, so "we ran the same test on your competitors" is TRUE and
every comparison ships from data that already exists.

Writes to competitor_scores (shared table, keyed by domain, since the same
competitor appears for many leads). Resumable: skips domains already scored.

Usage:
  python db\\score_competitors.py --list          (show unique domains, no API calls)
  python db\\score_competitors.py --limit 10      (test batch)
  python db\\score_competitors.py                 (all pending)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, and optionally PSI_API_KEY
     (falls back to keyless PSI calls, which are rate-limited but workable)
"""
import os
import sys
import time
import json
import argparse
from urllib.parse import urlparse

import httpx

from competitor_blocklist import is_blocked

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PSI_KEY = os.environ.get("PSI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")

SLEEP_BETWEEN_CALLS = 2.0
PSI_TIMEOUT = 90


def headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if minimal:
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


def collect_competitor_domains(vertical: str = "hvac") -> dict:
    """Unique competitor domains -> a display name, from the pool's serp_competitors."""
    domains = {}
    page = 0
    while True:
        params = {
            "select": "serp_competitors",
            "source": f"like.google_maps:{vertical}*",
            "serp_competitors": "not.is.null",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params,
                      headers=headers(False), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for row in batch:
            comp = row.get("serp_competitors")
            if isinstance(comp, str):
                try:
                    comp = json.loads(comp)
                except Exception:
                    continue
            if not comp:
                continue
            for slot in ("map_pack", "organic_top3", "ads", "lsa"):
                for item in comp.get(slot) or []:
                    d = normalise_domain(item.get("domain") or "")
                    n = (item.get("name") or "").strip()
                    if d and d not in domains:
                        domains[d] = n
        page += 1
    return domains


def already_scored() -> set:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/competitor_scores",
                  params={"select": "domain", "limit": "10000"},
                  headers=headers(False), timeout=30)
    r.raise_for_status()
    return {row["domain"] for row in r.json()}


def psi_mobile_lcp(domain: str):
    """Returns (lcp_seconds, perf_score) or (None, None) on failure."""
    params = {
        "url": f"https://{domain}/",
        "strategy": "mobile",
        "category": "performance",
    }
    if PSI_KEY:
        params["key"] = PSI_KEY
    r = httpx.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                  params=params, timeout=PSI_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    audits = (d.get("lighthouseResult") or {}).get("audits") or {}
    lcp_ms = (audits.get("largest-contentful-paint") or {}).get("numericValue")
    perf = ((d.get("lighthouseResult") or {}).get("categories") or {}).get("performance", {}).get("score")
    lcp_s = round(lcp_ms / 1000, 1) if lcp_ms is not None else None
    perf_100 = int(perf * 100) if perf is not None else None
    return lcp_s, perf_100


def save(domain: str, name: str, lcp, perf) -> bool:
    row = {"domain": domain, "name": name, "psi_mobile_lcp": lcp,
           "psi_mobile_perf": perf, "scored_at": "now()"}
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/competitor_scores", json=row,
                   headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SAVE FAILED {domain}: {r.status_code} {r.text[:150]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--home-state", default="FL", dest="home_state")
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    domains = collect_competitor_domains(args.vertical)

    blocked, clean = {}, {}
    for d, n in domains.items():
        hit, reason = is_blocked(d, n, home_state=args.home_state)
        if hit:
            blocked[d] = reason
        else:
            clean[d] = n

    done = already_scored() if not args.list else set()
    pending = {d: n for d, n in clean.items() if d not in done}

    print(f"Unique competitor domains: {len(domains)} | blocked: {len(blocked)} | "
          f"already scored: {len(done)} | pending: {len(pending)}")
    if args.list:
        if blocked:
            print("\nBLOCKED (will not be scored):")
            for d, reason in sorted(blocked.items()):
                print(f"  {d:45s} {reason}")
        print("\nPENDING:")
        for d, n in sorted(pending.items()):
            print(f"  {d:45s} {n}")
        return

    items = list(pending.items())
    if args.limit:
        items = items[:args.limit]
    if not PSI_KEY:
        print("NOTE: no PSI_API_KEY set, using keyless calls (rate-limited, slower).")

    stats = {"scored": 0, "errors": 0}
    for domain, name in items:
        try:
            lcp, perf = psi_mobile_lcp(domain)
            if save(domain, name, lcp, perf):
                stats["scored"] += 1
                print(f"  {domain:45s} LCP {lcp if lcp is not None else '?':>5}s | perf {perf if perf is not None else '?'}")
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR {domain}: {str(e)[:100]}")
        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone. scored={stats['scored']} errors={stats['errors']}")


if __name__ == "__main__":
    main()
