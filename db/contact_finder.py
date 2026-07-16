"""
contact_finder.py - merged owner + confirmed-email discovery.

Replaces the two-pass owner_enrich.py + enrich.py workflow with one pass
that does what a human researcher does: search for the owner, then search
to CONFIRM their email, rather than guessing it from a pattern.

Sequence per lead:
  1. Search "owner of {company} {city}" -> extract a candidate name
     (same voting logic as owner_enrich.py: 2+ agreeing sources = high
     confidence, 1 source = medium).
  2. If a name was found: search '"{name}" "{company}" email' -> a REAL
     confirmation search, not a pattern guess. If it returns an email on
     the company's own domain, that is the strongest possible result:
     verified name + verified email from one research pass, exactly what
     a manual Google search proved out on Xtreme Roofing and Rubinsky
     Roofing this week.
  3. If step 2 finds nothing, fall through to the existing enrich.py
     three-stage flow (site scrape -> "email for {domain}" search ->
     pattern guess) as a safety net so nothing regresses.

Confidence levels written:
  - name_and_email_confirmed  (highest: both found via real search)
  - email_only_confirmed      (enrich.py stage1/stage2 fallback found an
                                on-domain email, no name)
  - stage3_pattern_guess      (last resort, unverified, as before)

Usage:
  python3 db/contact_finder.py --vertical roofers --limit 20             (DRY RUN)
  python3 db/contact_finder.py --vertical roofers --limit 20 --live
  python3 db/contact_finder.py --vertical roofers --live --limit 0       (0 = all pending)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SERPAPI_KEY
"""
import os
import re
import sys
import time
import asyncio
import argparse

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.enrich import (
    clean_domain, extract_emails, is_noise, is_valid_email_shape,
    stage1_website_scrape, stage2_google_search, stage3_pattern_guess,
    pick_best_email,
)

FREEMAIL_DOMAINS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "aol.",
                    "icloud.", "live.", "msn.")
PLACEHOLDER_LOCAL_PARTS = {"first", "last", "firstname", "lastname",
                           "fname", "lname", "name"}


def is_own_domain_or_freemail(email, domain):
    """True if email is on the company's own domain, OR a personal
    freemail address (both are trustworthy once confirmed by a
    name+company search). False for a DIFFERENT custom domain, which
    is the signature of a wrong-company match."""
    local = email.split("@")[0].lower()
    if local in PLACEHOLDER_LOCAL_PARTS:
        return False
    dom = email.split("@")[-1].lower()
    if any(dom.startswith(f) for f in FREEMAIL_DOMAINS):
        return True
    return clean_domain(domain) in email

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")

SLEEP_BETWEEN = 1.5

# --- owner-name extraction, same approach as owner_enrich.py ---
OWNER_WORDS = r"(?i:owner[/\s-]operator|owner|founder|president|ceo|principal|proprietor)"
P_NAME_ROLE = re.compile(
    rf"([A-Z][a-z]+(?:\s[A-Z][a-z]+){{1,2}})\s*(?:,|-|\u2013|\u2014|\||\bis\b|\bserves as\b)?\s*(?:[Tt]he\s+)?{OWNER_WORDS}")
P_ROLE_NAME = re.compile(
    rf"(?:{OWNER_WORDS}|(?i:founded by|owned by|started by))\s*[,:]?\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+){{1,2}})")

STOPWORDS = {"roofing", "roof", "construction", "company", "contractor", "contractors",
             "llc", "inc", "texas", "the", "and", "general", "commercial", "residential",
             "google", "facebook", "yelp", "bbb", "linkedin", "reviews", "best", "top",
             "defendant", "plaintiff", "attorney", "court",
             "trusted", "quality", "premier", "elite", "pro", "solutions", "services",
             "group", "team", "systems", "specialists", "experts", "masters", "remodeling",
             "builders", "certified", "professional", "complete", "reliable", "affordable",
             "premium", "advanced", "superior", "prime", "select", "custom"}


def plausible_name(name, company):
    parts = name.split()
    if len(parts) < 2 or len(parts) > 3:
        return False
    lowered = {p.lower() for p in parts}
    if lowered & STOPWORDS:
        return False
    comp_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", company)}
    if lowered <= comp_tokens:
        return False
    return all(re.fullmatch(r"[A-Z][a-z]+", p) for p in parts)


def extract_owner(texts, company):
    votes = {}
    for t in texts:
        seen_here = set()
        for pat in (P_NAME_ROLE, P_ROLE_NAME):
            for m in pat.finditer(t):
                name = m.group(1).strip()
                if plausible_name(name, company) and name not in seen_here:
                    votes[name] = votes.get(name, 0) + 1
                    seen_here.add(name)
    if not votes:
        return None, 0
    best = max(votes.items(), key=lambda kv: kv[1])
    return best


def headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_pending(vertical, limit):
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website,location,email,first_name,last_name",
            "source": "like.google_maps:" + vertical + "*",
            "website": "not.is.null",
            "in_instantly": "eq.false",
            "status": "not.in.(archived,timeout)",
            "order": "psi_mobile_lcp.desc.nullslast",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        r = httpx.get(SUPABASE_URL + "/rest/v1/contacts", params=params,
                      headers=headers(False), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    # prioritise leads missing BOTH name and email, since that's the whole win
    rows.sort(key=lambda c: 0 if not c.get("email") else 1)
    if limit:
        rows = rows[:limit]
    return rows


def city_of(location):
    return (location or "").split(",")[0].strip()


async def serp_search(query):
    """One SerpAPI call, returns a flat list of text blobs to scan
    (organic snippets/titles + AI overview + answer box)."""
    if not SERPAPI_KEY:
        return []
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get("https://serpapi.com/search.json",
                        params={"engine": "google", "q": query, "api_key": SERPAPI_KEY,
                                "num": 10, "gl": "us", "hl": "en"})
        data = r.json()
    texts = []
    for res in data.get("organic_results") or []:
        t = " ".join(filter(None, [res.get("title"), res.get("snippet")]))
        if t:
            texts.append(t)
    for box in ("answer_box", "ai_overview", "knowledge_graph"):
        b = data.get(box)
        if isinstance(b, dict):
            blob = " ".join(str(v) for v in b.values() if isinstance(v, str))
            if blob:
                texts.insert(0, blob)
    return texts


def save(contact_id, fields):
    r = httpx.patch(SUPABASE_URL + "/rest/v1/contacts?id=eq." + str(contact_id),
                    json=fields, headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print("    SAVE FAILED " + str(contact_id) + ": " + str(r.status_code) + " " + r.text[:150])
        return False
    return True


async def find_contact(c, log):
    company = (c.get("company") or "").strip()
    website = (c.get("website") or "").strip()
    city = city_of(c.get("location"))
    has_email = bool((c.get("email") or "").strip())
    has_name = bool((c.get("first_name") or "").strip())

    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    domain = clean_domain(website)

    fields = {}
    name = None

    # Stage A: owner search (skip if we already have a name)
    if not has_name:
        texts = await serp_search('owner of "' + company + '" ' + city)
        name, votes = extract_owner(texts, company)
        await asyncio.sleep(SLEEP_BETWEEN)
        if name:
            fn, ln = name.split()[0], " ".join(name.split()[1:])
            conf = "high" if votes >= 2 else "medium"
            await log("    owner search: " + name + " (votes=" + str(votes) + ", " + conf + ")")
    else:
        name = (c.get("first_name") or "") + " " + (c.get("last_name") or "")
        name = name.strip()

    # Stage B: confirmation search, only meaningful if we need an email.
    # Natural phrasing (not quoted-exact-phrase) is what actually finds
    # results: Google's own ranking beats forcing an exact-match query.
    # Domain match is NOT required here, unlike the nameless fallback
    # stages: a name+company pairing this specific is trustworthy even
    # when the confirmed address turns out to be a personal Gmail (common
    # for small contractors, proven on Aqua Werx Seamless Gutters).
    if name and not has_email:
        q = "email for " + name + " of " + company
        texts = await serp_search(q)
        candidates = []
        for t in texts:
            candidates.extend(extract_emails(t, domain))
        await asyncio.sleep(SLEEP_BETWEEN)
        best = pick_best_email(candidates, domain)
        if best and is_own_domain_or_freemail(best, domain):
            await log("    confirmed via search: " + best)
            if not has_name:
                fields["first_name"] = name.split()[0]
                fields["last_name"] = " ".join(name.split()[1:])
                fields["owner_source"] = "contact_finder_owner_search"
            fields["email"] = best
            fields["email_source"] = "name_and_email_confirmed"
            fields["email_confidence"] = "high"
            return fields
        else:
            await log("    confirmation search found nothing, falling back")
    elif not has_name and name is None:
        await log("    no owner found, falling back to enrich.py flow")

    # Fallback: existing enrich.py flow (only if email still missing)
    if not has_email:
        contact_for_fallback = dict(c)
        if name and not has_name:
            contact_for_fallback["first_name"] = name.split()[0]
            contact_for_fallback["last_name"] = " ".join(name.split()[1:])

        emails = await stage1_website_scrape(website, domain, log)
        best = pick_best_email(emails, domain) if emails else None
        if best and clean_domain(domain) in best:
            fields["email"] = best
            fields["email_source"] = "stage1_scrape"
            fields["email_confidence"] = "high"
        else:
            emails = await stage2_google_search(domain, company, log)
            best = pick_best_email(emails, domain) if emails else None
            if best and clean_domain(domain) in best:
                fields["email"] = best
                fields["email_source"] = "stage2_search"
                fields["email_confidence"] = "medium"
            else:
                emails = await stage3_pattern_guess(domain, contact_for_fallback, log)
                best = pick_best_email(emails, domain) if emails else None
                if best:
                    fields["email"] = best
                    fields["email_source"] = "stage3_pattern_guess"
                    fields["email_confidence"] = "low"

    if name and not has_name and "first_name" not in fields:
        fields["first_name"] = name.split()[0]
        fields["last_name"] = " ".join(name.split()[1:])
        fields["owner_source"] = "contact_finder_owner_search"

    return fields or None


async def main_async(args):
    missing = [v for v in ("SUPABASE_SERVICE_KEY",) if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)
    if not SERPAPI_KEY:
        print("WARNING: no SERPAPI_KEY, owner and confirmation searches will be skipped")

    pool = fetch_pending(args.vertical, args.limit)
    mode = "LIVE" if args.live else "DRY RUN"
    print("=== contact_finder :: " + mode + " :: vertical=" + args.vertical + " :: " + str(len(pool)) + " leads ===\n")

    stats = {"name_and_email": 0, "email_only": 0, "guess": 0, "none": 0, "errors": 0}

    async def log(msg):
        print(msg)

    for i, c in enumerate(pool, 1):
        company = (c.get("company") or "").strip()
        print("[" + str(i) + "/" + str(len(pool)) + "] " + company)
        try:
            result = await find_contact(c, log)
        except Exception as e:
            stats["errors"] += 1
            print("    ERROR: " + str(e)[:150])
            continue

        if not result:
            stats["none"] += 1
            print("    -> nothing found")
            continue

        src = result.get("email_source", "")
        if src == "name_and_email_confirmed":
            stats["name_and_email"] += 1
        elif src in ("stage1_scrape", "stage2_search"):
            stats["email_only"] += 1
        elif src == "stage3_pattern_guess":
            stats["guess"] += 1

        print("    -> " + str(result))
        if args.live:
            save(c["id"], result)

    print("\n=== Done: name+email confirmed=" + str(stats["name_and_email"]) +
          " | email only=" + str(stats["email_only"]) +
          " | low-conf guess=" + str(stats["guess"]) +
          " | none=" + str(stats["none"]) +
          " | errors=" + str(stats["errors"]) + " ===")
    if not args.live:
        print("(dry run: nothing written, add --live to commit)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
