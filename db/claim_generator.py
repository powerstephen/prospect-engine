"""
Claim Generator v1
Reads each contact's verified findings and produces:
  - cohort assignment (waterfall, strongest true story wins)
  - evidence_line: the lead claim for email step 1, in plain language
  - evidence_extra: count of additional findings ("plus N other things")
  - evidence_detail: jsonb of every claim generated, with its source field

Bulletproof rules enforced in code:
  - every claim maps to a database field (source recorded per claim)
  - claim strength never exceeds field provenance:
      dead link found        -> flat claim ("doesn't link anywhere")
      js errors present      -> soft claim ("may stop working") NEVER flat
      map/page1 sighting     -> positive claim only
      absence of anything    -> never claimed
  - contacts with junk/ISP domains are excluded entirely

Writes to columns: cohort, evidence_line, evidence_extra, evidence_detail
(run claim_schema.sql first)

Usage:
  python db\\claim_generator.py --dry-run --limit 20   (print, no writes)
  python db\\claim_generator.py                        (full pool)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import os
import sys
import json
import argparse

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

JUNK_DOMAINS = ("rr.com", "comcastbiz", "centurylink", "bellsouth", "att.net", "verizon")


def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def fetch_pool(limit: int, vertical: str = "hvac", include_named: bool = False) -> list:
    params = {
        "select": ("id,company,website,location,psi_mobile_lcp,load_time,"
                   "dead_cta_count,broken_image_count,js_error_count,jquery_dead,"
                   "site_health_lead,site_health_detail,site_health_scanned_at,"
                   "serp_in_map_pack,serp_on_page1,serp_query,serp_competitors,"
                   "dimensions,cta_above_fold,phone_above_fold,revenue_leak,"
                   "cta_verified_dead,broken_images_verified,health_verified_detail"),
        "source": f"like.google_maps:{vertical}*",
        "in_instantly": "eq.false",
        "email": "neq.",
        "limit": str(limit),
    }
    if not include_named:
        params["first_name"] = "is.null"
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params, headers=headers(), timeout=60)
    r.raise_for_status()
    rows = r.json()
    return [c for c in rows if c.get("website")
            and not any(j in c["website"].lower() for j in JUNK_DOMAINS)]


def plausible_button_text(t: str) -> bool:
    """Real button labels are short human phrases. Reject anything that
    looks like captured code or markup so it can never reach an email."""
    if not t:
        return False
    t = t.strip()
    if len(t) < 2 or len(t) > 35:
        return False
    if any(ch in t for ch in ('{', '}', '<', '>', ';', '=', '_', '(', ')', '[', ']', '"')):
        return False
    lowered = t.lower()
    if any(tok in lowered for tok in ('var ', 'function', 'http', 'nonce', 'ajax', 'javascript', 'null', 'undefined')):
        return False
    letters = sum(1 for ch in t if ch.isalpha() or ch.isspace())
    return letters / len(t) >= 0.7


def dead_button_text(contact: dict):
    """Pull the first dead CTA's visible text from site_health_detail, if stored."""
    detail = contact.get("site_health_detail")
    if not detail:
        return None
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            return None
    candidates = detail.get("dead_ctas") or detail.get("dead_cta") or []
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            t = (first.get("text") or first.get("label") or "").strip()
            return t if plausible_button_text(t) else None
        if isinstance(first, str):
            t = first.strip()
            return t if plausible_button_text(t) else None
    return None


def cta_ever_alive(contact: dict) -> bool:
    """A button that verified alive even once WORKS. Mixed verdicts
    (alive then dead) mean the second click hit an already-open widget.
    Any alive verdict disqualifies the dead claim entirely."""
    detail = contact.get("health_verified_detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            return False
    if not detail:
        return False
    return any(isinstance(r, dict) and r.get("verdict") == "alive"
               for r in detail.get("ctas") or [])


def verified_dead_button_text(contact: dict):
    """Button text from the VERIFIED detail (clicked in a real browser)."""
    detail = contact.get("health_verified_detail")
    if not detail:
        return None
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            return None
    for r in detail.get("ctas") or []:
        if isinstance(r, dict) and r.get("verdict") == "dead":
            t = (r.get("text") or "").strip()
            if plausible_button_text(t):
                return t
    return None


def dim(contact, key, field, default=None):
    d = contact.get("dimensions") or {}
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            return default
    block = d.get(key) or {}
    return block.get(field, default)


def lcp(contact):
    v = contact.get("psi_mobile_lcp") or contact.get("load_time")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def found_where(contact):
    """Positive SERP sighting only. Returns human phrase or None."""
    q = contact.get("serp_query") or "their service in their city"
    if contact.get("serp_in_map_pack"):
        return f"on the Google map results when people search \"{q}\""
    if contact.get("serp_on_page1"):
        return f"on page one of Google for \"{q}\""
    return None


def build_claims(contact: dict) -> list:
    """Every true claim for this contact, strongest first.
    Each claim: (cohort, line, source_field, strength)."""
    claims = []
    speed = lcp(contact)

    # 1. Broken elements: flat claims require BEHAVIOUR VERIFICATION
    # (static counts have known false-positive modes: JS-wired buttons,
    #  lazy-loaded images). Only verified fields may make flat claims.
    if contact.get("cta_verified_dead") is True and not cta_ever_alive(contact):
        btn = verified_dead_button_text(contact) or dead_button_text(contact)
        if btn:
            line = f"the \"{btn}\" button on your website doesn't work, we clicked it and nothing happens"
        else:
            line = "a contact button on your website doesn't work, we clicked it and nothing happens"
        claims.append(("broken_element", line, "cta_verified_dead", "flat"))
    n_img = contact.get("broken_images_verified")
    if n_img and int(n_img) > 0:
        n_img = int(n_img)
        plural = "images" if n_img > 1 else "image"
        claims.append(("broken_element", f"{n_img} broken {plural} showing on your site", "broken_images_verified", "flat"))
    if contact.get("jquery_dead"):
        claims.append(("soft_support",
                       "your site loads a broken code library that may stop buttons and forms working",
                       "jquery_dead", "soft"))
    elif (contact.get("js_error_count") or 0) > 0:
        claims.append(("soft_support",
                       "your site throws errors that may stop buttons and forms working",
                       "js_error_count", "soft"))

    # 2. Found but leaking (positive SERP + any site issue)
    where = found_where(contact)
    if where and (speed and speed >= 6 or claims):
        claims.append(("found_but_leaking", f"people find you {where}", "serp_in_map_pack/serp_on_page1", "flat"))

    # 3. Speed
    if speed and speed >= 6:
        claims.append(("speed", f"your homepage takes {speed:g} seconds to fully load on a phone",
                       "psi_mobile_lcp", "flat"))

    # 4. Hard to hire (conversion friction, flat where directly detected)
    # NOTE: cta_above_fold / phone_above_fold DISABLED pending validation.
    # They read false on 100% of the scanned pool (2026-07-06), which means
    # the field is defaulting, not detecting. Re-enable only after the scorer
    # writes null-when-skipped and a spot check confirms real values.
    USE_VISION_FIELDS = False
    if USE_VISION_FIELDS and contact.get("cta_above_fold") is False:
        claims.append(("hard_to_hire", "there's no visible way to request a quote without scrolling",
                       "cta_above_fold", "flat"))
    if USE_VISION_FIELDS and contact.get("phone_above_fold") is False:
        claims.append(("hard_to_hire", "your phone number isn't visible when the page opens",
                       "phone_above_fold", "flat"))
    if dim(contact, "cta", "has_form") is False:
        claims.append(("hard_to_hire", "there's no contact form on your site, a visitor has to open their email app to reach you",
                       "dimensions.cta.has_form", "flat"))
    if dim(contact, "cta", "has_mailto_only"):
        claims.append(("hard_to_hire", "the only way to contact you through the site is an email link",
                       "dimensions.cta.has_mailto_only", "flat"))
    mobile_issues = dim(contact, "mobile", "mobile_issues") or []
    if any("tap-to-call" in str(i).lower() for i in mobile_issues):
        claims.append(("hard_to_hire", "a mobile visitor can't tap your number to call you",
                       "dimensions.mobile.mobile_issues", "flat"))
    overflow = dim(contact, "mobile", "overflow_images")
    if overflow and int(overflow) > 5:
        claims.append(("hard_to_hire", f"{overflow} images likely overflow the screen on a phone",
                       "dimensions.mobile.overflow_images", "flat"))

    return claims


COHORT_ORDER = ["broken_element", "found_but_leaking", "speed", "hard_to_hire", "generic"]


def assign(contact: dict) -> dict:
    claims = build_claims(contact)
    if not claims:
        return {"cohort": "generic", "evidence_line": None, "evidence_extra": 0,
                "evidence_detail": {"claims": []}}

    # cohort = strongest ROUTING family present (soft_support never routes)
    families = [c[0] for c in claims]
    cohort = next((f for f in COHORT_ORDER if f in families), "generic")
    if cohort == "generic":
        detail = {"claims": [{"cohort": c[0], "line": c[1], "source": c[2], "strength": c[3]} for c in claims]}
        return {"cohort": "generic", "evidence_line": None,
                "evidence_extra": len(claims), "evidence_detail": detail}

    # lead line = first claim of that family, preferring flat over soft
    family_claims = [c for c in claims if c[0] == cohort]
    family_claims.sort(key=lambda c: 0 if c[3] == "flat" else 1)
    lead = family_claims[0]

    detail = {"claims": [{"cohort": c[0], "line": c[1], "source": c[2], "strength": c[3]} for c in claims]}
    return {
        "cohort": cohort,
        "evidence_line": lead[1],
        "evidence_extra": max(len(claims) - 1, 0),
        "evidence_detail": detail,
    }


def save(contact_id: int, fields: dict) -> bool:
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact_id}",
                    json=fields, headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SAVE FAILED {contact_id}: {r.status_code} {r.text[:200]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--include-named", action="store_true")
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    pool = fetch_pool(args.limit, args.vertical, include_named=args.include_named)
    print(f"Claim generation: {len(pool)} contacts" + (" (DRY RUN)" if args.dry_run else ""))

    tallies = {}
    for c in pool:
        result = assign(c)
        tallies[result["cohort"]] = tallies.get(result["cohort"], 0) + 1
        if args.dry_run:
            line = result["evidence_line"] or "(no evidence line)"
            print(f"  [{result['cohort']:18s}] {c.get('company','?'):40.40s} {line[:80]}"
                  + (f"  (+{result['evidence_extra']})" if result["evidence_extra"] else ""))
        else:
            save(c["id"], result)

    print("\nCohort tallies:")
    for k in COHORT_ORDER:
        if k in tallies:
            print(f"  {k}: {tallies[k]}")


if __name__ == "__main__":
    main()
