"""
tag_instantly.py - Reconcile Supabase with Instantly reality.

Pulls every lead email from the Instantly workspace (API v2, paginated),
then sets in_instantly = true on every matching contact row. Run after
EVERY Instantly load, no exceptions: this is what keeps exports from
re-uploading people already in campaigns.

Read-only against Instantly; the only writes are the Supabase flags.

Usage:
  python db\\tag_instantly.py            (tag everything that matches)
  python db\\tag_instantly.py --dry-run  (show counts, write nothing)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, INSTANTLY_API_KEY
"""
import os
import sys
import time

import httpx

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
INSTANTLY_KEY = os.environ.get("INSTANTLY_API_KEY", "")

DRY_RUN = "--dry-run" in sys.argv
BATCH = 100


def sb_headers(minimal=True):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_instantly_emails() -> set:
    """Every lead email in the workspace, all campaigns, paginated."""
    emails = set()
    starting_after = None
    pages = 0
    while True:
        body = {"limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        r = httpx.post("https://api.instantly.ai/api/v2/leads/list",
                       json=body,
                       headers={"Authorization": f"Bearer {INSTANTLY_KEY}",
                                "Content-Type": "application/json"},
                       timeout=60)
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or []
        for lead in items:
            e = (lead.get("email") or "").strip().lower()
            if e:
                emails.add(e)
        pages += 1
        starting_after = data.get("next_starting_after")
        if not starting_after or not items:
            break
        time.sleep(0.3)
    print(f"Instantly: {len(emails)} unique lead emails across {pages} pages")
    return emails


def count_flagged() -> int:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts",
                  params={"select": "id", "in_instantly": "eq.true", "limit": "1"},
                  headers={**sb_headers(False), "Prefer": "count=exact"},
                  timeout=30)
    cr = r.headers.get("content-range", "")
    try:
        return int(cr.split("/")[-1])
    except ValueError:
        return -1


def tag_emails(emails: set) -> int:
    """Set in_instantly = true for matching contacts, in batches."""
    tagged = 0
    batch = []
    all_emails = sorted(emails)
    for i, e in enumerate(all_emails, 1):
        batch.append(e)
        if len(batch) >= BATCH or i == len(all_emails):
            quoted = ",".join('"' + b.replace('"', '') + '"' for b in batch)
            r = httpx.patch(
                f"{SUPABASE_URL}/rest/v1/contacts",
                params={"email": f"in.({quoted})", "in_instantly": "eq.false"},
                json={"in_instantly": True},
                headers={**sb_headers(False), "Prefer": "return=representation"},
                timeout=60,
            )
            if r.status_code in (200, 201):
                tagged += len(r.json() or [])
            else:
                print(f"  batch failed: {r.status_code} {r.text[:150]}")
            batch = []
            time.sleep(0.2)
    return tagged


def main():
    missing = [v for v in ("SUPABASE_SERVICE_KEY", "INSTANTLY_API_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    before = count_flagged()
    print(f"Supabase: in_instantly=true before run: {before}")

    emails = fetch_instantly_emails()
    if not emails:
        print("No leads returned from Instantly. Nothing to tag.")
        return

    if DRY_RUN:
        print("(dry run, no flags written)")
        return

    newly = tag_emails(emails)
    after = count_flagged()
    print(f"\nDone. newly tagged: {newly} | in_instantly=true now: {after}")
    print("Note: Instantly leads with no matching contact row (manual adds, "
          "edited emails) are not counted here.")


if __name__ == "__main__":
    main()
