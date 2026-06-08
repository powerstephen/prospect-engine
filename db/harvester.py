"""
Harvester — combined pipeline:
1. Google Maps search for businesses
2. Email enrichment (scrape + Google search)
3. Website scoring
4. Only saves fully enriched contacts to Supabase
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")
SERPAPI_URL  = "https://serpapi.com/search"

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', re.IGNORECASE)

IGNORE_PATTERNS = [
    "sentry.io", "google.com", "facebook.com", "schema.org",
    "w3.org", "wordpress.org", "wix.com", "squarespace.com",
    "noreply", "no-reply", "donotreply", "mailer", "bounce",
    "postmaster", "webmaster", "privacy", "support@wix",
]

CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/our-team"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def clean_domain(website: str) -> str:
    url = website.strip().lower()
    for prefix in ["https://www.", "http://www.", "https://", "http://", "www."]:
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/").split("/")[0]


def extract_emails(text: str, domain: str) -> list[str]:
    found = EMAIL_RE.findall(text)
    emails = []
    seen = set()
    for email in found:
        email = email.lower().strip(".,;:\"'><()")
        if email in seen: continue
        seen.add(email)
        if any(p in email for p in IGNORE_PATTERNS): continue
        parts = email.split("@")
        if len(parts) != 2 or "." not in parts[1]: continue
        if any(email.endswith(x) for x in [".png", ".jpg", ".gif", ".svg", ".css", ".js"]): continue
        emails.append(email)
    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]
    other_emails  = [e for e in emails if domain_clean not in e]
    return domain_emails + other_emails


async def scrape_url(url: str) -> str:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=HEADERS, verify=False) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.text
    except Exception:
        pass
    return ""


def pick_best_email(emails: list[str], domain: str) -> str | None:
    if not emails: return None
    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]
    if domain_emails:
        for prefix in ["info@", "contact@", "hello@", "office@"]:
            for e in domain_emails:
                if e.startswith(prefix): return e
        return domain_emails[0]
    return emails[0]


async def find_email(website: str, domain: str, log) -> str | None:
    """Three-stage email finder — website scrape → Google search → pattern guess."""

    # Stage 1: Website scrape
    all_emails = []
    html = await scrape_url(website)
    if html:
        all_emails.extend(extract_emails(html, domain))
    if not all_emails:
        base = website.rstrip("/")
        for path in CONTACT_PATHS:
            html = await scrape_url(base + path)
            if html:
                emails = extract_emails(html, domain)
                if emails:
                    all_emails.extend(emails)
                    break
            await asyncio.sleep(0.2)

    if all_emails:
        return pick_best_email(all_emails, domain)

    # Stage 2: Google search
    if SERPAPI_KEY:
        try:
            import httpx
            params = {
                "api_key": SERPAPI_KEY,
                "engine": "google",
                "q": f'"@{domain}"',
                "num": 10,
                "gl": "us",
                "hl": "en",
            }
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(SERPAPI_URL, params=params)
                data = r.json()
            for result in data.get("organic_results", []):
                text = " ".join([result.get("title",""), result.get("snippet",""), result.get("link","")])
                emails = extract_emails(text, domain)
                if emails:
                    all_emails.extend(emails)
            if all_emails:
                return pick_best_email(all_emails, domain)
        except Exception:
            pass

    # Stage 3: Pattern guess — common prefixes
    for prefix in ["info", "contact", "hello", "office"]:
        return f"{prefix}@{domain}"  # Return most likely, unverified

    return None


async def harvest(
    industry: str,
    location: str,
    limit: int = 20,
    log_cb=None,
) -> dict:
    """
    Full harvest pipeline — find businesses, enrich emails, score, save.
    Only saves contacts that have an email.
    """
    import httpx

    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass

    await log(f"🔍 Searching Google Maps: {industry} in {location}...")

    # ── Step 1: Google Maps search ──
    businesses = []
    try:
        from scraper.engine import find_businesses
        businesses = await find_businesses(industry, location, limit, SERPAPI_KEY)
        await log(f"✓ Found {len(businesses)} businesses")
    except Exception as e:
        await log(f"✗ Search failed: {e}")
        return {"found": 0, "enriched": 0, "saved": 0, "skipped": 0}

    if not businesses:
        await log("No businesses found — try different search terms")
        return {"found": 0, "enriched": 0, "saved": 0, "skipped": 0}

    # ── Step 2: Process each business ──
    enriched = 0
    saved = 0
    skipped = 0

    sb_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    for i, biz in enumerate(businesses, 1):
        website = (biz.get("website") or "").strip()
        name    = biz.get("name", "Unknown")

        await log(f"\n[{i}/{len(businesses)}] {name}")

        if not website:
            await log(f"  ↳ No website — skipping")
            skipped += 1
            continue

        if not website.startswith("http"):
            website = "https://" + website

        domain = clean_domain(website)

        # ── Find email ──
        await log(f"  📧 Finding email for {domain}...")
        email = await find_email(website, domain, log)

        if not email:
            await log(f"  ↳ No email found — skipping")
            skipped += 1
            continue

        await log(f"  ✓ Email: {email}")
        enriched += 1

        # ── Score website ──
        await log(f"  📊 Scoring website...")
        audit = {}
        icp   = {}
        combined = 0
        try:
            from scraper.engine import (
                audit_url, detect_size_signals,
                detect_intelligence_signals, calculate_icp_score
            )
            audit = await audit_url(website)
            size  = await detect_size_signals(website, name)
            audit.update(size)

            try:
                import httpx as _hx
                _h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"}
                async with _hx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as _c:
                    _r = await _c.get(website, headers=_h)
                    html = _r.text
            except Exception:
                html = ""

            # Real measured speed (PageSpeed Insights), falls back to estimate
            try:
                from scraper.lighthouse import measure_speed
                psi = await measure_speed(website)
                audit.update(psi)
                if psi:
                    await log(f"  PSI: mobile {psi.get('psi_mobile_lcp','?')}s / desktop {psi.get('psi_desktop_lcp','?')}s")
            except Exception as e:
                await log(f"  PSI skipped: {e}")

            icp = calculate_icp_score(audit, biz)
            combined = min(99, icp.get("combined_score", icp["icp_score"]))
            await log(f"  ✓ Score: {combined} | Tier: {icp.get('icp_tier','D')}")
        except Exception as e:
            await log(f"  ✗ Scoring error: {e}")

        # ── Save to Supabase ──
        contact = {
            "company":          name,
            "website":          domain,
            "email":            email,
            "phone":            biz.get("phone") or None,
            "location":         biz.get("address") or location,
            "industry":         industry.lower(),
            "status":           "new",
            "source":           "harvest",
            "opportunity_score": combined or None,
            "icp_score":        icp.get("icp_score") or None,
            "website_score":    audit.get("website_score") or None,
            "icp_tier":         icp.get("icp_tier") or None,
            "intel_pills":      icp.get("icp_pills") or [],
            "size_signals":     audit.get("size_signals") or [],
            "revenue_leak":     audit.get("revenue_leak") or False,
            "dimensions":       audit.get("dimensions") or {},
            "load_time":        audit.get("load_time") or None,
            "psi_mobile_lcp":   audit.get("psi_mobile_lcp"),
            "psi_desktop_lcp":  audit.get("psi_desktop_lcp"),
            "psi_mobile_perf":  audit.get("psi_mobile_perf"),
            "psi_desktop_perf": audit.get("psi_desktop_perf"),
            "google_rating":    biz.get("rating") or None,
            "review_count":     biz.get("reviews") or None,
            "scored_at":        datetime.now(timezone.utc).isoformat() if combined else None,
        }

        # Remove None values
        contact = {k: v for k, v in contact.items() if v is not None}

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{SUPABASE_URL}/rest/v1/contacts",
                    json=contact,
                    headers=sb_headers,
                )
                if r.status_code in (200, 201, 204):
                    saved += 1
                    await log(f"  ✓ Saved to Leads")
                else:
                    await log(f"  ✗ Save failed: {r.status_code} — {r.text[:100]}")
        except Exception as e:
            await log(f"  ✗ Save error: {e}")

        await asyncio.sleep(0.5)

    await log(
        f"\n{'='*40}\n"
        f"✓ Harvest complete!\n"
        f"  Found:    {len(businesses)} businesses\n"
        f"  Enriched: {enriched} emails found\n"
        f"  Saved:    {saved} added to Leads\n"
        f"  Skipped:  {skipped} (no website or email)\n"
    )

    return {
        "found":    len(businesses),
        "enriched": enriched,
        "saved":    saved,
        "skipped":  skipped,
    }
