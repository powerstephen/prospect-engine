"""
Email enrichment v4 - three-stage approach with provenance and a noise filter.

v4 changes from v3:
  - NOISE_PATTERNS: blocks known non-business addresses (Wix Sentry, web-dev
    "site by..." credits, font foundries, website-builder placeholders,
    template literal domains like example.com/domain.com) BEFORE they can
    ever be returned as a result.
  - Every candidate is validated against a real email shape; malformed
    strings (image filenames, asset paths) are rejected outright.
  - Stage 2 query upgraded from a bare "@domain.com" search to "email for
    {domain}", which is what actually finds directory/AI-overview results
    (proven manually: this exact query found a named president's direct
    email that the old bare-domain search missed entirely).
  - Every result now carries an email_source stage tag (stage1_scrape /
    stage2_search / stage3_pattern_guess) and email_confidence
    (high/medium/low). Stage 3 pattern guesses are NEVER reported with the
    same confidence as a real scrape or search hit, they are the last
    resort, not a peer of the other two stages.
  - "0 errors, 100% hit rate" can no longer happen silently: low-confidence
    guesses are counted and reported separately in run_bulk_enrich's summary.
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

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
EMAIL_FIND_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)

IGNORE_PATTERNS = [
    "sentry.io", "sentry-next.wixpress.com", "google.com", "facebook.com",
    "schema.org", "w3.org", "wordpress.org", "wix.com", "squarespace.com",
    "noreply", "no-reply", "donotreply", "mailer", "bounce",
    "postmaster", "webmaster", "privacy", "support@wix",
    "support@webador.com", "support@townsquareinteractive.com",
    "clients@townsquareinteractive.com", "filler@godaddy.com",
]

KNOWN_NOISE_EMAILS = {
    "eben@eyebytes.com", "jonpinhorn.typedesign@gmail.com",
    "impallari@gmail.com", "team@latofonts.com", "matt@pixelspread.com",
}

PLACEHOLDER_EMAILS = {
    "jane@example.com", "user@domain.com", "email@domain.com",
    "your@email.com", "you@email.com", "name@example.com",
    "test@test.com", "sample@sample.com", "info@bbb.org",
}

# Any address on these domains is placeholder boilerplate regardless of the
# local part (info@example.com is exactly as fake as jane@example.com).
PLACEHOLDER_DOMAINS = {"example.com", "domain.com", "email.com", "test.com", "sample.com"}

# Asset-file extensions that a loose regex can mistake for a TLD (a URL
# fragment like ".../2x.ck7nhwq8.webp" matches the email shape by accident).
ASSET_FALSE_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "css", "js", "webp",
    "ico", "woff", "woff2", "ttf", "eot",
}

CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact-us/", "/about",
    "/about-us", "/reach-us", "/get-in-touch", "/our-team",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COMMON_PREFIXES = ["info", "contact", "hello", "office", "admin", "mail", "enquiries", "enquiry"]


def clean_domain(website):
    url = website.strip().lower()
    for prefix in ["https://www.", "http://www.", "https://", "http://", "www."]:
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/").split("/")[0]


def is_valid_email_shape(email):
    if not EMAIL_RE.match(email):
        return False
    tld = email.rsplit(".", 1)[-1].lower()
    if tld in ASSET_FALSE_TLDS:
        return False
    return True


def is_noise(email):
    e = email.lower()
    if e in KNOWN_NOISE_EMAILS or e in PLACEHOLDER_EMAILS:
        return True
    domain_part = e.split("@")[-1] if "@" in e else ""
    if domain_part in PLACEHOLDER_DOMAINS:
        return True
    if any(p in e for p in IGNORE_PATTERNS):
        return True
    return False


def extract_emails(text, domain):
    found = EMAIL_FIND_RE.findall(text)
    emails = []
    seen = set()
    for email in found:
        email = email.lower().strip(".,;:\"'><()")
        if email in seen:
            continue
        seen.add(email)
        if not is_valid_email_shape(email):
            continue
        if is_noise(email):
            continue
        if any(email.endswith(x) for x in [".png", ".jpg", ".gif", ".svg", ".css", ".js", ".webp"]):
            continue
        emails.append(email)

    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]
    other_emails  = [e for e in emails if domain_clean not in e]
    return domain_emails + other_emails


async def scrape_url(url):
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=HEADERS, verify=False) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.text
    except Exception:
        pass
    return ""


async def stage1_website_scrape(website, domain, log):
    await log("  Stage 1: Scraping website...")

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
            await asyncio.sleep(0.3)

    if all_emails:
        await log("  OK Stage 1: Found " + str(len(all_emails)) + " email(s) on website")
    else:
        await log("  -> Stage 1: No emails on website")

    return all_emails


async def stage2_google_search(domain, company, log):
    if not SERPAPI_KEY:
        await log("  -> Stage 2: No SerpAPI key, skipping")
        return []

    await log("  Stage 2: Searching 'email for " + domain + "'...")

    try:
        import httpx
        params = {
            "api_key": SERPAPI_KEY,
            "engine": "google",
            "q": "email for " + domain,
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
            text = " ".join([
                result.get("title", ""),
                result.get("snippet", ""),
                result.get("link", ""),
            ])
            all_emails.extend(extract_emails(text, domain))

        ao = data.get("answer_box") or data.get("ai_overview")
        if isinstance(ao, dict):
            blob = " ".join(str(v) for v in ao.values() if isinstance(v, str))
            all_emails.extend(extract_emails(blob, domain))

        if all_emails:
            await log("  OK Stage 2: Found " + str(len(all_emails)) + " email(s) via search")
        else:
            await log("  -> Stage 2: No emails found via search")

        return all_emails

    except Exception as e:
        await log("  X Stage 2 error: " + str(e))
        return []


async def stage3_pattern_guess(domain, contact, log):
    await log("  Stage 3: Trying common email patterns (UNVERIFIED)...")

    candidates = []
    for prefix in COMMON_PREFIXES:
        candidates.append(prefix + "@" + domain)

    first = (contact.get("first_name") or "").strip().lower()
    last  = (contact.get("last_name") or "").strip().lower()

    if first and last:
        candidates.extend([
            first + "@" + domain,
            first + "." + last + "@" + domain,
            first[0] + last + "@" + domain,
            first + "_" + last + "@" + domain,
        ])
    elif first:
        candidates.append(first + "@" + domain)

    await log("  -> Stage 3: Generated " + str(len(candidates)) + " pattern candidates (unverified)")

    top = [c for c in candidates if any(c.startswith(p + "@") for p in ["info", "contact", "hello"])]
    return top[:2] if top else candidates[:1]


def pick_best_email(emails, domain):
    if not emails:
        return None

    domain_clean = clean_domain(domain)
    domain_emails = [e for e in emails if domain_clean in e]

    if domain_emails:
        for prefix in ["info@", "contact@", "hello@", "office@"]:
            for e in domain_emails:
                if e.startswith(prefix):
                    return e
        return domain_emails[0]

    return emails[0] if emails else None


async def find_emails_for_contact(contact, log_cb=None):
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass

    website = (contact.get("website") or "").strip()
    company = contact.get("company", "?")

    if not website:
        await log("  -> " + company + " - no website")
        return {}

    if contact.get("email"):
        await log("  -> " + company + " - already has email")
        return {}

    if not website.startswith("http"):
        website = "https://" + website

    domain = clean_domain(website)
    await log("  Enriching " + company + " (" + domain + ")...")

    emails = await stage1_website_scrape(website, domain, log)
    if emails:
        best = pick_best_email(emails, domain)
        if best:
            await log("  OK Best email: " + best + " (stage1_scrape, high confidence)")
            return {"email": best, "email_source": "stage1_scrape", "email_confidence": "high"}

    emails = await stage2_google_search(domain, company, log)
    if emails:
        best = pick_best_email(emails, domain)
        if best:
            await log("  OK Best email: " + best + " (stage2_search, medium confidence)")
            return {"email": best, "email_source": "stage2_search", "email_confidence": "medium"}

    emails = await stage3_pattern_guess(domain, contact, log)
    if emails:
        best = pick_best_email(emails, domain)
        if best:
            await log("  WARN Best email: " + best + " (stage3_pattern_guess, LOW confidence, UNVERIFIED)")
            return {"email": best, "email_source": "stage3_pattern_guess", "email_confidence": "low"}

    await log("  X No email found for " + company)
    return {}


async def run_bulk_enrich(ids, log_cb=None):
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass

    import httpx
    from db.supabase_client import get_contacts

    all_contacts = get_contacts(limit=5000)
    id_set   = set(ids)
    contacts = [c for c in all_contacts if c["id"] in id_set]
    to_enrich = [c for c in contacts if not c.get("email")]
    already   = len(contacts) - len(to_enrich)

    await log("Enriching " + str(len(to_enrich)) + " contacts | " + str(already) + " already have emails")

    stage_counts = {"stage1_scrape": 0, "stage2_search": 0, "stage3_pattern_guess": 0}
    not_found = 0
    errors    = 0

    sb_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for contact in to_enrich:
        result = await find_emails_for_contact(contact, log_cb=log)

        if not result:
            not_found += 1
            continue

        stage_counts[result.get("email_source", "stage3_pattern_guess")] += 1
        payload = dict(result)
        payload["status"] = "enriched"

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.patch(
                    SUPABASE_URL + "/rest/v1/contacts?id=eq." + str(contact["id"]),
                    json=payload,
                    headers=sb_headers,
                )
                if r.status_code in (200, 201, 204):
                    await log("  OK Saved")
                else:
                    await log("  X Save failed: " + str(r.status_code))
                    errors += 1
        except Exception as e:
            await log("  X Error: " + str(e))
            errors += 1

        await asyncio.sleep(0.5)

    verified = stage_counts["stage1_scrape"] + stage_counts["stage2_search"]
    guessed  = stage_counts["stage3_pattern_guess"]
    total_found = verified + guessed

    await log(
        "\nDone -- " + str(total_found) + " enriched (" + str(verified) + " verified, "
        + str(guessed) + " UNVERIFIED pattern guesses) | "
        + str(not_found) + " not found | " + str(errors) + " errors | " + str(already) + " skipped"
    )
    await log(
        "  Breakdown: stage1_scrape=" + str(stage_counts["stage1_scrape"])
        + " stage2_search=" + str(stage_counts["stage2_search"])
        + " stage3_pattern_guess(LOW CONF)=" + str(stage_counts["stage3_pattern_guess"])
    )
    return {
        "found": total_found, "verified": verified, "guessed": guessed,
        "not_found": not_found, "errors": errors, "skipped": already,
    }
