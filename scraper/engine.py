"""
Roaster Bot — Core Engine v3
10 dimensions x 10 points = 100 total score.
Low score = high opportunity for Eversite.
~58 individual signal checks.
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

# ── Google Maps Search ────────────────────────────────────────────────────────

async def find_businesses(industry: str, location: str, limit: int, api_key: str) -> list[dict]:
    params = {
        "api_key": api_key,
        "engine": "google_maps",
        "q": f"{industry} {location}",
        "ll": "@25.7617,-80.1918,14z",
        "type": "search",
        "hl": "en",
        "gl": "us",
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(SERPAPI_URL, params=params)
        r.raise_for_status()
        data = r.json()

    out = []
    for p in data.get("local_results", [])[:limit]:
        out.append({
            "name": p.get("title", ""),
            "website": p.get("website", ""),
            "rating": p.get("rating") or 0,
            "reviews": p.get("reviews") or 0,
            "address": p.get("address", ""),
            "phone": p.get("phone", ""),
            "category": p.get("type", industry),
            "google_url": p.get("link", ""),
            "thumbnail": p.get("thumbnail", ""),
        })
    return out


# ── Page Fetch ────────────────────────────────────────────────────────────────

async def fetch_page(url: str) -> tuple[str, str, float, bool]:
    if not url.startswith("http"):
        url = "https://" + url
    start = time.time()
    html, text, is_ssl = "", "", url.startswith("https://")

    for attempt_url in [url, url.replace("https://", "http://") if url.startswith("https://") else None]:
        if not attempt_url:
            continue
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=HEADERS) as c:
                resp = await c.get(attempt_url)
                html = resp.text
                is_ssl = str(resp.url).startswith("https://")
                # Strip scripts/styles then extract visible text
                clean = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
                clean = re.sub(r'<style[^>]*>.*?</style>', ' ', clean, flags=re.DOTALL | re.IGNORECASE)
                clean = re.sub(r'<[^>]+>', ' ', clean)
                clean = re.sub(r'&[a-z]+;', ' ', clean)
                text = re.sub(r'\s+', ' ', clean).lower().strip()
                break
        except Exception:
            continue

    return html, text, round(time.time() - start, 2), is_ssl


# ── 10x10 Scoring Engine ──────────────────────────────────────────────────────

def score_site(html: str, text: str, load_time: float, is_ssl: bool) -> dict:
    """
    10 dimensions × 10 points = 100 total.
    Lower score = worse website = higher opportunity for Eversite.
    ~58 signal checks total.
    """
    h = html.lower()
    t = text
    dims = {}

    # ── 1. Speed (10pts) ── [3 signal checks]
    if load_time < 2:     sv, st, sf = 10, f"Fast ({load_time}s)", "Good"
    elif load_time < 3:   sv, st, sf = 7,  f"Moderate ({load_time}s)", "Needs Work"
    elif load_time < 4:   sv, st, sf = 4,  f"Slow ({load_time}s) — losing visitors", "Needs Work"
    else:                 sv, st, sf = 0,  f"Very slow ({load_time}s) — 40%+ bounce rate", "Critical"
    dims["speed"] = {"score": sv, "max": 10, "label": "Speed", "icon": "⚡", "status": st, "flag": sf}

    # ── 2. Mobile (10pts) ── [4 signal checks]
    has_viewport = 'name="viewport"' in h or "name='viewport'" in h
    has_flex = any(x in h for x in ["flex", "grid-template", "col-sm", "col-md"])
    has_framework = any(x in h for x in ["bootstrap", "tailwind", "foundation", "bulma"])
    has_media = "@media" in h
    mobile_signals = sum([has_viewport, has_flex or has_framework, has_media])
    if mobile_signals >= 3:   mv, mt, mf = 10, "Fully responsive", "Good"
    elif mobile_signals == 2: mv, mt, mf = 6,  "Mostly responsive", "Needs Work"
    elif mobile_signals == 1: mv, mt, mf = 3,  "Basic viewport only", "Needs Work"
    else:                     mv, mt, mf = 0,  "Not mobile friendly — 60%+ traffic is mobile", "Critical"
    dims["mobile"] = {"score": mv, "max": 10, "label": "Mobile", "icon": "📱", "status": mt, "flag": mf}

    # ── 3. SSL (10pts) ── [1 signal check]
    dims["ssl"] = {
        "score": 10 if is_ssl else 0, "max": 10,
        "label": "SSL", "icon": "🔒",
        "status": "Secure HTTPS" if is_ssl else "No SSL — browsers flag as unsafe",
        "flag": "Good" if is_ssl else "Critical"
    }

    # ── 4. CTA Effectiveness (10pts) ── [20 signal checks]
    cta_strong = [
        "schedule now", "book now", "book appointment", "book online",
        "schedule appointment", "schedule online", "request appointment",
        "get a free quote", "free quote", "free estimate", "get started today",
        "call now", "contact us today", "new patients welcome",
        "accept new patients", "same day", "next day service",
        "get your free", "claim your free", "start today",
    ]
    cta_weak = [
        "schedule", "appointment", "book", "contact", "quote",
        "estimate", "call us", "get started", "request", "reserve",
    ]
    strong_hits = sum(1 for c in cta_strong if c in t)  # 19 checks
    weak_hits = sum(1 for c in cta_weak if c in t)       # 10 checks

    if strong_hits >= 3:   cv, ct, cf = 10, f"Multiple strong CTAs ({strong_hits})", "Good"
    elif strong_hits == 2: cv, ct, cf = 8,  "Good CTAs present", "Good"
    elif strong_hits == 1: cv, ct, cf = 5,  "One strong CTA — needs more", "Needs Work"
    elif weak_hits >= 3:   cv, ct, cf = 3,  "Only weak CTAs — not action-driving", "Needs Work"
    elif weak_hits >= 1:   cv, ct, cf = 1,  "Very weak CTAs", "Critical"
    else:                  cv, ct, cf = 0,  "No calls to action at all", "Critical"
    dims["cta"] = {"score": cv, "max": 10, "label": "CTA", "icon": "🎯", "status": ct, "flag": cf}

    # ── 5. Trust Signals (10pts) ── [6 signal checks]
    trust = 0
    trust_found = []
    if any(x in t for x in ["testimonial", "what our patients say", "what our clients say", "customer review", "what people say"]):
        trust += 3; trust_found.append("testimonials")
    if any(x in t for x in ["meet the team", "meet our team", "about dr", "our doctor", "our dentist", "our team", "meet the owner"]):
        trust += 2; trust_found.append("team profiles")
    if any(x in t for x in ["certified", "accredited", "licensed", "insured", "bonded", "bbb accredited", "award"]):
        trust += 2; trust_found.append("credentials")
    if any(x in t for x in ["insurance accepted", "we accept", "delta dental", "cigna", "aetna", "metlife", "most insurance"]):
        trust += 2; trust_found.append("insurance")
    if re.search(r'(since|established|founded|serving)\s*(since\s*)?\d{4}|over\s+\d+\s+years', t):
        trust += 1; trust_found.append("experience")
    trust = min(trust, 10)
    tf = "Good" if trust >= 7 else ("Needs Work" if trust >= 4 else "Critical")
    tt = f"Trust signals: {', '.join(trust_found)}" if trust_found else "No trust signals — visitors can't verify credibility"
    dims["trust"] = {"score": trust, "max": 10, "label": "Trust", "icon": "🛡️", "status": tt, "flag": tf}

    # ── 6. Booking Flow (10pts) ── [8 signal checks]
    booking_strong = [
        "book online", "book appointment", "schedule online", "online scheduling",
        "request appointment online", "patient portal", "online booking",
    ]
    booking_platforms = ["calendly", "acuity", "zocdoc", "healthgrades", "booksy", "vagaro", "mindbody", "mychart"]
    booking_basic = ["contact form", "send us a message", "fill out", "<form"]

    strong_b = sum(1 for b in booking_strong if b in t)  # 7 checks
    platform_b = sum(1 for b in booking_platforms if b in h)  # 8 checks
    basic_b = sum(1 for b in booking_basic if b in t or b in h)  # 4 checks

    if strong_b >= 2 or platform_b >= 1:  bv, bst, bf = 10, f"Strong online booking", "Good"
    elif strong_b == 1:                   bv, bst, bf = 7,  "Basic online booking", "Needs Work"
    elif basic_b >= 1:                    bv, bst, bf = 3,  "Contact form only — no real booking", "Needs Work"
    else:                                 bv, bst, bf = 0,  "No booking flow — phone calls only", "Critical"
    dims["booking"] = {"score": bv, "max": 10, "label": "Booking", "icon": "📅", "status": bst, "flag": bf}

    # ── 7. Social Proof (10pts) ── [5 signal checks]
    sp = 0
    sp_found = []
    if re.search(r'\d[\d,]*\s*(google\s*)?reviews?|google\s*rating|\d+\s*\+?\s*5[\-\s]?star', t):
        sp += 4; sp_found.append("Google reviews")
    if any(x in h for x in ["trustpilot", "yelp", "healthgrades", "zocdoc", "birdeye", "podium", "grade.us"]):
        sp += 3; sp_found.append("review platform")
    if any(x in h for x in ["facebook.com", "instagram.com"]):
        sp += 2; sp_found.append("social media")
    if re.search(r'\d{2,}\s*(happy\s*)?(patients?|clients?|customers?|smiles?|homes?|projects?)', t):
        sp += 1; sp_found.append("customer count")
    sp = min(sp, 10)
    spf = "Good" if sp >= 7 else ("Needs Work" if sp >= 3 else "Critical")
    spt = f"Social proof: {', '.join(sp_found)}" if sp_found else "No social proof visible on site"
    dims["social"] = {"score": sp, "max": 10, "label": "Social Proof", "icon": "⭐", "status": spt, "flag": spf}

    # ── 8. SEO Basics (10pts) ── [5 signal checks]
    seo = 0
    seo_found = []
    if re.search(r'<title[^>]*>[^<]{10,}</title>', html, re.IGNORECASE):
        seo += 3; seo_found.append("title tag")
    if re.search(r'<meta[^>]*(name=["\']description["\'][^>]*content|content=[^>]*name=["\']description)["\'][^>]*>', html, re.IGNORECASE):
        seo += 3; seo_found.append("meta description")
    if re.search(r'<h1[^>]*>[^<]{5,}</h1>', html, re.IGNORECASE):
        seo += 2; seo_found.append("H1 tag")
    if re.search(r'(serving|near|located in|local|city|county|florida|texas|miami|houston|dallas)', t):
        seo += 2; seo_found.append("local keywords")
    seo = min(seo, 10)
    sf2 = "Good" if seo >= 7 else ("Needs Work" if seo >= 4 else "Critical")
    st2 = f"SEO: {', '.join(seo_found)}" if seo_found else "Missing key SEO elements"
    dims["seo"] = {"score": seo, "max": 10, "label": "SEO", "icon": "🔍", "status": st2, "flag": sf2}

    # ── 9. Visual Layout (10pts) ── [8 signal checks] NEW
    visual = 0
    visual_found = []
    visual_issues = []

    # Image count — a real page has photos
    img_count = len(re.findall(r'<img\s', html, re.IGNORECASE))
    if img_count >= 5:    visual += 2; visual_found.append(f"{img_count} images")
    elif img_count >= 2:  visual += 1; visual_issues.append(f"only {img_count} images")
    else:                 visual_issues.append("no images — text only")

    # Heading structure — H2/H3 show content hierarchy
    h2_count = len(re.findall(r'<h2[\s>]', html, re.IGNORECASE))
    h3_count = len(re.findall(r'<h3[\s>]', html, re.IGNORECASE))
    if h2_count >= 3:     visual += 2; visual_found.append("good heading structure")
    elif h2_count >= 1:   visual += 1; visual_issues.append("minimal headings")
    else:                 visual_issues.append("no content structure")

    # Wall of text detection — average paragraph length
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
    if paragraphs:
        avg_len = sum(len(re.sub(r'<[^>]+>', '', p)) for p in paragraphs) / len(paragraphs)
        if avg_len < 200:   visual += 2; visual_found.append("well-structured content")
        elif avg_len < 500: visual += 1; visual_issues.append("some long text blocks")
        else:               visual_issues.append("wall of text detected 🚩")

    # Sections/layout structure
    section_count = len(re.findall(r'<section[\s>]', html, re.IGNORECASE))
    div_classes = len(re.findall(r'class=["\'][^"\']*(?:section|hero|banner|card|feature|block|row|container)[^"\']*["\']', html, re.IGNORECASE))
    if section_count >= 3 or div_classes >= 4:
        visual += 2; visual_found.append("structured sections")
    elif section_count >= 1 or div_classes >= 2:
        visual += 1; visual_issues.append("basic layout structure")
    else:
        visual_issues.append("no layout structure")

    # Lists — structured content
    list_count = html.lower().count('<ul') + html.lower().count('<ol')
    if list_count >= 2:   visual += 2; visual_found.append("structured lists")
    elif list_count == 1: visual += 1

    visual = min(visual, 10)
    vf = "Good" if visual >= 7 else ("Needs Work" if visual >= 4 else "Critical")
    if visual_issues:
        vt = f"Issues: {', '.join(visual_issues)}"
    else:
        vt = f"Layout: {', '.join(visual_found)}"
    dims["visual"] = {"score": visual, "max": 10, "label": "Visual Layout", "icon": "🎨", "status": vt, "flag": vf}

    # ── 10. Tech & Conversion (10pts) ── [7 signal checks]
    tech = 0
    tech_found = []
    staleness = []

    # Analytics
    if re.search(r'gtag|google-analytics|googletagmanager|_ga\b', h):
        tech += 3; tech_found.append("Analytics")
    # Meta pixel
    if re.search(r'fbq|facebook.*pixel|fb\.init', h):
        tech += 2; tech_found.append("Meta pixel")
    # Live chat
    if re.search(r'intercom|drift|tidio|livechat|tawk\.to|crisp\.chat|zendesk', h):
        tech += 3; tech_found.append("live chat")
    # Lead capture
    if re.search(r'popup|exit.intent|optinmonster|sumo|hello.bar', h):
        tech += 2; tech_found.append("lead capture")

    # Staleness — copyright year
    copyright_match = re.search(r'©\s*(\d{4})|copyright\s*©?\s*(\d{4})', t)
    if copyright_match:
        year = int(copyright_match.group(1) or copyright_match.group(2))
        age = 2026 - year
        if age >= 4: staleness.append(f"© {year} — {age} years old 🚩")
        elif age >= 2: staleness.append(f"© {year} — may be dated")

    # DIY builder
    if any(x in h for x in ["wix.com", "squarespace.com", "weebly.com", "godaddy website builder", "jimdo"]):
        staleness.append("DIY builder detected 🚩")

    tech = min(tech, 10)
    tf3 = "Good" if tech >= 7 else ("Needs Work" if tech >= 3 else "Critical")
    tt3 = f"Tools: {', '.join(tech_found)}" if tech_found else "No tracking or conversion tools"
    if staleness:
        tt3 += " | " + " | ".join(staleness)
    dims["tech"] = {"score": tech, "max": 10, "label": "Tech & Conversion", "icon": "📊", "status": tt3, "flag": tf3}

    # ── Totals ──
    total = sum(v["score"] for v in dims.values())  # out of 100

    if total <= 30:   grade, grade_color = "F — Urgent", "#dc2626"
    elif total <= 45: grade, grade_color = "D — Poor", "#ef4444"
    elif total <= 60: grade, grade_color = "C — Average", "#f97316"
    elif total <= 75: grade, grade_color = "B — Decent", "#f59e0b"
    elif total <= 88: grade, grade_color = "A — Good", "#84cc16"
    else:             grade, grade_color = "A+ — Strong", "#10b981"

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
        "signal_count": 58,
    }


async def audit_url(url: str) -> dict:
    if not url:
        return {
            "error": "No website", "website_score": 0, "opportunity_score": 100,
            "grade": "F — Urgent", "grade_color": "#dc2626",
            "dimensions": {}, "load_time": 0, "is_ssl": False,
            "critical_count": 10, "needs_work_count": 0,
            "staleness_flags": ["No website found 🚩"], "signal_count": 58,
        }
    try:
        html, text, load_time, is_ssl = await fetch_page(url)
        if not html:
            return {
                "error": "Could not load", "website_score": 5, "opportunity_score": 95,
                "grade": "F — Urgent", "grade_color": "#dc2626",
                "dimensions": {}, "load_time": load_time, "is_ssl": is_ssl,
                "critical_count": 8, "needs_work_count": 0,
                "staleness_flags": ["Site could not be loaded 🚩"], "signal_count": 58,
            }
        return score_site(html, text, load_time, is_ssl)
    except Exception as e:
        return {
            "error": str(e)[:80], "website_score": 10, "opportunity_score": 90,
            "grade": "F — Urgent", "grade_color": "#dc2626",
            "dimensions": {}, "load_time": 0, "is_ssl": False,
            "critical_count": 6, "needs_work_count": 0,
            "staleness_flags": [], "signal_count": 58,
        }




async def detect_intelligence_signals(html: str, website_score: int) -> dict:
    """
    Detect commercial intelligence signals from page HTML.
    Returns signals, pills, and revenue leak indicators.
    No extra API calls — works from already-fetched HTML.
    """
    if not html:
        return {"intel_pills": [], "intel_signals": {}, "revenue_leak": False, "revenue_leak_reason": ""}

    h = html.lower()
    signals = {}
    pills = []
    revenue_leak = False
    revenue_leak_reasons = []

    # ── Ad Spend Detection ──────────────────────────────────────────────
    running_ads = []

    if any(x in h for x in ["googleadservices.com", "google_conversion", "gtag('event'", "aw-", "adwords"]):
        running_ads.append("Google Ads")
        signals["google_ads"] = True
        pills.append({"label": "Google Ads", "color": "blue", "icon": "📢"})

    if any(x in h for x in ["connect.facebook.net/en_us/fbevents", "fbq('init'", "facebook pixel"]):
        running_ads.append("Meta Ads")
        signals["meta_ads"] = True
        pills.append({"label": "Meta Ads", "color": "blue", "icon": "📢"})

    if "bat.bing.com" in h or "uetq" in h:
        running_ads.append("Bing Ads")
        signals["bing_ads"] = True
        pills.append({"label": "Bing Ads", "color": "blue", "icon": "📢"})

    if "tiktok" in h and ("ttq" in h or "analytics.tiktok" in h):
        running_ads.append("TikTok Ads")
        signals["tiktok_ads"] = True
        pills.append({"label": "TikTok Ads", "color": "blue", "icon": "📢"})

    # Revenue leak — running ads + bad website
    if running_ads and website_score < 65:
        revenue_leak = True
        revenue_leak_reasons.append(f"Running {' + '.join(running_ads)} with weak website")
        pills.append({"label": "💸 Revenue Leak", "color": "red", "icon": "💸"})

    # ── Analytics & Tracking ────────────────────────────────────────────
    if any(x in h for x in ["gtag('config', 'g-", "ga4", "googletagmanager.com/gtm"]):
        signals["ga4"] = True
        pills.append({"label": "GA4", "color": "green", "icon": "📊"})
    elif any(x in h for x in ["google-analytics.com/analytics.js", "ua-"]):
        signals["ga_ua"] = True
        pills.append({"label": "GA (old)", "color": "amber", "icon": "📊"})

    if "googletagmanager.com" in h:
        signals["gtm"] = True
        pills.append({"label": "Tag Manager", "color": "green", "icon": "🏷"})

    if any(x in h for x in ["hotjar.com", "hj('create'", "_hjSettings"]):
        signals["hotjar"] = True
        pills.append({"label": "Hotjar", "color": "green", "icon": "🔥"})

    # ── CRM & Marketing Tools ────────────────────────────────────────────
    if any(x in h for x in ["hubspot.com", "hs-scripts.com", "_hsq"]):
        signals["hubspot"] = True
        pills.append({"label": "HubSpot", "color": "orange", "icon": "🔧"})

    if "salesforce" in h and ("force.com" in h or "pardot" in h):
        signals["salesforce"] = True
        pills.append({"label": "Salesforce", "color": "blue", "icon": "☁️"})

    if any(x in h for x in ["intercom.io", "intercomcdn", "window.intercom"]):
        signals["intercom"] = True
        pills.append({"label": "Intercom", "color": "purple", "icon": "💬"})

    if any(x in h for x in ["drift.com", "driftt.com"]):
        signals["drift"] = True
        pills.append({"label": "Drift", "color": "purple", "icon": "💬"})

    if any(x in h for x in ["mailchimp.com", "chimpified", "mc.js"]):
        signals["mailchimp"] = True
        pills.append({"label": "Mailchimp", "color": "amber", "icon": "📧"})

    # ── Booking & Conversion ─────────────────────────────────────────────
    if any(x in h for x in ["calendly.com", "cal.com/embed", "acuityscheduling"]):
        signals["booking_tool"] = True
        pills.append({"label": "Online Booking", "color": "green", "icon": "📅"})

    if any(x in h for x in ["callrail.com", "calltracking"]):
        signals["call_tracking"] = True
        pills.append({"label": "Call Tracking", "color": "green", "icon": "📞"})

    if any(x in h for x in ["trustpilot.com/widget", "reviews.io", "birdeye"]):
        signals["review_widget"] = True
        pills.append({"label": "Review Widget", "color": "green", "icon": "⭐"})

    # ── No tracking at all = dark = opportunity ──────────────────────────
    has_any_tracking = signals.get("ga4") or signals.get("ga_ua") or signals.get("gtm") or running_ads
    if not has_any_tracking:
        signals["no_tracking"] = True
        pills.append({"label": "No Analytics", "color": "red", "icon": "🔴"})
        revenue_leak_reasons.append("No tracking — flying blind")

    return {
        "intel_pills": pills,
        "intel_signals": signals,
        "running_ads": running_ads,
        "revenue_leak": revenue_leak,
        "revenue_leak_reason": " · ".join(revenue_leak_reasons),
    }

async def detect_size_signals(website: str, company_name: str) -> dict:
    """
    Detect company size signals from website content.
    Returns size_tier: 'smb' | 'mid' | 'enterprise' | 'unknown'
    and size_signals list for display.
    """
    if not website:
        return {"size_tier": "unknown", "size_signals": [], "employee_estimate": ""}

    signals = []
    tier = "unknown"
    employee_estimate = ""

    try:
        import httpx, re as _re
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as c:
            r = await c.get(website, headers=headers)
            text = r.text.lower()

        # Employee count mentioned explicitly
        emp_match = _re.search(r'(\d+)\+?\s*(?:employees|staff|team members|people)', text)
        if emp_match:
            count = int(emp_match.group(1))
            employee_estimate = f"{count}+ employees"
            if count >= 200:
                tier = "enterprise"
                signals.append(f"{count}+ employees")
            elif count >= 50:
                tier = "mid"
                signals.append(f"{count}+ employees")
            elif count >= 10:
                tier = "smb"
                signals.append(f"{count}+ employees")
            else:
                tier = "micro"
                signals.append(f"{count} employees")

        # Job postings page = hiring = growing
        if any(x in text for x in ["/careers", "/jobs", "/join-us", "/join-our-team", "we are hiring", "open positions", "job openings"]):
            signals.append("Active hiring")
            if tier == "unknown": tier = "smb"

        # Multiple locations
        loc_count = len(_re.findall(r'(?:office|location|branch)\s+in\s+[a-z]', text))
        if loc_count >= 3:
            signals.append(f"{loc_count}+ locations")
            if tier in ("unknown", "smb"): tier = "mid"
        elif loc_count >= 1:
            signals.append("Multiple locations")

        # Enterprise tech stack
        enterprise_tech = []
        if "salesforce" in text: enterprise_tech.append("Salesforce")
        if "hubspot" in text: enterprise_tech.append("HubSpot")
        if "marketo" in text: enterprise_tech.append("Marketo")
        if "workday" in text: enterprise_tech.append("Workday")
        if enterprise_tech:
            signals.append(f"Uses {', '.join(enterprise_tech[:2])}")
            if tier in ("unknown", "smb") and "salesforce" in text: tier = "mid"

        # Marketing team indicator
        if any(x in text for x in ["marketing manager", "marketing director", "head of marketing", "cmo", "chief marketing"]):
            signals.append("Has marketing team")
            if tier in ("unknown"): tier = "smb"

        # Awards/certifications = established
        if any(x in text for x in ["bbb accredited", "inc 500", "fortune 500", "award winning"]):
            signals.append("Industry recognition")

        # Solo indicators = micro
        if any(x in text for x in ["family owned", "family-owned", "owner operated", "owner-operated", "i am", "i have been"]):
            signals.append("Owner operated")
            if tier == "unknown": tier = "micro"

    except Exception:
        pass

    # Default if no signals found
    if tier == "unknown":
        tier = "smb"

    return {
        "size_tier": tier,
        "size_signals": signals,
        "employee_estimate": employee_estimate,
    }


def calculate_icp_score(audit: dict, biz: dict) -> dict:
    """
    Score a business against Eversite's ideal customer profile.
    Returns icp_score 0-100 and breakdown dict.
    
    Ideal prospect:
    - Website score 45-65 (needs help, has some investment)
    - SMB size 10-50 employees
    - Owner-led, no in-house marketing team
    - Basic tech stack (not Salesforce/enterprise)
    - Established business (20-200 reviews)
    - Home services vertical
    """
    score = 0
    breakdown = {}

    # 1. Website sweet spot (30 pts)
    ws = audit.get("website_score", 0)
    if 45 <= ws <= 65:
        website_pts = 30
    elif 35 <= ws < 45 or 65 < ws <= 75:
        website_pts = 20
    elif 25 <= ws < 35 or 75 < ws <= 85:
        website_pts = 10
    else:
        website_pts = 0
    score += website_pts
    breakdown["website_fit"] = website_pts

    # 2. Company size (25 pts)
    size_tier = audit.get("size_tier", "unknown")
    if size_tier == "smb":
        size_pts = 25
    elif size_tier == "micro":
        size_pts = 15
    elif size_tier == "mid":
        size_pts = 10
    elif size_tier == "enterprise":
        size_pts = 0
    else:
        # Use reviews as proxy
        rv = biz.get("reviews", 0)
        if 20 <= rv <= 200:
            size_pts = 20
        elif rv < 20:
            size_pts = 10
        else:
            size_pts = 5
    score += size_pts
    breakdown["size_fit"] = size_pts

    # 3. Marketing signals (15 pts)
    # No marketing team = high opportunity for Eversite
    size_signals = audit.get("size_signals", [])
    has_marketing = any("marketing" in s.lower() for s in size_signals)
    has_hiring = any("hiring" in s.lower() for s in size_signals)
    
    if not has_marketing and not has_hiring:
        marketing_pts = 15  # No marketing team = Eversite can own it
    elif has_hiring and not has_marketing:
        marketing_pts = 10  # Growing but no marketing yet
    else:
        marketing_pts = 5   # Already has marketing function
    score += marketing_pts
    breakdown["marketing_opportunity"] = marketing_pts

    # 4. Tech stack signals (15 pts)
    # Basic tech = no in-house capability = needs Eversite
    dims = audit.get("dimensions", {})
    tech_score = dims.get("tech", {}).get("score", 5) if dims else 5
    has_enterprise_tech = any("salesforce" in s.lower() or "hubspot" in s.lower() 
                               for s in size_signals)
    
    if has_enterprise_tech:
        tech_pts = 5   # Has tools, maybe has team too
    elif tech_score <= 3:
        tech_pts = 15  # Very basic = high opportunity
    elif tech_score <= 6:
        tech_pts = 10  # Some tools but not sophisticated
    else:
        tech_pts = 5
    score += tech_pts
    breakdown["tech_opportunity"] = tech_pts

    # 5. Business quality (15 pts)
    # Established enough to have budget
    rv = biz.get("reviews", 0)
    r = biz.get("rating", 0)
    if 20 <= rv <= 300 and r >= 4.0:
        quality_pts = 15  # Established, good reputation
    elif rv >= 10 and r >= 3.5:
        quality_pts = 10
    elif rv > 0:
        quality_pts = 5
    else:
        quality_pts = 3   # No reviews = unknown
    score += quality_pts
    breakdown["business_quality"] = quality_pts

    # ICP tier label
    if score >= 80:
        icp_tier = "A"
        icp_label = "🎯 Perfect ICP"
    elif score >= 65:
        icp_tier = "B"
        icp_label = "✅ Good ICP"
    elif score >= 45:
        icp_tier = "C"
        icp_label = "⚡ Possible"
    else:
        icp_tier = "D"
        icp_label = "✗ Poor fit"

    # Ad spend bonus — running ads with bad website = highest priority
    running_ads = audit.get("running_ads", [])
    if running_ads and website_pts <= 20:
        ad_bonus = 15
        score = min(100, score + ad_bonus)
        breakdown["ad_spend_bonus"] = ad_bonus
    elif running_ads:
        ad_bonus = 8
        score = min(100, score + ad_bonus)
        breakdown["ad_spend_bonus"] = ad_bonus

    # Build explanation pills
    pills = []
    if running_ads: pills.append(f"Running {'+'.join(running_ads)}")
    if audit.get("revenue_leak"): pills.append("💸 Revenue leak")
    if website_pts >= 20: pills.append("Sweet spot website")
    elif website_pts == 0: pills.append("Website out of range")
    if size_pts >= 20: pills.append("SMB size ✓")
    elif size_pts == 0: pills.append("Too large")
    if marketing_pts >= 15: pills.append("No in-house marketing")
    elif marketing_pts == 5: pills.append("Has marketing team")
    if tech_pts >= 15: pills.append("Basic tech stack")
    elif tech_pts == 5: pills.append("Enterprise tech")
    if quality_pts >= 15: pills.append("Established business")
    elif quality_pts <= 3: pills.append("Low reviews")

    # Combined opportunity score (website 40% + ICP 60%)
    ws = audit.get("website_score", 0)
    combined = round(ws * 0.4 + score * 0.6)

    return {
        "icp_score": score,
        "icp_tier": icp_tier,
        "icp_label": icp_label,
        "icp_breakdown": breakdown,
        "icp_pills": pills,
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
        await log("No businesses found — try different search terms")
        return []

    await log(f"Found {len(businesses)} businesses — running 58-point audit...")
    results = []

    for i, biz in enumerate(businesses, 1):
        await log(f"  [{i}/{len(businesses)}] {biz['name']}")
        audit = await audit_url(biz.get("website", ""))

        # Size signal detection from website content
        size_signals = await detect_size_signals(biz.get("website", ""), biz.get("name", ""))
        audit.update(size_signals)

        # Intelligence signals from HTML (already fetched in audit_url)
        # Re-fetch is cheap since it's cached by the OS/connection pool
        try:
            import httpx as _httpx
            _h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with _httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as _c:
                _r = await _c.get(biz.get("website", ""), headers=_h)
                _html = _r.text
        except Exception:
            _html = ""
        intel = await detect_intelligence_signals(_html, audit.get("website_score", 0))
        audit.update(intel)

        # Business quality score from reviews/rating
        bq = 0
        r, rv = biz.get("rating", 0), biz.get("reviews", 0)
        if rv >= 200: bq += 25
        elif rv >= 100: bq += 20
        elif rv >= 50: bq += 12
        elif rv >= 20: bq += 6
        if r >= 4.7: bq += 20
        elif r >= 4.3: bq += 12
        elif r >= 4.0: bq += 6

        # Sweet spot scoring — ideal prospect is in the middle
        # Website score 45-65 = sweet spot (needs help but has some investment)
        # Too low (0-30) = probably no budget
        # Too high (70+) = already sorted or has in-house team
        ws = audit.get("website_score", 0)
        SWEET_SPOT = 55  # centre of ideal range
        IDEAL_RANGE = 15  # +/- from centre = full score
        distance = abs(ws - SWEET_SPOT)
        if distance <= IDEAL_RANGE:
            website_fit = 100
        elif distance <= IDEAL_RANGE * 2:
            website_fit = 100 - (distance - IDEAL_RANGE) * 5
        else:
            website_fit = max(0, 100 - (distance - IDEAL_RANGE) * 8)

        # Size fit from detected tier + reviews proxy
        size_tier = audit.get("size_tier", "unknown")
        if size_tier == "smb":
            size_fit = 100   # perfect - 10-50 employees
        elif size_tier == "micro":
            size_fit = 60    # small but possible
        elif size_tier == "mid":
            size_fit = 75    # borderline - worth checking
        elif size_tier == "enterprise":
            size_fit = 20    # too big - own teams
        else:
            # Fall back to review count proxy
            if 20 <= rv <= 200:
                size_fit = 90
            elif rv < 20:
                size_fit = max(40, rv * 3)
            else:
                size_fit = max(50, 100 - (rv - 200) // 10)

        # Combined sweet spot priority
        # website_fit: are they in the right zone to need + afford help?
        # biz_quality: are they a real established business?
        # size_fit: are they the right size?
        priority = int(website_fit * 0.5 + bq * 0.3 + size_fit * 0.2)

        # ICP score
        icp = calculate_icp_score(audit, biz)

        results.append({
            **biz, **audit,
            "biz_quality": bq,
            "priority_score": priority,
            "website_fit": website_fit,
            "size_fit": size_fit,
            **icp,
        })
        await asyncio.sleep(0.3)

    results.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    await log(f"Done — {len(results)} businesses scored across 58 signals")
    return results
