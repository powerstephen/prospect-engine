"""
Email scraper enrichment — finds emails directly from company websites.
No API credits needed. Works great for local trade businesses.
Strategy:
1. Scrape homepage for mailto: links
2. Try /contact, /contact-us, /about pages
3. Extract emails from HTML using regex
4. Write back to Supabase
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
 
# Email regex — finds emails in HTML
EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE
)
 
# Emails to ignore — generic/spam traps
IGNORE_PATTERNS = [
    "example.com", "domain.com", "email.com", "test.com",
    "sentry.io", "google.com", "facebook.com", "schema.org",
    "w3.org", "wordpress.com", "wix.com", "squarespace.com",
    "yourdomain", "yourname", "info@info", "noreply", "no-reply",
    "donotreply", "mailer", "bounce", "postmaster", "webmaster",
    "@2x", ".png", ".jpg", ".gif", ".svg", ".css", ".js",
]
 
CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/contact-us/",
    "/about",
    "/about-us",
    "/reach-us",
    "/get-in-touch",
]
 
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
 
 
def extract_emails(html: str, domain: str) -> list[str]:
    """Extract valid emails from HTML, filtered to domain-relevant ones."""
    found = EMAIL_RE.findall(html)
    
    emails = []
    seen = set()
    
    for email in found:
        email = email.lower().strip(".,;:\"'><()")
        
        # Skip if already seen
        if email in seen:
            continue
        seen.add(email)
        
        # Skip obvious non-emails
        if any(p in email for p in IGNORE_PATTERNS):
            continue
        
        # Skip if no proper TLD
        parts = email.split("@")
        if len(parts) != 2:
            continue
        if "." not in parts[1]:
            continue
            
        emails.append(email)
    
    # Prefer emails from the same domain
    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]
    other_emails = [e for e in emails if domain_clean not in e]
    
    # Return domain emails first, then others
    return domain_emails + other_emails
 
 
async def scrape_emails_from_url(url: str) -> list[str]:
    """Fetch a URL and extract emails."""
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers=HEADERS,
            verify=False,
        ) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return extract_emails(r.text, url)
    except Exception:
        pass
    return []
 
 
async def find_emails_for_contact(contact: dict, log_cb=None) -> dict:
    """
    Find emails for a contact by scraping their website.
    Returns dict with email and any other found data.
    """
    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass
 
    website = (contact.get("website") or "").strip()
    company = contact.get("company", "?")
 
    if not website:
        await log(f"  ↳ {company} — no website")
        return {}
 
    # Already has email
    if contact.get("email"):
        await log(f"  ↳ {company} — already has email ({contact['email']})")
        return {}
 
    if not website.startswith("http"):
        website = "https://" + website
 
    domain = clean_domain(website)
    await log(f"  🔍 Scraping {company} ({domain})...")
 
    all_emails = []
 
    # 1. Scrape homepage
    emails = await scrape_emails_from_url(website)
    all_emails.extend(emails)
 
    # 2. Try contact pages if no emails found yet
    if not all_emails:
        base = website.rstrip("/")
        for path in CONTACT_PATHS:
            emails = await scrape_emails_from_url(base + path)
            if emails:
                all_emails.extend(emails)
                break
            await asyncio.sleep(0.3)
 
    # 3. Also check for mailto: specifically (higher confidence)
    mailto_re = re.compile(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.IGNORECASE)
 
    if all_emails:
        # Pick the best email — prefer domain match, then info/contact
        domain_emails = [e for e in all_emails if domain in e]
        
        if domain_emails:
            # Prefer info@, contact@, hello@ over random ones
            preferred = None
            for prefix in ["info@", "contact@", "hello@", "office@", "admin@"]:
                for e in domain_emails:
                    if e.startswith(prefix):
                        preferred = e
                        break
                if preferred:
                    break
            best_email = preferred or domain_emails[0]
        else:
            best_email = all_emails[0]
 
        await log(f"  ✓ Found: {best_email} (from {len(all_emails)} candidates)")
        return {"email": best_email}
    else:
        await log(f"  ↳ No emails found on {domain}")
        return {}
 
 
async def run_bulk_enrich(ids: list[int], log_cb=None) -> dict:
    """Scrape emails for a list of contacts and write back to Supabase."""
 
    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass
 
    import httpx
    from db.supabase_client import get_contacts
 
    all_contacts = get_contacts(limit=5000)
    id_set   = set(ids)
    contacts = [c for c in all_contacts if c["id"] in id_set]
 
    # Skip contacts that already have emails
    to_enrich = [c for c in contacts if not c.get("email")]
    already   = len(contacts) - len(to_enrich)
 
    await log(f"Email scraper — {len(to_enrich)} to enrich, {already} already have emails")
 
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
                    await log(f"  ✓ Saved to Supabase")
                else:
                    await log(f"  ✗ Save failed: {r.status_code}")
                    errors += 1
        except Exception as e:
            await log(f"  ✗ Save error: {e}")
            errors += 1
 
        await asyncio.sleep(0.5)
 
    await log(
        f"\n✓ Done — {found_count} emails found | "
        f"{not_found} not found | {errors} errors | {already} skipped (already had email)"
    )
    return {"found": found_count, "not_found": not_found, "errors": errors, "skipped": already}
