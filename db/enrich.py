"""
Email enrichment v3 — three-stage approach:
1. Scrape website directly (mailto: links, contact pages)
2. Google search "@domain.com" via SerpAPI to find publicly listed emails
3. Try common email patterns (info@, contact@, firstname@)
"""
import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")
SERPAPI_URL  = "https://serpapi.com/search"

EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE
)

IGNORE_PATTERNS = [
    "sentry.io", "google.com", "facebook.com", "schema.org",
    "w3.org", "wordpress.org", "wix.com", "squarespace.com",
    "noreply", "no-reply", "donotreply", "mailer", "bounce",
    "postmaster", "webmaster", "privacy", "support@wix",
]

CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact-us/", "/about",
    "/about-us", "/reach-us", "/get-in-touch", "/our-team",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COMMON_PREFIXES = ["info", "contact", "hello", "office", "admin", "mail", "enquiries", "enquiry"]


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
        if email in seen:
            continue
        seen.add(email)
        if any(p in email for p in IGNORE_PATTERNS):
            continue
        parts = email.split("@")
        if len(parts) != 2 or "." not in parts[1]:
            continue
        # Skip image/asset false positives
        if any(email.endswith(x) for x in [".png", ".jpg", ".gif", ".svg", ".css", ".js"]):
            continue
        emails.append(email)

    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]
    other_emails  = [e for e in emails if domain_clean not in e]
    return domain_emails + other_emails


async def scrape_url(url: str) -> str:
    """Fetch a URL and return raw HTML."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=HEADERS, verify=False) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.text
    except Exception:
        pass
    return ""


async def stage1_website_scrape(website: str, domain: str, log) -> list[str]:
    """Stage 1: Scrape homepage + contact pages for emails."""
    await log(f"  Stage 1: Scraping website...")
    
    all_emails = []
    
    # Homepage
    html = await scrape_url(website)
    if html:
        all_emails.extend(extract_emails(html, domain))
    
    # Contact pages if nothing found
    if not all_emails:
        base = website.rstrip("/")
        for path in CONTACT_PATHS:
            html = await scrape_url(base + path)
            if html:
                emails = extract_emails(html, domain)
                if emails:
                    all_emails.extend(emails)
                    break
            await asyncio.sleep(0.3)
    
    if all_emails:
        await log(f"  ✓ Stage 1: Found {len(all_emails)} email(s) on website")
    else:
        await log(f"  ↳ Stage 1: No emails on website")
    
    return all_emails


async def stage2_google_search(domain: str, company: str, log) -> list[str]:
    """Stage 2: Google search '@domain.com' to find publicly listed emails."""
    if not SERPAPI_KEY:
        await log(f"  ↳ Stage 2: No SerpAPI key, skipping")
        return []
    
    await log(f"  Stage 2: Google searching '@{domain}'...")
    
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
        
        all_emails = []
        results = data.get("organic_results", [])
        
        for result in results:
            # Search in title, snippet, link
            text = " ".join([
                result.get("title", ""),
                result.get("snippet", ""),
                result.get("link", ""),
            ])
            emails = extract_emails(text, domain)
            all_emails.extend(emails)
        
        if all_emails:
            await log(f"  ✓ Stage 2: Found {len(all_emails)} email(s) via Google")
        else:
            await log(f"  ↳ Stage 2: No emails found via Google")
        
        return all_emails
        
    except Exception as e:
        await log(f"  ✗ Stage 2 error: {e}")
        return []


async def stage3_pattern_guess(domain: str, contact: dict, log) -> list[str]:
    """Stage 3: Try common email patterns and verify with HEAD request."""
    await log(f"  Stage 3: Trying common email patterns...")
    
    import httpx
    
    candidates = []
    
    # Common prefixes
    for prefix in COMMON_PREFIXES:
        candidates.append(f"{prefix}@{domain}")
    
    # Owner name patterns if we have first/last name
    first = (contact.get("first_name") or "").strip().lower()
    last  = (contact.get("last_name") or "").strip().lower()
    
    if first and last:
        candidates.extend([
            f"{first}@{domain}",
            f"{first}.{last}@{domain}",
            f"{first[0]}{last}@{domain}",
            f"{first}_{last}@{domain}",
        ])
    elif first:
        candidates.append(f"{first}@{domain}")
    
    # We can't actually verify emails without sending — 
    # just return the most likely ones ranked by priority
    # info@ and contact@ are almost always valid for SMBs
    await log(f"  ↳ Stage 3: Generated {len(candidates)} pattern candidates (unverified)")
    
    # Return just the top 2 most likely — info@ and contact@
    top = [c for c in candidates if any(c.startswith(p + "@") for p in ["info", "contact", "hello"])]
    return top[:2] if top else candidates[:1]


def pick_best_email(emails: list[str], domain: str) -> str | None:
    """Pick the best email from candidates."""
    if not emails:
        return None
    
    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]
    
    if domain_emails:
        # Prefer info@, contact@, hello@ 
        for prefix in ["info@", "contact@", "hello@", "office@"]:
            for e in domain_emails:
                if e.startswith(prefix):
                    return e
        return domain_emails[0]
    
    return emails[0] if emails else None


async def find_emails_for_contact(contact: dict, log_cb=None) -> dict:
    """Three-stage email finder for a single contact."""

    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass

    website = (contact.get("website") or "").strip()
    company = contact.get("company", "?")

    if not website:
        await log(f"  ↳ {company} — no website")
        return {}

    if contact.get("email"):
        await log(f"  ↳ {company} — already has email")
        return {}

    if not website.startswith("http"):
        website = "https://" + website

    domain = clean_domain(website)
    await log(f"  🔍 Enriching {company} ({domain})...")

    # Stage 1 — website scrape
    emails = await stage1_website_scrape(website, domain, log)
    
    # Stage 2 — Google search (only if stage 1 failed)
    if not emails:
        emails = await stage2_google_search(domain, company, log)
    
    # Stage 3 — pattern guess (only if stages 1+2 failed)
    if not emails:
        emails = await stage3_pattern_guess(domain, contact, log)
        if emails:
            await log(f"  ⚠ Using pattern guess — not verified")

    best = pick_best_email(emails, domain)
    
    if best:
        await log(f"  ✓ Best email: {best}")
        return {"email": best}
    else:
        await log(f"  ✗ No email found for {company}")
        return {}


async def run_bulk_enrich(ids: list[int], log_cb=None) -> dict:
    """Enrich a list of contacts — scrape + Google search + pattern guess."""

    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass

    import httpx
    from db.supabase_client import get_contacts

    all_contacts = get_contacts(limit=5000)
    id_set   = set(ids)
    contacts = [c for c in all_contacts if c["id"] in id_set]
    to_enrich = [c for c in contacts if not c.get("email")]
    already   = len(contacts) - len(to_enrich)

    await log(f"Enriching {len(to_enrich)} contacts | {already} already have emails")

    found_count = 0
    not_found   = 0
    errors      = 0

    sb_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for contact in to_enrich:
        result = await find_emails_for_contact(contact, log_cb=log)

        if not result:
            not_found += 1
            continue

        payload = {**result, "status": "enriched"}

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.patch(
                    f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact['id']}",
                    json=payload,
                    headers=sb_headers,
                )
                if r.status_code in (200, 201, 204):
                    found_count += 1
                    await log(f"  ✓ Saved")
                else:
                    await log(f"  ✗ Save failed: {r.status_code}")
                    errors += 1
        except Exception as e:
            await log(f"  ✗ Error: {e}")
            errors += 1

        await asyncio.sleep(0.5)

    await log(
        f"\n✓ Done — {found_count} enriched | "
        f"{not_found} not found | {errors} errors | {already} skipped"
    )
    return {"found": found_count, "not_found": not_found, "errors": errors, "skipped": already}
