"""
site_health_check.py - HTML-based site-health detection.

Runs on the raw HTML that batch_scorer already fetches via httpx (no Playwright,
no AI-vision dependency, no browser). Detects the high-value, statically-visible
problems: broken images, dead CTAs, and missing jQuery/library references.

Call from score_contact after html is fetched:
    from db.site_health_check import check_site_health_html
    _health = check_site_health_html(html)

Returns a dict with: js_error_count, dead_cta_count, broken_image_count,
jquery_dead, site_health_lead, site_health_detail (dict).

Note: because this is static-HTML (not a live browser), js_error_count reflects
inline error indicators / missing-library signals, not runtime console errors.
It stays conservative and honest - it flags candidates, never asserts activity.
"""
import re

FREE_TRACKER_HINTS = ["bat.bing","doubleclick","facebook.com/tr","google-analytics",
    "googletagmanager","clarity.ms","hotjar","segment","quantserve","/collect","/track","pixel"]

CTA_WORDS = ["book now","book online","book appointment","schedule","request a quote",
    "request quote","get a quote","get quote","free quote","free estimate","get started",
    "contact us","call now","request appointment","get in touch","request service"]

def _looks_like_image(src):
    s = src.lower()
    if any(t in s for t in FREE_TRACKER_HINTS):
        return False
    return bool(re.search(r'[.](jpg|jpeg|png|gif|webp|svg|avif)([?]|$)', s)) \
        or '/wp-content/' in s or '/images/' in s or '/uploads/' in s

def check_site_health_html(html):
    out = {
        "js_error_count": 0,
        "dead_cta_count": 0,
        "broken_image_count": 0,
        "jquery_dead": False,
        "site_health_lead": None,
        "site_health_detail": None,
    }
    if not html or len(html) < 200:
        return out

    h = html.lower()

    # --- jQuery / library reference check ---
    # If the page CALLS jQuery ($ or jQuery) but never LOADS a jquery script, that's
    # a strong "interactive features broken" signal.
    uses_jquery = bool(re.search(r'\bjquery\b|\$\(', h))
    loads_jquery = "jquery" in h and (".js" in h or "jquery.min" in h or "ajax.googleapis" in h or "code.jquery" in h)
    jquery_dead = uses_jquery and not loads_jquery

    # --- dead CTAs: <a> with a CTA label but no real destination ---
    dead_ctas = []
    for m in re.finditer(r'<a\b([^>]*)>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        attrs = m.group(1)
        inner = re.sub(r'<[^>]+>', '', m.group(2))  # strip nested tags
        text = re.sub(r'\s+', ' ', inner).strip()
        low = text.lower()
        if len(text) <= 2:
            continue
        if not any(w in low for w in CTA_WORDS):
            continue
        href_m = re.search(r'href\s*=\s*["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        href = (href_m.group(1).strip() if href_m else "").strip()
        has_handler = bool(re.search(r'(onclick|x-on:click|@click)\s*=', attrs, re.IGNORECASE))
        dead_href = href in ("", "#", "javascript:void(0)", "javascript:;", "javascript:")
        is_anchor = href.startswith("#") and len(href) > 1
        if dead_href and not is_anchor and not has_handler:
            dead_ctas.append({"text": text[:50], "href": href or "(empty)"})

    # --- broken images: <img> with no src, or src that is clearly a placeholder ---
    broken_images = []
    for m in re.finditer(r'<img\b([^>]*)>', html, re.IGNORECASE):
        attrs = m.group(1)
        src_m = re.search(r'\bsrc\s*=\s*["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        src = (src_m.group(1).strip() if src_m else "")
        # no src at all, or a data-src lazy placeholder with empty real src
        if not src or src.startswith("data:image/svg") or src in ("#",):
            # only count if it looks like a real content image slot (has alt or class)
            if re.search(r'\balt\s*=', attrs, re.IGNORECASE) or re.search(r'\bclass\s*=', attrs, re.IGNORECASE):
                broken_images.append(src or "(no src)")

    out["jquery_dead"] = jquery_dead
    out["dead_cta_count"] = len(dead_ctas)
    out["broken_image_count"] = len(broken_images)
    # js_error_count: conservative - jquery_dead counts as 1 structural error signal
    out["js_error_count"] = 1 if jquery_dead else 0

    # lead angle (highest confidence first)
    lead = None
    if dead_ctas:
        names = ", ".join('"'+c["text"]+'"' for c in dead_ctas[:2])
        lead = ("dead_cta", f"contact button(s) link nowhere: {names}")
    elif jquery_dead:
        lead = ("jquery_dead", "page uses jQuery but does not load it - interactive features likely broken")
    elif broken_images:
        lead = ("broken_images", f"{len(broken_images)} image(s) appear to be missing a source")

    out["site_health_lead"] = lead[0] if lead else None
    out["site_health_detail"] = {
        "lead_message": lead[1] if lead else None,
        "dead_ctas": dead_ctas[:5],
        "broken_images": broken_images[:5],
    }
    return out
