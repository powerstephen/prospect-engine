"""
facebook_email_enrich.py - Automates the "search company + Facebook" trick,
plus a second stage that searches the literal rejected email address itself
to find directory listings (BBB, Yelp, secondary FB pages) that echo it
next to a working alternate.

Usage:
  python3 db/facebook_email_enrich.py --vertical roofers --domains-file db/invalid_domains.txt
  python3 db/facebook_email_enrich.py --vertical roofers --domains-file db/invalid_domains.txt --live

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SERPAPI_KEY
"""
import os
import re
import csv
import sys
import time
import argparse

import httpx

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

SLEEP_BETWEEN = 1.5
REVIEW_CSV = os.path.join("exports", "facebook_email_review.csv")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FREEMAIL = ("gmail.", "yahoo.", "hotmail.", "outlook.", "icloud.", "aol.", "live.", "msn.")
GENERIC_LOCAL = ("info", "office", "contact", "contactus", "admin", "mail", "hello", "sales", "support")


def headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_pending(vertical: str, limit: int, only_invalid: bool) -> list:
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website,location,email,first_name",
            "source": f"like.google_maps:{vertical}*",
            "website": "not.is.null",
            "in_instantly": "eq.false",
            "status": "not.in.(archived,timeout)",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        if only_invalid:
            params["or"] = "(email.is.null,email.eq.)"
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params,
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


def load_domains_file(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return [ln.strip().lower() for ln in f if ln.strip()]


def fetch_by_domains(vertical: str, domains: list) -> list:
    rows = []
    for i in range(0, len(domains), 20):
        chunk = domains[i:i + 20]
        ors = ",".join(f"website.ilike.*{d}*" for d in chunk)
        params = {
            "select": "id,company,website,location,email,first_name",
            "source": f"like.google_maps:{vertical}*",
            "or": f"({ors})",
            "limit": "1000",
        }
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params,
                      headers=headers(False), timeout=60)
        r.raise_for_status()
        rows.extend(r.json())
    return rows


def domain_of(website: str) -> str:
    w = (website or "").lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    return w.split("/")[0].strip()


def serp_search(query: str) -> list:
    r = httpx.get("https://serpapi.com/search.json",
                  params={"engine": "google", "q": query, "api_key": SERPAPI_KEY,
                          "num": "10", "gl": "us", "hl": "en"},
                  timeout=60)
    r.raise_for_status()
    data = r.json()
    texts = []
    for res in data.get("organic_results") or []:
        t = " ".join(filter(None, [res.get("title"), res.get("snippet")]))
        link = (res.get("link") or "").lower()
        if t:
            texts.append((t, "facebook.com" in link))
    ao = data.get("ai_overview")
    if isinstance(ao, dict):
        blob = " ".join(str(v) for v in ao.values() if isinstance(v, str))
        if blob:
            texts.insert(0, (blob, False))
    return texts


def extract_alternate(texts_with_fb_flag: list, current_email: str, own_domain: str):
    current = (current_email or "").lower()
    for text, is_fb in texts_with_fb_flag:
        for m in EMAIL_RE.finditer(text):
            email = m.group(0).lower()
            if email == current:
                continue
            local, _, dom = email.partition("@")
            if local in GENERIC_LOCAL:
                continue
            is_free = any(dom.startswith(f) or ("." + f) in dom for f in FREEMAIL)
            if is_free:
                return email, "freemail", True, is_fb
            if own_domain and dom == own_domain:
                return email, "high", False, is_fb
            return email, "medium", False, is_fb
    return None, None, False, False


def extract_email(texts_with_fb_flag: list, own_domain: str):
    for text, is_fb in texts_with_fb_flag:
        for m in EMAIL_RE.finditer(text):
            email = m.group(0).lower()
            local, _, dom = email.partition("@")
            if local in GENERIC_LOCAL:
                continue
            is_free = any(dom.startswith(f) or ("." + f) in dom for f in FREEMAIL)
            if is_free:
                return email, "freemail", True, is_fb
            if own_domain and dom == own_domain:
                return email, "high", False, is_fb
            return email, "medium", False, is_fb
    return None, None, False, False


def save(contact_id: int, fields: dict) -> bool:
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact_id}",
                    json=fields, headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"    SAVE FAILED {contact_id}: {r.status_code} {r.text[:150]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="hvac")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only-invalid", action="store_true")
    ap.add_argument("--domains-file", default=None)
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_SERVICE_KEY", "SERPAPI_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    if args.domains_file:
        domains = load_domains_file(args.domains_file)
        pool = fetch_by_domains(args.vertical, domains)
    else:
        pool = fetch_pending(args.vertical, args.limit, args.only_invalid)

    mode = "LIVE" if args.live else "DRY RUN"
    print(f"=== facebook_email_enrich :: {mode} :: vertical={args.vertical} :: {len(pool)} leads ===\n")

    os.makedirs("exports", exist_ok=True)
    review_rows = []
    stats = {"high": 0, "medium": 0, "freemail_skipped": 0, "none": 0, "errors": 0}

    for i, c in enumerate(pool, 1):
        company = (c.get("company") or "").strip()
        dom = domain_of(c.get("website"))
        query = f'"{company}" Facebook'
        print(f"[{i}/{len(pool)}] {company}")
        try:
            texts = serp_search(query)
            email, conf, is_free, from_fb = extract_email(texts, dom)
            stage = "facebook_snippet"
            if not email and (c.get("email") or "").strip():
                q2 = f'"{c["email"].strip()}"'
                texts2 = serp_search(q2)
                email, conf, is_free, from_fb = extract_alternate(texts2, c["email"], dom)
                if email:
                    stage = "email_lookup_alternate"
        except Exception as e:
            stats["errors"] += 1
            print(f"    ! search error: {str(e)[:100]}")
            time.sleep(SLEEP_BETWEEN)
            continue

        if not email:
            stats["none"] += 1
            print("    -> no email found")
        elif conf == "freemail":
            stats["freemail_skipped"] += 1
            print(f"    -> found {email} but it's freemail, logged not written")
            review_rows.append({"id": c["id"], "company": company, "domain": dom,
                                "found_email": email, "confidence": "freemail (skipped)",
                                "from_facebook": from_fb, "stage": stage})
        elif conf == "high":
            stats["high"] += 1
            print(f"    -> {email}  (matches own domain, high, via {stage})")
            if args.live:
                save(c["id"], {"email": email, "email_source": stage,
                              "email_confidence": "high"})
        else:
            stats["medium"] += 1
            print(f"    -> {email}  (different domain, medium, via {stage}, queued for review)")
            review_rows.append({"id": c["id"], "company": company, "domain": dom,
                                "found_email": email, "confidence": "medium",
                                "from_facebook": from_fb, "stage": stage})

        time.sleep(SLEEP_BETWEEN)

    if review_rows:
        with open(REVIEW_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(review_rows[0].keys()))
            w.writeheader()
            w.writerows(review_rows)
        print(f"\n{len(review_rows)} candidates for review -> {REVIEW_CSV}")

    print(f"\n=== Done: high {stats['high']} | medium(review) {stats['medium']} | "
          f"freemail(logged) {stats['freemail_skipped']} | none {stats['none']} | "
          f"errors {stats['errors']} ===")
    if not args.live:
        print("(dry run: nothing written, run again with --live to commit high-confidence emails)")


if __name__ == "__main__":
    main()
