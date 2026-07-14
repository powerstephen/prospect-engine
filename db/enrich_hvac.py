"""
enrich_hvac.py - Batch email enrichment for scraped leads (SCRAPE-ONLY, quality-first).

Pass 1 of the enrichment pipeline:
  Stage 1 website scrape only. Accepts on-domain emails and genuine personal
  inboxes (gmail/yahoo/etc). Rejects other-company emails (dev/agency addresses).
  Found -> status=enriched. Not found -> stays status=new (flows to QuickEnrich).

Deliberately does NOT use Google search or pattern guessing here. Those are
separate, lower-confidence channels handled later.

Usage:
  python -m db.enrich_hvac                 # DRY RUN (default)
  python -m db.enrich_hvac --live          # writes verified scraped emails
  python -m db.enrich_hvac --live --limit 200
  python -m db.enrich_hvac --source google_maps:roofers --live
"""
import os, sys, asyncio, httpx

from db.enrich import stage1_website_scrape, clean_domain

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

LIVE = "--live" in sys.argv

def _arg(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default

SOURCE = _arg("--source", "google_maps:hvac")
LIMIT  = int(_arg("--limit", "5000"))

# Free personal inboxes that are legitimate business emails for SMB contractors.
FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "icloud.com", "me.com", "live.com", "msn.com", "comcast.net",
    "bellsouth.net", "att.net", "verizon.net", "sbcglobal.net", "ymail.com",
}

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

def email_domain(email):
    return email.split("@")[-1].strip().lower()

def acceptable_email(email, company_domain):
    """Accept on-domain emails or genuine personal inboxes. Reject other-company."""
    if not email:
        return False
    ed = email_domain(email)
    cd = clean_domain(company_domain)
    if cd and cd in ed:
        return True            # on the company's own domain
    if ed in FREE_PROVIDERS:
        return True            # real personal inbox (joeshvac@gmail.com)
    return False               # other company (dev/agency) -> reject

async def fetch_targets(client):
    rows = []
    offset = 0
    page = 1000
    while True:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/contacts",
            headers=SB_HEADERS,
            params={
                "select": "id,company,website,email,status,source",
                "source": f"eq.{SOURCE}",
                "status": "eq.new",
                "limit": str(page),
                "offset": str(offset),
            },
        )
        batch = r.json()
        if isinstance(batch, dict):
            raise RuntimeError(f"Supabase error: {batch}")
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return [c for c in rows if not (c.get("email") or "").strip()]

async def save_email(client, row_id, email):
    h = dict(SB_HEADERS); h["Prefer"] = "return=minimal"
    r = await client.patch(
        f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{row_id}",
        headers=h,
        json={"email": email, "status": "enriched"},
    )
    return r.status_code in (200, 201, 204)

async def main():
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY env var required")
        return
    mode = "LIVE" if LIVE else "DRY RUN (no writes)"
    print(f"=== enrich_hvac (scrape-only) :: {mode} :: source={SOURCE} :: limit {LIMIT} ===\n")

    async def log(msg):
        print(msg)

    async with httpx.AsyncClient(timeout=30) as client:
        targets = await fetch_targets(client)
        targets = targets[:LIMIT]
        print(f"Targets needing email: {len(targets)}\n")

        found = 0
        rejected = 0
        not_found = 0
        errors = 0

        for i, contact in enumerate(targets, 1):
            company = contact.get("company", "?")
            website = (contact.get("website") or "").strip()
            print(f"[{i}/{len(targets)}] {company}")
            if not website:
                not_found += 1
                continue
            if not website.startswith("http"):
                website = "https://" + website
            domain = clean_domain(website)

            try:
                emails = await stage1_website_scrape(website, domain, log)
            except Exception as e:
                print(f"    ! scrape error: {e}")
                errors += 1
                continue

            # filter to acceptable, prefer on-domain
            acceptable = [e for e in emails if acceptable_email(e, domain)]
            on_domain = [e for e in acceptable if clean_domain(domain) in email_domain(e)]
            chosen = None
            if on_domain:
                for pref in ["info@", "contact@", "hello@", "office@"]:
                    for e in on_domain:
                        if e.startswith(pref):
                            chosen = e; break
                    if chosen: break
                chosen = chosen or on_domain[0]
            elif acceptable:
                chosen = acceptable[0]   # personal inbox

            if not chosen:
                if emails:
                    print(f"    -> found only off-domain ({emails[0]}) -> rejected, leaving for QuickEnrich")
                    rejected += 1
                else:
                    not_found += 1
                continue

            print(f"    -> accepted: {chosen}")
            if LIVE:
                ok = await save_email(client, contact["id"], chosen)
                if ok:
                    found += 1
                else:
                    print(f"    ! save failed")
                    errors += 1
            else:
                found += 1
            await asyncio.sleep(0.5)

        print(f"\n=== Done ===")
        print(f"  accepted (enriched): {found}" + ("" if LIVE else "  [dry run, nothing written]"))
        print(f"  off-domain rejected: {rejected}  (left as new -> QuickEnrich)")
        print(f"  no email on site:    {not_found}  (left as new -> QuickEnrich)")
        print(f"  errors:              {errors}")
        if targets:
            print(f"  scrape hit rate:     {round(100*found/len(targets))}%")
            print(f"  -> {rejected + not_found} contacts will flow to QuickEnrich")

if __name__ == "__main__":
    asyncio.run(main())
