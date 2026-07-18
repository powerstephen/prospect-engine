"""
Roaster Bot Ã¢â‚¬â€ Core Engine v4
10 dimensions x 10 points = 100 total score.
Low score = high opportunity.
Mobile scoring overhauled Ã¢â‚¬â€ page builders, image overflow, real mobile signals.
Combined score weighted toward website/mobile quality Ã¢â‚¬â€ ICP is a modifier not a gate.
"""

import asyncio
import re
import time
import httpx

SERPAPI_URL = "https://serpapi.com/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"


PLATFORM_DOMAINS = {
    "instagram.com", "facebook.com", "google.com", "yelp.com",
    "linktr.ee", "business.site", "godaddysites.com", "wixsite.com",
    "square.site", "linkedin.com", "youtube.com", "tiktok.com",
}
JUNK_TLDS = (".top", ".site", ".online", ".best", ".homes", ".pro", ".xyz", ".icu", ".click")


def _clean_domain(website):
    url = (website or "").strip().lower()
    for p in ["https://www.", "http://www.", "https://", "http://", "www."]:
        if url.startswith(p):
            url = url[len(p):]
    return url.split("/")[0].split("?")[0]


def is_junk_domain(dom):
    for p in PLATFORM_DOMAINS:
        if dom == p or dom.endswith("." + p):
            return True, "platform (" + p + ")"
    for tld in JUNK_TLDS:
        if dom.endswith(tld):
            return True, "junk TLD (" + tld + ")"
    return False, ""


async def _maps_page(query, start, api_key):
    params = {
        "api_key": api_key, "engine": "google_maps", "q": query,
        "type": "search", "hl": "en", "gl": "us", "start": str(start),
    }
    for attempt in range(4):
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(SERPAPI_URL, params=params)
        if r.status_code == 429:
            await asyncio.sleep(10 * (2 ** attempt))
            continue
        r.raise_for_status()
        return r.json().get("local_results") or []
    raise RuntimeError("rate limited after retries")


async def find_businesses(industry: str, location: str, limit: int, api_key: str) -> list:
    """No hardcoded geo anchor (was silently biasing every search toward
    Miami), real pagination up to limit, junk-domain results filtered
    out before they ever reach enrichment."""
    query = industry + " in " + location
    out = []
    seen_domains = set()
    page = 0
    while len(out) < limit and page < 5:
        try:
            results = await _maps_page(query, page * 20, api_key)
        except Exception:
            break
        if not results:
            break
        for p in results:
            if len(out) >= limit:
                break
            website = (p.get("website") or "").strip()
            if not website:
                continue
            dom = _clean_domain(website)
            junk, _reason = is_junk_domain(dom)
            if junk or dom in seen_domains:
                continue
            seen_domains.add(dom)
            out.append({
                "name": p.get("title", ""),
                "website": website,
                "rating": p.get("rating") or 0,
                "reviews": p.get("reviews") or 0,
                "address": p.get("address", ""),
                "phone": p.get("phone", ""),
                "category": p.get("type", industry),
                "google_url": p.get("link", ""),
                "thumbnail": p.get("thumbnail", ""),
            })
        page += 1
        await asyncio.sleep(1.0)
    return out


async def fetch_page(url: str, mobile: bool = False) -> tuple[str, str, float, bool]:
    if not url.startswith("http"):
        url = "https://" + url
    start = time.time()
    html, text, is_ssl = "", "", url.startswith("https://")

    headers = {**HEADERS}
    if mobile:
        headers["User-Agent"] = MOBILE_UA

    for attempt_url in [url, url.replace("https://", "http://") if url.startswith("https://") else None]:
        if not attempt_url:
            continue
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as c:
                resp = await c.get(attempt_url)
                html = resp.text
                is_ssl = str(resp.url).startswith("https://")
                clean = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
                clean = re.sub(r'<style[^>]*>.*?</style>', ' ', clean, flags=re.DOTALL | re.IGNORECASE)
                clean = re.sub(r'<[^>]+>', ' ', clean)
                clean = re.sub(r'&[a-z]+;', ' ', clean)
                text = re.sub(r'\s+', ' ', clean).lower().strip()
                break
        except Exception:
            continue

    return html, text, round(time.time() - start, 2), is_ssl


def score_mobile(html: str, load_time: float) -> dict:
    """Deep mobile scoring Ã¢â‚¬â€ detects broken page builders, image overflow, real mobile signals."""
    h = html.lower()
    issues = []
    positives = []
    score = 10

    # Broken page builder detection
    if "revslider" in h or "slider-revolution" in h or "rev_slider" in h:
        score -= 4
        issues.append("Slider Revolution Ã°Å¸Å¡Â© Ã¢â‚¬â€ notorious mobile killer")
    if "wpbakery" in h or "vc_row" in h or "vc_column" in h:
        score -= 3
        issues.append("WPBakery page builder Ã¢â‚¬â€ poor mobile rendering")
    if "et_pb_" in h or ("divi" in h and "divi-builder" in h):
        score -= 2
        issues.append("Divi builder Ã¢â‚¬â€ heavy mobile load")
    if "elementor" in h:
        if load_time > 3:
            score -= 2
            issues.append("Elementor + slow load Ã¢â‚¬â€ mobile users leaving")
        else:
            positives.append("Elementor (ok)")

    # Image overflow checks
    img_tags = re.findall(r'<img[^>]+>', html, re.IGNORECASE)
    overflow_imgs = 0
    for img in img_tags:
        img_l = img.lower()
        has_fixed_width = re.search(r'width=["\']?\d{3,}["\']?', img_l)
        has_max_width = 'max-width' in img_l or 'img-fluid' in img_l or 'w-100' in img_l
        has_class_resp = any(x in img_l for x in ['responsive', 'wp-post-image', 'attachment-', 'size-'])
        if has_fixed_width and not has_max_width and not has_class_resp:
            overflow_imgs += 1
    if overflow_imgs >= 3:
        score -= 3
        issues.append(f"{overflow_imgs} images likely overflowing on mobile Ã°Å¸Å¡Â©")
    elif overflow_imgs >= 1:
        score -= 1
        issues.append(f"{overflow_imgs} image(s) may overflow on mobile")

    # Responsive framework checks
    has_viewport = 'name="viewport"' in h or "name='viewport'" in h
    has_bootstrap = "bootstrap" in h
    has_tailwind = "tailwind" in h
    has_media = "@media" in h
    has_flex = "display:flex" in h.replace(" ", "") or "display: flex" in h

    if not has_viewport:
        score -= 4
        issues.append("No viewport meta tag Ã°Å¸Å¡Â©")
    else:
        viewport_match = re.search(r'name=["\']viewport["\'][^>]*content=["\']([^"\']+)["\']', h)
        if viewport_match and "user-scalable=no" in viewport_match.group(1):
            score -= 1
            issues.append("Viewport blocks zoom")

    if has_bootstrap or has_tailwind:
        score = min(score + 1, 10)
        positives.append("Responsive framework")
    elif has_media and has_flex:
        positives.append("Custom responsive CSS")

    # Load time (mobile ~2.5x slower than desktop)
    mobile_est = load_time * 2.5
    if mobile_est > 8:
        score -= 3
    elif mobile_est > 5:
        score -= 2
    elif mobile_est > 3:
        score -= 1

    # Tap-to-call
    has_tel_links = 'href="tel:' in h or "href='tel:" in h
    if not has_tel_links:
        score -= 1
        issues.append("No tap-to-call links")
    else:
        positives.append("Clickable phone numbers")

    # Fixed positioning abuse
    fixed_count = h.count("position:fixed") + h.count("position: fixed")
    if fixed_count >= 3:
        score -= 1
        issues.append("Heavy fixed positioning")

    score = max(0, min(10, score))
    flag = "Good" if score >= 8 else ("Needs Work" if score >= 5 else "Critical")
    status = f"Issues: {', '.join(issues[:2])}" if issues else f"Mobile: {', '.join(positives[:2])}"

    return {
        "score": score, "max": 10,
        "label": "Mobile", "icon": "Ã°Å¸â€œÂ±",
        "status": status, "flag": flag,
        "mobile_issues": issues,
        "mobile_positives": positives,
        "overflow_images": overflow_imgs,
    }


def score_site(html: str, text: str, load_time: float, is_ssl: bool) -> dict:
    h = html.lower()
    t = text
    dims = {}

    # 1. Speed
    if load_time < 2:     sv, st, sf = 10, f"Fast ({load_time}s)", "Good"
    elif load_time < 3:   sv, st, sf = 7,  f"Moderate ({load_time}s)", "Needs Work"
    elif load_time < 4:   sv, st, sf = 4,  f"Slow ({load_time}s)", "Needs Work"
    else:                 sv, st, sf = 0,  f"Very slow ({load_time}s) Ã¢â‚¬â€ 40%+ bounce", "Critical"
    dims["speed"] = {"score": sv, "max": 10, "label": "Speed", "icon": "Ã¢Å¡Â¡", "status": st, "flag": sf}

    # 2. Mobile (deep scoring)
    dims["mobile"] = score_mobile(html, load_time)

    # 3. SSL
    dims["ssl"] = {
        "score": 10 if is_ssl else 0, "max": 10,
        "label": "SSL", "icon": "Ã°Å¸â€â€™",
        "status": "Secure HTTPS" if is_ssl else "No SSL Ã¢â‚¬â€ browsers flag as unsafe",
        "flag": "Good" if is_ssl else "Critical"
    }

    # 4. CTA Ã¢â‚¬â€ including UX quality checks
    cta_strong = [
        "schedule now", "book now", "book appointment", "book online",
        "schedule appointment", "schedule online", "request appointment",
        "get a free quote", "free quote", "free estimate", "get started today",
        "call now", "contact us today", "same day", "next day service",
        "get your free", "claim your free", "start today", "instant quote",
    ]
    cta_weak = ["schedule", "appointment", "book", "contact", "quote", "estimate", "call us", "get started", "request", "reserve"]
    strong_hits = sum(1 for c in cta_strong if c in t)
    weak_hits   = sum(1 for c in cta_weak if c in t)

    if strong_hits >= 3:   cv, ct, cf = 10, f"Multiple strong CTAs ({strong_hits})", "Good"
    elif strong_hits == 2: cv, ct, cf = 8,  "Good CTAs present", "Good"
    elif strong_hits == 1: cv, ct, cf = 5,  "One strong CTA", "Needs Work"
    elif weak_hits >= 3:   cv, ct, cf = 3,  "Only weak CTAs", "Needs Work"
    elif weak_hits >= 1:   cv, ct, cf = 1,  "Very weak CTAs", "Critical"
    else:                  cv, ct, cf = 0,  "No calls to action", "Critical"

    # UX quality deductions
    has_form    = bool(re.search(r'<form[\s>]|contact.form|wpcf7|wpforms|gravity.form|ninja.form|formidable', h))
    has_mailto  = 'href="mailto:' in h or "href='mailto:" in h
    has_tel     = 'href="tel:' in h or "href='tel:" in h
    has_booking = any(x in h for x in ["calendly", "acuity", "booksy", "oncehub", "hubspot.meetings"])

    cta_issues = []
    if has_mailto and not has_form and not has_booking:
        cv = max(0, cv - 4)
        cta_issues.append("mailto: only Ã¢â‚¬â€ opens email client Ã°Å¸Å¡Â©")
        cf = "Critical"
    if has_tel and not has_form and not has_booking and not has_mailto:
        cv = max(0, cv - 2)
        cta_issues.append("phone-only CTA Ã¢â‚¬â€ no online form Ã°Å¸Å¡Â©")
        if cf == "Good": cf = "Needs Work"
    if not has_form and not has_booking:
        cv = max(0, cv - 2)
        cta_issues.append("no contact form detected Ã°Å¸Å¡Â©")
        if cf == "Good": cf = "Needs Work"
    if has_booking:
        cv = min(10, cv + 2)
        ct = "Online booking tool detected Ã¢Å“â€œ"
    if cta_issues:
        ct = ct + " | " + " | ".join(cta_issues)

    dims["cta"] = {"score": cv, "max": 10, "label": "CTA", "icon": "Ã°Å¸Å½Â¯", "status": ct, "flag": cf,
                   "has_form": has_form, "has_mailto_only": has_mailto and not has_form,
                   "has_booking": has_booking}

    # 5. Trust Ã¢â‚¬â€ with modernity and social proof signals
    trust = 0
    trust_found = []
    trust_issues = []

    # Google reviews widget or count
    has_google_reviews = bool(re.search(r'google.*review|review.*google|\d+\s*google\s*review', h))
    if has_google_reviews:
        trust += 3; trust_found.append("Google reviews")
    elif any(x in t for x in ["testimonial", "what our clients say", "customer review", "reviews"]):
        trust += 2; trust_found.append("testimonials")
    else:
        trust_issues.append("no reviews visible Ã°Å¸Å¡Â©")

    # Star rating displayed
    if re.search(r'\d\.\d\s*(?:stars?|Ã¢Ëœâ€¦|out of 5|/5)', t):
        trust += 2; trust_found.append("star rating")

    # Team / owner profiles
    if any(x in t for x in ["meet the team", "meet our team", "our team", "meet the owner", "about us"]):
        trust += 1; trust_found.append("team profiles")

    # Credentials
    if any(x in t for x in ["certified", "accredited", "licensed", "insured", "bonded", "bbb", "award"]):
        trust += 2; trust_found.append("credentials")

    # Experience
    if re.search(r'(since|established|founded|serving)\s*(since\s*)?\d{4}|over\s+\d+\s+years', t):
        trust += 1; trust_found.append("experience")

    # Warranty / guarantee
    if any(x in t for x in ["warranty", "guarantee", "guaranteed"]):
        trust += 1; trust_found.append("warranty")

    # Project count / social proof numbers
    if re.search(r'\d{2,}\+?\s*(happy\s*)?(clients?|customers?|homes?|projects?|roofs?|jobs?)', t):
        trust += 1; trust_found.append("project count")

    # Review platforms
    if any(x in h for x in ["trustpilot", "birdeye", "podium", "grade.us", "reviews.io"]):
        trust += 1; trust_found.append("review platform")

    # Modernity penalty Ã¢â‚¬â€ old copyright
    copyright_match = re.search(r'Ã‚Â©\s*(\d{4})|copyright\s*Ã‚Â©?\s*(\d{4})', t)
    if copyright_match:
        year = int(copyright_match.group(1) or copyright_match.group(2))
        age = 2026 - year
        if age >= 4:
            trust = max(0, trust - 2)
            trust_issues.append(f"Ã‚Â© {year} Ã¢â‚¬â€ site looks dated Ã°Å¸Å¡Â©")

    # DIY builder penalty
    if any(x in h for x in ["wix.com", "squarespace.com", "weebly.com"]):
        trust = max(0, trust - 1)
        trust_issues.append("DIY website builder detected")

    trust = min(trust, 10)
    tf = "Good" if trust >= 7 else ("Needs Work" if trust >= 4 else "Critical")
    tt = f"Trust: {chr(44).join(trust_found)}" if trust_found else "No trust signals"
    if trust_issues:
        tt += " | Issues: " + ", ".join(trust_issues)
    dims["trust"] = {"score": trust, "max": 10, "label": "Trust", "icon": "Ã°Å¸â€ºÂ¡Ã¯Â¸Â", "status": tt, "flag": tf,
                     "has_google_reviews": has_google_reviews, "trust_issues": trust_issues}

    # 6. Booking
    booking_strong = ["book online", "book appointment", "schedule online", "online scheduling", "request appointment online", "online booking", "instant quote"]
    booking_platforms = ["calendly", "acuity", "zocdoc", "booksy", "vagaro", "mindbody"]
    booking_basic = ["contact form", "send us a message", "fill out", "<form"]
    strong_b = sum(1 for b in booking_strong if b in t)
    platform_b = sum(1 for b in booking_platforms if b in h)
    basic_b = sum(1 for b in booking_basic if b in t or b in h)
    if strong_b >= 2 or platform_b >= 1:  bv, bst, bf = 10, "Strong online booking", "Good"
    elif strong_b == 1:                   bv, bst, bf = 7,  "Basic online booking", "Needs Work"
    elif basic_b >= 1:                    bv, bst, bf = 3,  "Contact form only", "Needs Work"
    else:                                 bv, bst, bf = 0,  "No booking Ã¢â‚¬â€ phone only", "Critical"
    dims["booking"] = {"score": bv, "max": 10, "label": "Booking", "icon": "Ã°Å¸â€œâ€¦", "status": bst, "flag": bf}

    # 7. Social Proof
    sp = 0
    sp_found = []
    if re.search(r'\d[\d,]*\s*(google\s*)?reviews?|google\s*rating|\d+\s*\+?\s*5[\-\s]?star', t):
        sp += 4; sp_found.append("Google reviews")
    if any(x in h for x in ["trustpilot", "yelp", "birdeye", "podium", "grade.us"]):
        sp += 3; sp_found.append("review platform")
    if any(x in h for x in ["facebook.com", "instagram.com"]):
        sp += 2; sp_found.append("social media")
    if re.search(r'\d{2,}\s*(happy\s*)?(clients?|customers?|homes?|projects?)', t):
        sp += 1; sp_found.append("customer count")
    sp = min(sp, 10)
    spf = "Good" if sp >= 7 else ("Needs Work" if sp >= 3 else "Critical")
    spt = f"Social: {', '.join(sp_found)}" if sp_found else "No social proof"
    dims["social"] = {"score": sp, "max": 10, "label": "Social Proof", "icon": "Ã¢Â­Â", "status": spt, "flag": spf}

    # 8. SEO
    seo = 0
    seo_found = []
    if re.search(r'<title[^>]*>[^<]{10,}</title>', html, re.IGNORECASE):
        seo += 3; seo_found.append("title tag")
    if re.search(r'<meta[^>]*name=["\']description["\'][^>]*>', html, re.IGNORECASE):
        seo += 3; seo_found.append("meta description")
    if re.search(r'<h1[^>]*>[^<]{5,}</h1>', html, re.IGNORECASE):
        seo += 2; seo_found.append("H1 tag")
    if re.search(r'(serving|near|located in|local|county|florida|texas|miami|houston|dallas)', t):
        seo += 2; seo_found.append("local keywords")
    seo = min(seo, 10)
    sf2 = "Good" if seo >= 7 else ("Needs Work" if seo >= 4 else "Critical")
    st2 = f"SEO: {', '.join(seo_found)}" if seo_found else "Missing SEO elements"
    dims["seo"] = {"score": seo, "max": 10, "label": "SEO", "icon": "Ã°Å¸â€Â", "status": st2, "flag": sf2}

    # 9. Visual Layout
    visual = 0
    visual_found = []
    visual_issues = []
    img_count = len(re.findall(r'<img\s', html, re.IGNORECASE))
    if img_count >= 5:    visual += 2; visual_found.append(f"{img_count} images")
    elif img_count >= 2:  visual += 1; visual_issues.append(f"only {img_count} images")
    else:                 visual_issues.append("no images")
    h2_count = len(re.findall(r'<h2[\s>]', html, re.IGNORECASE))
    if h2_count >= 3:     visual += 2; visual_found.append("good headings")
    elif h2_count >= 1:   visual += 1; visual_issues.append("minimal headings")
    else:                 visual_issues.append("no structure")
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
    if paragraphs:
        avg_len = sum(len(re.sub(r'<[^>]+>', '', p)) for p in paragraphs) / len(paragraphs)
        if avg_len < 200:   visual += 2; visual_found.append("structured content")
        elif avg_len < 500: visual += 1; visual_issues.append("long text blocks")
        else:               visual_issues.append("wall of text Ã°Å¸Å¡Â©")
    section_count = len(re.findall(r'<section[\s>]', html, re.IGNORECASE))
    div_classes = len(re.findall(r'class=["\'][^"\']*(?:section|hero|banner|card|feature|block|row|container)[^"\']*["\']', html, re.IGNORECASE))
    if section_count >= 3 or div_classes >= 4:
        visual += 2; visual_found.append("structured sections")
    elif section_count >= 1 or div_classes >= 2:
        visual += 1
    list_count = html.lower().count('<ul') + html.lower().count('<ol')
    if list_count >= 2:   visual += 2; visual_found.append("structured lists")
    elif list_count == 1: visual += 1
    visual = min(visual, 10)
    vf = "Good" if visual >= 7 else ("Needs Work" if visual >= 4 else "Critical")
    vt = f"Issues: {', '.join(visual_issues)}" if visual_issues else f"Layout: {', '.join(visual_found)}"
    dims["visual"] = {"score": visual, "max": 10, "label": "Visual Layout", "icon": "Ã°Å¸Å½Â¨", "status": vt, "flag": vf}

    # 10. Tech & Conversion
    tech = 0
    tech_found = []
    staleness = []
    if re.search(r'gtag|google-analytics|googletagmanager|_ga\b', h):
        tech += 3; tech_found.append("Analytics")
    if re.search(r'fbq|facebook.*pixel|fb\.init', h):
        tech += 2; tech_found.append("Meta pixel")
    if re.search(r'intercom|drift|tidio|livechat|tawk\.to|crisp\.chat|zendesk', h):
        tech += 3; tech_found.append("live chat")
    if re.search(r'popup|exit.intent|optinmonster|sumo|hello.bar', h):
        tech += 2; tech_found.append("lead capture")
    copyright_match = re.search(r'Ã‚Â©\s*(\d{4})|copyright\s*Ã‚Â©?\s*(\d{4})', t)
    if copyright_match:
        year = int(copyright_match.group(1) or copyright_match.group(2))
        age = 2026 - year
        if age >= 4:   staleness.append(f"Ã‚Â© {year} Ã¢â‚¬â€ {age} years old Ã°Å¸Å¡Â©")
        elif age >= 2: staleness.append(f"Ã‚Â© {year} Ã¢â‚¬â€ may be dated")
    if any(x in h for x in ["wix.com", "squarespace.com", "weebly.com", "godaddy website builder", "jimdo"]):
        staleness.append("DIY builder Ã°Å¸Å¡Â©")
    tech = min(tech, 10)
    tf3 = "Good" if tech >= 7 else ("Needs Work" if tech >= 3 else "Critical")
    tt3 = f"Tools: {', '.join(tech_found)}" if tech_found else "No tracking tools"
    if staleness:
        tt3 += " | " + " | ".join(staleness)
    dims["tech"] = {"score": tech, "max": 10, "label": "Tech & Conversion", "icon": "Ã°Å¸â€œÅ ", "status": tt3, "flag": tf3}

    total = sum(v["score"] for v in dims.values())
    # Critical speed caps the headline: a catastrophically slow site cannot score "good"
    if dims.get("speed", {}).get("score", 10) <= 2:
        lcp = dims.get("speed", {}).get("psi_mobile_lcp")
        if lcp is not None:
            if lcp >= 20:
                total = min(total, 35)
            elif lcp >= 10:
                total = min(total, 45)
            elif lcp >= 4:
                total = min(total, 55)
        else:
            total = min(total, 55)
    if total <= 30:   grade, grade_color = "F Ã¢â‚¬â€ Urgent", "#dc2626"
    elif total <= 45: grade, grade_color = "D Ã¢â‚¬â€ Poor", "#ef4444"
    elif total <= 60: grade, grade_color = "C Ã¢â‚¬â€ Average", "#f97316"
    elif total <= 75: grade, grade_color = "B Ã¢â‚¬â€ Decent", "#f59e0b"
    elif total <= 88: grade, grade_color = "A Ã¢â‚¬â€ Good", "#84cc16"
    else:             grade, grade_color = "A+ Ã¢â‚¬â€ Strong", "#10b981"

    return {
        "website_score": total,
        "opportunity_score": 100 - total,
        "grade": grade,
        "grade_color": grade_color,
        "dimensions": dims,
        "critical_count": len([k for k, v in dims.items() if v["flag"] == "Critical"]),
        "needs_work_count": len([k for k, v in dims.items() if v["flag"] == "Needs Work"]),
        "load_time": load_time,
        "is_ssl": is_ssl,
        "staleness_flags": staleness,
        "signal_count": 62,
        "mobile_issues": dims["mobile"].get("mobile_issues", []),
    }


async def scan_image_weight(html: str, base_url: str) -> dict:
    """Measure total image weight on the page by fetching image sizes.
    Returns total_image_kb and heavy_images (largest few). Capped and time-limited
    so it does not slow the batch or fail on slow sites.
    """
    result = {"total_image_kb": None, "heavy_images": None}
    try:
        from urllib.parse import urljoin
        if not base_url.startswith("http"):
            base_url = "https://" + base_url

        # Collect <img src> URLs (also srcset first candidate) and CSS url() backgrounds
        srcs = []
        for m in re.findall(r'<img[^>]+>', html, re.IGNORECASE):
            src = re.search(r'src=["\']([^"\']+)["\']', m, re.IGNORECASE)
            if src:
                srcs.append(src.group(1))
        for bg in re.findall(r'url\(["\']?([^)"\']+\.(?:jpg|jpeg|png|webp|gif))["\']?\)', html, re.IGNORECASE):
            srcs.append(bg)

        # Normalise, dedupe, skip data URIs and tiny tracking pixels
        seen = set()
        urls = []
        for s in srcs:
            if s.startswith("data:"):
                continue
            full = urljoin(base_url, s)
            if full in seen:
                continue
            seen.add(full)
            urls.append(full)
        urls = urls[:15]  # cap so we do not hammer slow sites
        if not urls:
            return result

        headers = {**HEADERS}

        async def get_size(client, u):
            try:
                # Try HEAD first (cheap), fall back to a ranged GET if no length
                r = await client.head(u, follow_redirects=True)
                cl = r.headers.get("content-length")
                if cl and cl.isdigit():
                    return (u, int(cl))
                r = await client.get(u, follow_redirects=True, headers={"Range": "bytes=0-0"})
                cl = r.headers.get("content-range") or r.headers.get("content-length")
                if cl:
                    digits = re.search(r'/(\d+)$', cl) or re.search(r'(\d+)', cl)
                    if digits:
                        return (u, int(digits.group(1)))
            except Exception:
                return None
            return None

        import asyncio as _asyncio
        async with httpx.AsyncClient(timeout=6, headers=headers, verify=False) as client:
            sizes = await _asyncio.gather(*[get_size(client, u) for u in urls])

        sizes = [s for s in sizes if s and s[1] > 0]
        if not sizes:
            return result

        total_bytes = sum(b for _, b in sizes)
        sizes.sort(key=lambda x: x[1], reverse=True)
        heavy = [{"url": u, "kb": round(b / 1024, 1)} for u, b in sizes[:3]]

        result["total_image_kb"] = round(total_bytes / 1024, 1)
        result["heavy_images"] = heavy
    except Exception as scan_err:
        pass
    return result


async def audit_url(url: str) -> dict:
    if not url:
        return {
            "error": "No website", "website_score": 0, "opportunity_score": 100,
            "grade": "F Ã¢â‚¬â€ Urgent", "grade_color": "#dc2626",
            "dimensions": {}, "load_time": 0, "is_ssl": False,
            "critical_count": 10, "needs_work_count": 0,
            "staleness_flags": [], "signal_count": 62,
        }
    try:
        html, text, load_time, is_ssl = await fetch_page(url)
        if not html:
            return {
                "error": "Could not load", "website_score": 5, "opportunity_score": 95,
                "grade": "F Ã¢â‚¬â€ Urgent", "grade_color": "#dc2626",
                "dimensions": {}, "load_time": load_time, "is_ssl": is_ssl,
                "critical_count": 8, "needs_work_count": 0,
                "staleness_flags": [], "signal_count": 62,
            }
        result = score_site(html, text, load_time, is_ssl)
        # Add image-weight data (best-effort; never blocks scoring)
        try:
            img_data = await scan_image_weight(html, url)
            result["total_image_kb"] = img_data.get("total_image_kb")
            result["heavy_images"] = img_data.get("heavy_images")
        except Exception as call_err:
            result["total_image_kb"] = None
            result["heavy_images"] = None
        return result
    except Exception as e:
        return {
            "error": str(e)[:80], "website_score": 10, "opportunity_score": 90,
            "grade": "F Ã¢â‚¬â€ Urgent", "grade_color": "#dc2626",
            "dimensions": {}, "load_time": 0, "is_ssl": False,
            "critical_count": 6, "needs_work_count": 0,
            "staleness_flags": [], "signal_count": 62,
        }


async def detect_intelligence_signals(html: str, website_score: int) -> dict:
    if not html:
        return {"intel_pills": [], "intel_signals": {}, "revenue_leak": False, "revenue_leak_reason": ""}

    h = html.lower()
    signals = {}
    pills = []
    revenue_leak = False
    revenue_leak_reasons = []
    running_ads = []

    if any(x in h for x in ["googleadservices.com", "google_conversion", "gtag('event'", "aw-", "adwords"]):
        running_ads.append("Google Ads"); signals["google_ads"] = True
        pills.append({"label": "Google Ads", "color": "blue", "icon": "Ã°Å¸â€œÂ¢"})
    if any(x in h for x in ["connect.facebook.net/en_us/fbevents", "fbq('init'", "facebook pixel", "facebook-domain-verification"]):
        running_ads.append("Meta Ads"); signals["meta_ads"] = True
        pills.append({"label": "Meta Ads", "color": "blue", "icon": "Ã°Å¸â€œÂ¢"})
    if "bat.bing.com" in h or "uetq" in h:
        running_ads.append("Bing Ads"); signals["bing_ads"] = True
        pills.append({"label": "Bing Ads", "color": "blue", "icon": "Ã°Å¸â€œÂ¢"})
    if "tiktok" in h and ("ttq" in h or "analytics.tiktok" in h):
        running_ads.append("TikTok Ads"); signals["tiktok_ads"] = True
        pills.append({"label": "TikTok Ads", "color": "blue", "icon": "Ã°Å¸â€œÂ¢"})

    if running_ads and website_score < 70:
        revenue_leak = True
        revenue_leak_reasons.append(f"Running {' + '.join(running_ads)} with weak website")
        pills.append({"label": "Ã°Å¸â€™Â¸ Revenue Leak", "color": "red", "icon": "Ã°Å¸â€™Â¸"})

    if any(x in h for x in ["gtag('config', 'g-", "ga4", "googletagmanager.com/gtm"]):
        signals["ga4"] = True; pills.append({"label": "GA4", "color": "green", "icon": "Ã°Å¸â€œÅ "})
    elif any(x in h for x in ["google-analytics.com/analytics.js", "ua-"]):
        signals["ga_ua"] = True; pills.append({"label": "GA (old)", "color": "amber", "icon": "Ã°Å¸â€œÅ "})
    if "googletagmanager.com" in h:
        signals["gtm"] = True; pills.append({"label": "Tag Manager", "color": "green", "icon": "Ã°Å¸ÂÂ·"})
    if any(x in h for x in ["hotjar.com", "_hjSettings"]):
        signals["hotjar"] = True; pills.append({"label": "Hotjar", "color": "green", "icon": "Ã°Å¸â€Â¥"})
    if any(x in h for x in ["hubspot.com", "hs-scripts.com", "_hsq"]):
        signals["hubspot"] = True; pills.append({"label": "HubSpot", "color": "orange", "icon": "Ã°Å¸â€Â§"})
    if "salesforce" in h and ("force.com" in h or "pardot" in h):
        signals["salesforce"] = True; pills.append({"label": "Salesforce", "color": "blue", "icon": "Ã¢ËœÂÃ¯Â¸Â"})
    if any(x in h for x in ["intercom.io", "intercomcdn"]):
        signals["intercom"] = True; pills.append({"label": "Intercom", "color": "purple", "icon": "Ã°Å¸â€™Â¬"})
    if any(x in h for x in ["drift.com", "driftt.com"]):
        signals["drift"] = True; pills.append({"label": "Drift", "color": "purple", "icon": "Ã°Å¸â€™Â¬"})
    if any(x in h for x in ["mailchimp.com", "chimpified"]):
        signals["mailchimp"] = True; pills.append({"label": "Mailchimp", "color": "amber", "icon": "Ã°Å¸â€œÂ§"})
    if any(x in h for x in ["calendly.com", "acuityscheduling"]):
        signals["booking_tool"] = True; pills.append({"label": "Online Booking", "color": "green", "icon": "Ã°Å¸â€œâ€¦"})
    if any(x in h for x in ["callrail.com", "calltracking"]):
        signals["call_tracking"] = True; pills.append({"label": "Call Tracking", "color": "green", "icon": "Ã°Å¸â€œÅ¾"})
    if any(x in h for x in ["trustpilot.com/widget", "reviews.io", "birdeye"]):
        signals["review_widget"] = True; pills.append({"label": "Review Widget", "color": "green", "icon": "Ã¢Â­Â"})

    # CMS detection
    if "wp-content" in h or "wp-includes" in h:
        signals["cms_wordpress"] = True; pills.append({"label": "WordPress", "color": "blue", "icon": "Ã°Å¸Å’Â"})
    if "revslider" in h or "slider-revolution" in h:
        signals["slider_revolution"] = True; pills.append({"label": "Slider Revolution Ã¢Å¡Â ", "color": "red", "icon": "Ã°Å¸â€œÂµ"})
    if "elementor" in h:
        signals["cms_elementor"] = True; pills.append({"label": "Elementor", "color": "blue", "icon": "Ã°Å¸Å½Â¨"})
    if "wix.com" in h:
        signals["cms_wix"] = True; pills.append({"label": "Wix", "color": "amber", "icon": "Ã¢Å¡Â "})
    if "squarespace.com" in h:
        signals["cms_squarespace"] = True; pills.append({"label": "Squarespace", "color": "amber", "icon": "Ã¢Å¡Â "})
    if "vc_row" in h or "wpbakery" in h:
        signals["cms_wpbakery"] = True; pills.append({"label": "WPBakery Ã¢Å¡Â ", "color": "red", "icon": "Ã°Å¸â€œÂµ"})

    has_any_tracking = signals.get("ga4") or signals.get("ga_ua") or signals.get("gtm") or running_ads
    if not has_any_tracking:
        signals["no_tracking"] = True
        pills.append({"label": "No Analytics", "color": "red", "icon": "Ã°Å¸â€Â´"})
        revenue_leak_reasons.append("No tracking Ã¢â‚¬â€ flying blind")

    return {
        "intel_pills": pills,
        "intel_signals": signals,
        "running_ads": running_ads,
        "revenue_leak": revenue_leak,
        "revenue_leak_reason": " Ã‚Â· ".join(revenue_leak_reasons),
    }


async def detect_size_signals(website: str, company_name: str) -> dict:
    if not website:
        return {"size_tier": "unknown", "size_signals": [], "employee_estimate": ""}

    signals = []
    tier = "unknown"
    employee_estimate = ""

    try:
        import httpx, re as _re
        headers = {"User-Agent": MOBILE_UA}
        async with httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as c:
            r = await c.get(website, headers=headers)
            text = r.text.lower()

        emp_match = _re.search(r'(\d+)\+?\s*(?:employees|staff|team members|people)', text)
        if emp_match:
            count = int(emp_match.group(1))
            employee_estimate = f"{count}+ employees"
            if count >= 200:   tier = "enterprise"; signals.append(f"{count}+ employees")
            elif count >= 50:  tier = "mid";        signals.append(f"{count}+ employees")
            elif count >= 10:  tier = "smb";        signals.append(f"{count}+ employees")
            else:              tier = "micro";       signals.append(f"{count} employees")

        if any(x in text for x in ["/careers", "/jobs", "/join-us", "we are hiring", "open positions"]):
            signals.append("Active hiring")
            if tier == "unknown": tier = "smb"

        loc_count = len(_re.findall(r'(?:office|location|branch)\s+in\s+[a-z]', text))
        if loc_count >= 3:
            signals.append(f"{loc_count}+ locations")
            if tier in ("unknown", "smb"): tier = "mid"
        elif loc_count >= 1:
            signals.append("Multiple locations")

        enterprise_tech = []
        if "salesforce" in text: enterprise_tech.append("Salesforce")
        if "hubspot" in text: enterprise_tech.append("HubSpot")
        if "marketo" in text: enterprise_tech.append("Marketo")
        if enterprise_tech:
            signals.append(f"Uses {', '.join(enterprise_tech[:2])}")
            if tier in ("unknown", "smb") and "salesforce" in text: tier = "mid"

        if any(x in text for x in ["marketing manager", "marketing director", "head of marketing", "cmo"]):
            signals.append("Has marketing team")
            if tier == "unknown": tier = "smb"

        if any(x in text for x in ["bbb accredited", "inc 500", "award winning"]):
            signals.append("Industry recognition")

        if any(x in text for x in ["family owned", "family-owned", "owner operated", "owner-operated"]):
            signals.append("Owner operated")
            if tier == "unknown": tier = "micro"

    except Exception:
        pass

    if tier == "unknown":
        tier = "smb"

    return {
        "size_tier": tier,
        "size_signals": signals,
        "employee_estimate": employee_estimate,
    }


def calculate_icp_score(audit: dict, biz: dict) -> dict:
    """
    Score against ideal customer profile.
    Website + mobile are primary signals Ã¢â‚¬â€ ICP is a modifier not a gate.
    Any business with a bad website and/or bad mobile is an opportunity.
    """
    score = 0
    breakdown = {}

    rv = biz.get("reviews", 0)
    r  = biz.get("rating",  0)
    ws = audit.get("website_score", 0)
    dims = audit.get("dimensions", {}) or {}
    size_tier = audit.get("size_tier", "unknown")
    size_signals = audit.get("size_signals", []) or []
    running_ads = audit.get("running_ads", []) or []
    intel_signals = audit.get("intel_signals", {}) or {}
    mobile_issues = audit.get("mobile_issues", []) or dims.get("mobile", {}).get("mobile_issues", []) or []
    mobile_score  = dims.get("mobile", {}).get("score", 5)

    # 1. Business quality (25 pts)
    if rv >= 200:    review_pts = 25
    elif rv >= 100:  review_pts = 22
    elif rv >= 50:   review_pts = 18
    elif rv >= 20:   review_pts = 14
    elif rv >= 10:   review_pts = 10
    elif rv > 0:     review_pts = 6
    else:            review_pts = 10  # Unknown Ã¢â‚¬â€ assume established

    if r >= 4.5:     review_pts = min(25, review_pts + 4)
    elif r >= 4.0:   review_pts = min(25, review_pts + 2)
    elif r < 3.5 and r > 0: review_pts = max(0, review_pts - 4)

    score += review_pts
    breakdown["business_quality"] = review_pts

    # 2. Mobile opportunity (25 pts) Ã¢â‚¬â€ major factor
    if mobile_score <= 2:    mobile_pts = 25
    elif mobile_score <= 4:  mobile_pts = 22
    elif mobile_score <= 6:  mobile_pts = 16
    elif mobile_score <= 8:  mobile_pts = 8
    else:                    mobile_pts = 3

    if any("slider revolution" in i.lower() for i in mobile_issues):
        mobile_pts = min(25, mobile_pts + 3)
    if any("overflow" in i.lower() for i in mobile_issues):
        mobile_pts = min(25, mobile_pts + 2)

    score += mobile_pts
    breakdown["mobile_opportunity"] = mobile_pts

    # 3. Digital gap (20 pts)
    has_crm      = intel_signals.get("hubspot") or intel_signals.get("salesforce")
    has_ads      = bool(running_ads)
    has_tracking = intel_signals.get("ga4") or intel_signals.get("gtm")
    has_booking  = intel_signals.get("booking_tool")
    has_chat     = intel_signals.get("intercom") or intel_signals.get("drift")

    gap_pts = 20
    if has_crm:              gap_pts -= 8
    if has_chat:             gap_pts -= 4
    if has_booking:          gap_pts -= 3
    if has_ads and ws >= 65: gap_pts -= 4
    if not has_tracking:     gap_pts = min(20, gap_pts + 3)
    gap_pts = max(0, gap_pts)
    score += gap_pts
    breakdown["digital_gap"] = gap_pts

    # 4. Size fit (15 pts) Ã¢â‚¬â€ size no longer penalises heavily
    if size_tier == "smb":          size_pts = 15
    elif size_tier == "micro":      size_pts = 10
    elif size_tier == "mid":        size_pts = 13  # Mid-market still good
    elif size_tier == "enterprise": size_pts = 8   # Even enterprise can be worth it
    else:
        if rv >= 50:   size_pts = 13
        elif rv >= 20: size_pts = 11
        elif rv >= 5:  size_pts = 9
        else:          size_pts = 10

    has_marketing_team = any("marketing" in s.lower() for s in size_signals)
    if has_marketing_team:
        size_pts = max(0, size_pts - 4)  # Reduced penalty Ã¢â‚¬â€ still worth trying
    score += size_pts
    breakdown["size_fit"] = size_pts

    # 5. Owner operated bonus (10 pts)
    owner_signals = [s for s in size_signals if any(x in s.lower() for x in ["owner", "family", "operated"])]
    if owner_signals:
        owner_pts = 10
    elif size_tier in ("micro", "smb") and not has_marketing_team:
        owner_pts = 7
    else:
        owner_pts = 5  # Still gets some points
    score += owner_pts
    breakdown["owner_operated"] = owner_pts

    score = min(100, score)

    # Revenue leak bonus
    revenue_leak = audit.get("revenue_leak", False)
    if revenue_leak:
        score = min(100, score + 8)
        breakdown["revenue_leak_bonus"] = 8

    # ICP tier
    if score >= 80:    icp_tier, icp_label = "A", "Ã°Å¸Å½Â¯ Perfect ICP"
    elif score >= 65:  icp_tier, icp_label = "B", "Ã¢Å“â€¦ Good ICP"
    elif score >= 45:  icp_tier, icp_label = "C", "Ã¢Å¡Â¡ Possible"
    else:              icp_tier, icp_label = "D", "Ã¢Å“â€” Poor fit"

    # Ã¢â€â‚¬Ã¢â€â‚¬ Combined opportunity score Ã¢â€â‚¬Ã¢â€â‚¬
    # Website + mobile are primary. ICP is a modifier.
    # Bad website = high opportunity regardless of ICP
    website_bonus = 0
    if ws < 80:             website_bonus += 10
    if ws < 60:             website_bonus += 15
    if ws < 40:             website_bonus += 20
    if mobile_score <= 4:   website_bonus += 20  # Broken mobile = losing 60%+ leads
    elif mobile_score <= 6: website_bonus += 12
    elif mobile_score <= 8: website_bonus += 6

    # Website weakness drives 50%, ICP score drives 50%
    website_opportunity = min(50, website_bonus)
    icp_contribution = round(score * 0.5)
    combined = min(99, website_opportunity + icp_contribution)

    # Pills
    pills = []
    if rv >= 100:           pills.append(f"Ã¢Â­Â {rv}+ reviews")
    elif rv >= 20:          pills.append(f"Ã¢Â­Â {rv} reviews")
    if mobile_score <= 4:   pills.append(f"Ã°Å¸â€œÂµ Broken mobile ({mobile_score}/10)")
    elif mobile_score <= 6: pills.append(f"Ã°Å¸â€œÂ± Poor mobile ({mobile_score}/10)")
    if running_ads:         pills.append(f"Ã°Å¸â€œÂ¢ Running {' + '.join(running_ads)}")
    if revenue_leak:        pills.append("Ã°Å¸â€™Â¸ Revenue leak")
    if not has_crm and not has_tracking: pills.append("Ã°Å¸â€Â´ No marketing tools")
    if not has_crm:         pills.append("No CRM")
    if owner_signals:       pills.append("Ã°Å¸â€˜Â¤ Owner operated")
    if has_marketing_team:  pills.append("Has marketing team")
    if ws <= 45:            pills.append("Ã¢Å¡Â  Weak website")
    elif ws >= 75:          pills.append("Ã¢Å“â€œ Decent website")
    if size_tier == "smb":  pills.append("SMB size Ã¢Å“â€œ")
    elif size_tier == "mid": pills.append("Mid-market")

    return {
        "icp_score":      score,
        "icp_tier":       icp_tier,
        "icp_label":      icp_label,
        "icp_breakdown":  breakdown,
        "icp_pills":      pills,
        "combined_score": combined,
    }


async def run_roaster(industry: str, location: str, limit: int, api_key: str, log_cb=None) -> list[dict]:
    async def log(msg):
        if log_cb:
            await log_cb(msg)

    await log(f"Searching: {industry} in {location}")

    try:
        businesses = await find_businesses(industry, location, limit, api_key)
    except Exception as e:
        await log(f"Search error: {e}")
        return []

    if not businesses:
        await log("No businesses found")
        return []

    await log(f"Found {len(businesses)} businesses Ã¢â‚¬â€ running audit...")
    results = []

    for i, biz in enumerate(businesses, 1):
        await log(f"  [{i}/{len(businesses)}] {biz['name']}")
        audit = await audit_url(biz.get("website", ""))

        size_signals = await detect_size_signals(biz.get("website", ""), biz.get("name", ""))
        audit.update(size_signals)

        try:
            import httpx as _httpx
            _h = {"User-Agent": MOBILE_UA}
            async with _httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as _c:
                _r = await _c.get(biz.get("website", ""), headers=_h)
                _html = _r.text
        except Exception:
            _html = ""
        intel = await detect_intelligence_signals(_html, audit.get("website_score", 0))
        audit.update(intel)
        audit["mobile_issues"] = audit.get("dimensions", {}).get("mobile", {}).get("mobile_issues", [])

        bq = 0
        r, rv = biz.get("rating", 0), biz.get("reviews", 0)
        if rv >= 200: bq += 25
        elif rv >= 100: bq += 20
        elif rv >= 50: bq += 12
        elif rv >= 20: bq += 6
        if r >= 4.7: bq += 20
        elif r >= 4.3: bq += 12
        elif r >= 4.0: bq += 6

        icp = calculate_icp_score(audit, biz)

        results.append({
            **biz, **audit,
            "biz_quality": bq,
            "priority_score": icp["combined_score"],
            **icp,
        })
        await asyncio.sleep(0.3)

    results.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    await log(f"Done Ã¢â‚¬â€ {len(results)} businesses scored")
    return results
