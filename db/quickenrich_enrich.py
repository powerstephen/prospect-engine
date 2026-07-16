"""
quickenrich_enrich.py - fills name + email via the QuickEnrich API's domain
search endpoint.

has_email is intentionally NOT sent as a request param, their validator
rejects it regardless of format tried, and it's documented as optional
anyway. Filtering for a real email happens locally instead.

Guard: QuickEnrich's own dataset occasionally has bad rows (literal "N/A"
placeholders, or the company name itself sitting in the person fields).
Both are filtered before anything gets written.

Usage:
  python3 db/quickenrich_enrich.py --vertical roofers --limit 20          (DRY RUN)
  python3 db/quickenrich_enrich.py --vertical roofers --limit 20 --live
  python3 db/quickenrich_enrich.py --vertical roofers --live --limit 0    (0 = all pending)
  python3 db/quickenrich_enrich.py --vertical roofers --city Houston --live --limit 0

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, QUICKENRICH_API_KEY, QUICKENRICH_BASE_URL
"""
import os
import re
import sys
import time
import argparse

import httpx

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
QE_API_KEY   = os.environ.get("QUICKENRICH_API_KEY", "")
QE_BASE_URL  = os.environ.get("QUICKENRICH_BASE_URL", "").rstrip("/")

SLEEP_BETWEEN = 0.25

TITLE_TIER = [
    (0, ("owner", "founder", "ceo", "chief executive", "president", "principal")),
    (1, ("vice president", "vp", "general manager", "managing partner", "director")),
    (2, ("office manager", "operations")),
]


def rank_title(title):
    t = (title or "").lower()
    for tier, keywords in TITLE_TIER:
        if any(k in t for k in keywords):
            return tier
    return 3 if t.strip() else 4


def clean_domain(website):
    url = (website or "").strip().lower()
    for prefix in ["https://www.", "http://www.", "https://", "http://", "www."]:
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/").split("/")[0]


def clean_field(v):
    v = (v or "").strip()
    if v.lower() in ("", "n/a", "na", "none", "null", "-"):
        return ""
    return v


def looks_like_person_name(first, last, company):
    if not first or not last:
        return False
    combined = (first + " " + last).lower()
    comp_tokens = set(re.findall(r"[a-z0-9]+", (company or "").lower()))
    name_tokens = set(re.findall(r"[a-z0-9]+", combined))
    if not name_tokens:
        return False
    if name_tokens <= comp_tokens:
        return False
    return True


def sb_headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_pending(vertical, limit, city=None):
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website,location,email,first_name,last_name,phone",
            "source": "like.google_maps:" + vertical + "*",
            "website": "not.is.null",
            "in_instantly": "eq.false",
            "status": "not.in.(archived,timeout)",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        if city:
            params["location"] = "ilike." + city + "%"
        r = httpx.get(SUPABASE_URL + "/rest/v1/contacts", params=params,
                      headers=sb_headers(False), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    rows = [c for c in rows if not c.get("email") or not c.get("first_name")]
    if limit:
        rows = rows[:limit]
    return rows


def quickenrich_domain_search(domain):
    if not QE_BASE_URL:
        raise RuntimeError("QUICKENRICH_BASE_URL not set")
    url = QE_BASE_URL + "/api/employees/dataset-search"
    headers = {"Authorization": "Bearer " + QE_API_KEY, "Accept": "application/json"}
    params = {"company_url": domain, "page": 1}
    r = httpx.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 429:
        time.sleep(5)
        r = httpx.get(url, params=params, headers=headers, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError("HTTP " + str(r.status_code) + ": " + r.text[:500])
    data = r.json()
    employees = data.get("data") or []
    return [e for e in employees if clean_field(e.get("email"))]


def pick_best_employee(employees):
    if not employees:
        return None
    by_date = sorted(employees, key=lambda e: e.get("email_verification_date") or "", reverse=True)
    by_tier = sorted(by_date, key=lambda e: rank_title(e.get("title")))
    return by_tier[0]


def save(contact_id, fields):
    r = httpx.patch(SUPABASE_URL + "/rest/v1/contacts?id=eq." + str(contact_id),
                    json=fields, headers=sb_headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print("    SAVE FAILED " + str(contact_id) + ": " + str(r.status_code) + " " + r.text[:150])
        return False
    return True


def build_fields(contact, employee, company_name):
    fields = {}
    has_name = bool((contact.get("first_name") or "").strip())
    has_email = bool((contact.get("email") or "").strip())

    fn = clean_field(employee.get("first_name"))
    ln = clean_field(employee.get("last_name"))
    if not has_name and looks_like_person_name(fn, ln, company_name):
        fields["first_name"] = fn
        fields["last_name"] = ln
        fields["owner_source"] = "quickenrich_dataset_search"

    em = clean_field(employee.get("email"))
    if not has_email and em:
        fields["email"] = em.lower()
        fields["email_source"] = "quickenrich_dataset_search"
        fields["email_confidence"] = "high"

    phone = clean_field(employee.get("employee_phone"))
    if phone and not contact.get("phone"):
        fields["phone"] = phone

    return fields


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--city", default=None)
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_SERVICE_KEY", "QUICKENRICH_API_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)
    if not QE_BASE_URL:
        print("Missing QUICKENRICH_BASE_URL env var, set it before running")
        sys.exit(1)

    pool = fetch_pending(args.vertical, args.limit, args.city)
    mode = "LIVE" if args.live else "DRY RUN"
    scope = (" city=" + args.city) if args.city else ""
    print("=== quickenrich_enrich :: " + mode + " :: vertical=" + args.vertical + scope +
          " :: " + str(len(pool)) + " leads ===\n")

    stats = {"found": 0, "no_match": 0, "rejected": 0, "errors": 0, "credits_used": 0}

    for i, c in enumerate(pool, 1):
        company = (c.get("company") or "").strip()
        domain = clean_domain(c.get("website"))
        print("[" + str(i) + "/" + str(len(pool)) + "] " + company + " (" + domain + ")")
        try:
            employees = quickenrich_domain_search(domain)
        except Exception as e:
            stats["errors"] += 1
            print("    ERROR: " + str(e)[:300])
            time.sleep(SLEEP_BETWEEN)
            continue

        if not employees:
            stats["no_match"] += 1
            print("    -> no match")
            time.sleep(SLEEP_BETWEEN)
            continue

        stats["credits_used"] += 1
        best = pick_best_employee(employees)
        fields = build_fields(c, best, company)
        if fields:
            stats["found"] += 1
            print("    -> " + (best.get("first_name") or "?") + " " + (best.get("last_name") or "") +
                  " (" + (best.get("title") or "?") + ") " + str(fields))
            if args.live:
                save(c["id"], fields)
        else:
            stats["rejected"] += 1
            print("    -> matched but rejected (N/A fields, company-name leak, or already had name+email)")

        time.sleep(SLEEP_BETWEEN)

    print("\n=== Done: found=" + str(stats["found"]) + " rejected=" + str(stats["rejected"]) +
          " no_match=" + str(stats["no_match"]) + " errors=" + str(stats["errors"]) +
          " | credits used ~" + str(stats["credits_used"]) + " ===")
    if not args.live:
        print("(dry run: nothing written, add --live to commit)")


if __name__ == "__main__":
    main()
