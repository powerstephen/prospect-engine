"""Cohort Export v1: Instantly-ready CSVs, one per sequence."""
import os
import csv
import sys
import json
import argparse

import httpx

from competitor_blocklist import is_blocked, state_from_location

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

OUT_DIR = "exports"


def headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def fetch_pool(vertical: str = "hvac"):
    rows, page = [], 0
    while True:
        params = {
            "select": ("id,email,first_name,company,website,location,cohort,"
                       "evidence_line,evidence_extra,psi_mobile_lcp,serp_query,serp_competitors"),
            "source": f"like.google_maps:{vertical}*",
            "in_instantly": "eq.false",
            "email": "neq.",
            "cohort": "not.is.null",
            "status": "neq.archived",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params, headers=headers(), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    return rows


def city(location):
    return (location or "").split(",")[0].strip()


def load_time(v):
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return ""


def competitors(contact):
    home_state = state_from_location(contact.get("location") or "") or "FL"
    comp = contact.get("serp_competitors")
    if isinstance(comp, str):
        try:
            comp = json.loads(comp)
        except Exception:
            comp = None
    if not comp:
        return ("", "")
    names = []
    own = (contact.get("company") or "").strip().lower()
    for slot in ("map_pack", "organic_top3", "ads", "lsa"):
        for item in comp.get(slot) or []:
            n = (item.get("name") or "").strip()
            d = (item.get("domain") or "").strip()
            hit, _ = is_blocked(d, n, home_state=home_state)
            if hit:
                continue
            if n and n.lower() != own and n not in names and 2 < len(n) <= 60:
                names.append(n)
            if len(names) >= 2:
                return (names[0], names[1])
    return (names[0] if names else "", names[1] if len(names) > 1 else "")


def row_for(contact):
    c1, c2 = competitors(contact)
    return {
        "email": (contact.get("email") or "").strip().lower(),
        "firstName": (contact.get("first_name") or "").strip(),
        "companyName": (contact.get("company") or "").strip(),
        "website": (contact.get("website") or "").strip(),
        "city": city(contact.get("location") or ""),
        "loadTime": load_time(contact.get("psi_mobile_lcp")),
        "evidenceLine": (contact.get("evidence_line") or "").strip(),
        "extraCount": contact.get("evidence_extra") or 0,
        "searchQuery": (contact.get("serp_query") or "").strip(),
        "competitor1": c1,
        "competitor2": c2,
    }


def write_csv(name, rows):
    if not rows:
        print(f"  {name}: 0 rows, skipped")
        return
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  {name}: {len(rows)} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="hvac")
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    pool = fetch_pool(args.vertical)
    print(f"Exporting {len(pool)} contacts...")

    buckets = {"speed_a": [], "speed_b": [], "hard_to_hire": [],
               "found_but_leaking": [], "generic": [], "broken_element": []}

    for c in pool:
        row = row_for(c)
        if not row["email"]:
            continue
        cohort = c.get("cohort")
        if cohort == "speed":
            variant = "speed_a" if (c.get("id") or 0) % 2 == 0 else "speed_b"
            if variant == "speed_b" and not (row["competitor1"] and row["searchQuery"]):
                variant = "speed_a"
            buckets[variant].append(row)
        elif cohort in buckets:
            buckets[cohort].append(row)

    write_csv("speed_variant_a.csv", buckets["speed_a"])
    write_csv("speed_variant_b.csv", buckets["speed_b"])
    write_csv("hard_to_hire.csv", buckets["hard_to_hire"])
    write_csv("found_but_leaking.csv", buckets["found_but_leaking"])
    write_csv("generic.csv", buckets["generic"])
    write_csv("broken_element_manual.csv", buckets["broken_element"])

    total = sum(len(v) for v in buckets.values())
    print(f"\nDone. {total} contacts exported to .\\{OUT_DIR}\\")
    print("Reminder: after loading to Instantly, run tag_instantly.py immediately.")


if __name__ == "__main__":
    main()
