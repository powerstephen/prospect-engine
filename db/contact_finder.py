"""
contact_finder.py - merged owner + confirmed-email discovery.

Sequence per lead:
  1. Search "owner of {company} {city}" -> extract a candidate name.
     If that comes back empty, retry once at STATE level (e.g. "texas"
     instead of a narrow city). A company's real HQ or the city Google
     has indexed it under often differs from whatever city landed in the
     scrape, and the broader query catches what the narrow one misses
     (proven case: Kodesh Constructions, HQ'd in Cisco TX, service area
     Dallas/Fort Worth/Austin/Abilene, a city-scoped search found
     nothing, "texas" found the owner immediately).
  2. If a name was found: search 'email for {name} of {company}' -> a
     REAL confirmation search. Accepts the company's own domain OR a
     personal freemail address.
  3. If NO name was found at all: try 'email for "{company}" {state}',
     a company-name-anchored search that needs no owner name. This is
     the step that was missing before, if owner search fails, the old
     code only fell to a weaker DOMAIN-based search, skipping the much
     stronger company-name search entirely.
  4. Only if all of that finds nothing: fall through to enrich.py's
     three-stage flow (site scrape -> "email for {domain}" -> pattern
     guess) as the final, lowest-confidence safety net.

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

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")

SLEEP_BETWEEN = 1.5

FREEMAIL_DOMAINS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "aol.",
                    "icloud.", "live.", "msn.")
PLACEHOLDER_LOCAL_PARTS = {"first", "last", "firstname", "lastname",
                           "fname", "lname", "name"}

STATE_NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
               "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
               "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
               "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
               "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
               "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
               "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
               "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
               "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
               "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
               "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
               "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
               "WI": "Wisconsin", "WY": "Wyoming", "DC": "Washington DC"}


def is_own_domain_or_freemail(email, domain):
    local = email.split("@")[0].lower()
    if local in PLACEHOLDER_LOCAL_PARTS:
        return False
    dom = email.split("@")[-1].lower()
    if any(dom.startswith(f) for f in FREEMAIL_DOMAINS):
        return True
    return clean_domain(domain) in email


OWNER_WORDS = r"(?i:owner[/\s-]operator|owner|founder|president|ceo|principal|proprietor)"
P_NAME_ROLE = re.compile(
    rf"([A-Z][a-z]+(?:\s[A-Z][a-z]+){{1,2}})\s*(?:,|-|\u2013|\u2014|\||\bis\b|\bserves as\b)?\s*(?:[Tt]he\s+)?{OWNER_WORDS}")
P_ROLE_NAME = re.compile(
    rf"(?:{OWNER_WORDS}|(?i:founded by|owned by|started by))\s*[,:]?\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+){{1,2}})")
# AI Overview often phrases it loosely: "the owner of X ... is listed
# publicly as NAME" - name sits far from the role word, neither pattern
# above catches it, this does.
P_LISTED_AS = re.compile(
    r"(?i:listed(?: publicly)? as|identified as|shown as)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})")

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
        for pat in (P_NAME_ROLE, P_ROLE_NAME, P_LISTED_AS):
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
            "first_name": "is.null",
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
    if limit:
        rows = rows[:limit]
    return rows


def domain_of(website):
    w = (website or "").lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    return w.split("/")[0].strip()


def city_of(location):
    return (location or "").split(",")[0].strip()


def state_of(location):
    parts = (location or "").split(",")
    if len(parts) < 2:
        return ""
    token = parts[1].strip().split()[0].strip().upper() if parts[1].strip() else ""
    return STATE_NAMES.get(token, "")


async def serp_search(query):
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
    state = state_of(c.get("location"))
    has_email = bool((c.get("email") or "").strip())
    has_name = bool((c.get("first_name") or "").strip())

    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    domain = clean_domain(website)

    fields = {}
    name = None
    votes = 0

    if not has_name:
        texts = await serp_search('owner of "' + company + '" ' + city)
        name, votes = extract_owner(texts, company)
        await asyncio.sleep(SLEEP_BETWEEN)
        if not name and state and state.lower() != city.lower():
            await log("    city-scoped owner search empty, retrying at state level...")
            texts = await serp_search('owner of "' + company + '" ' + state)
            name, votes = extract_owner(texts, company)
            await asyncio.sleep(SLEEP_BETWEEN)
        if name:
            conf = "high" if votes >= 2 else "medium"
            await log("    owner search: " + name + " (votes=" + str(votes) + ", " + conf + ")")
    else:
        name = ((c.get("first_name") or "") + " " + (c.get("last_name") or "")).strip()

    if name and not has_email:
        q = "email for " + name + " of " + company
        texts = await serp_search(q)
        candidates = []
        for t in texts:
            candidates.extend(extract_emails(t, domain))
        await asyncio.sleep(SLEEP_BETWEEN)
        best = pick_best_email(candidates, domain)
        if best and is_own_domain_or_freemail(best, domain):
            await log("    confirmed via name+company search: " + best)
            if not has_name:
                fields["first_name"] = name.split()[0]
                fields["last_name"] = " ".join(name.split()[1:])
                fields["owner_source"] = "contact_finder_owner_search"
            fields["email"] = best
            fields["email_source"] = "name_and_email_confirmed"
            fields["email_confidence"] = "high"
            return fields
        else:
            await log("    name+company confirmation found nothing, trying company-only search")

    if not name and not has_email:
        q2 = 'email for "' + company + '" ' + (state or city)
        texts2 = await serp_search(q2)
        candidates2 = []
        for t in texts2:
            candidates2.extend(extract_emails(t, domain))
        await asyncio.sleep(SLEEP_BETWEEN)
        best2 = pick_best_email(candidates2, domain)
        if best2 and is_own_domain_or_freemail(best2, domain):
            await log("    confirmed via company-name search: " + best2)
            fields["email"] = best2
            fields["email_source"] = "company_email_confirmed"
            fields["email_confidence"] = "high"
            return fields
        else:
            await log("    company-name search found nothing, falling back to enrich.py flow")

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

    stats = {"name_and_email": 0, "company_email": 0, "email_only": 0, "guess": 0, "none": 0, "errors": 0}

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
        elif src == "company_email_confirmed":
            stats["company_email"] += 1
        elif src in ("stage1_scrape", "stage2_search"):
            stats["email_only"] += 1
        elif src == "stage3_pattern_guess":
            stats["guess"] += 1

        print("    -> " + str(result))
        if args.live:
            save(c["id"], result)

    print("\n=== Done: name+email confirmed=" + str(stats["name_and_email"]) +
          " | company-only confirmed=" + str(stats["company_email"]) +
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
