"""
email_enrich.py - Email waterfall runner for contacts that HAVE a website
but no email address (the maps_scraper output shape).

Complements enrich_queue.py, which handles the opposite case
(status=no_website rows that need a website found first). This runner skips
straight to the email stage: db.enrich.find_emails_for_contact per contact.

Resumable: only pulls rows where email is null. Crash-safe: writes each
result immediately.

Usage (run as module, same as enrich_queue):
  python -m db.email_enrich --vertical roofers                (DRY RUN, default limit 5)
  python -m db.email_enrich --vertical roofers --live --limit 25
  python -m db.email_enrich --vertical roofers --live --limit 0   (0 = all pending)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (plus whatever db.enrich itself needs)
"""
import os
import sys
import asyncio

import httpx

from db.enrich import find_emails_for_contact

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "https://neonmrgszujadgfidlbj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

LIVE = "--live" in sys.argv
LIMIT = 5
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])
VERTICAL = "hvac"
if "--vertical" in sys.argv:
    VERTICAL = sys.argv[sys.argv.index("--vertical") + 1].strip().lower()

SLEEP_BETWEEN = 2.0

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


async def fetch_pending(client) -> list:
    rows, page = [], 0
    while True:
        params = {
            "select": "id,company,website,location,email,first_name,last_name",
            "source": f"like.google_maps:{VERTICAL}*",
            "email": "is.null",
            "website": "not.is.null",
            "status": "not.in.(archived,timeout)",
            "order": "id.asc",
            "limit": "1000",
            "offset": str(page * 1000),
        }
        r = await client.get(f"{SUPABASE_URL}/rest/v1/contacts",
                             headers=SB_HEADERS, params=params)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        page += 1
    if LIMIT:
        rows = rows[:LIMIT]
    return rows


async def update_row(client, row_id: int, fields: dict):
    h = dict(SB_HEADERS)
    h["Prefer"] = "return=minimal"
    r = await client.patch(f"{SUPABASE_URL}/rest/v1/contacts",
                           headers=h, params={"id": f"eq.{row_id}"}, json=fields)
    if r.status_code not in (200, 201, 204):
        print(f"    SAVE FAILED {row_id}: {r.status_code} {r.text[:150]}")


async def main():
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY env var required")
        return
    mode = "LIVE" if LIVE else "DRY RUN (no writes)"
    print(f"=== email_enrich :: {mode} :: vertical={VERTICAL} :: "
          f"limit {'ALL' if not LIMIT else LIMIT} ===\n")

    async with httpx.AsyncClient(timeout=30) as client:
        rows = await fetch_pending(client)
        print(f"Pending (website present, email null): {len(rows)}\n")

        stats = {"found": 0, "none": 0, "errors": 0}
        for i, row in enumerate(rows, 1):
            company = (row.get("company") or "").strip()
            print(f"[{i}/{len(rows)}] {company}  [{row.get('website')}]")
            contact = {
                "website": row.get("website"),
                "company": company,
                "email": None,
                "first_name": row.get("first_name"),
                "last_name": row.get("last_name"),
            }
            try:
                res = await find_emails_for_contact(contact)
            except Exception as e:
                stats["errors"] += 1
                print(f"    ! email stage error: {str(e)[:120]}")
                await asyncio.sleep(SLEEP_BETWEEN)
                continue

            email = (res or {}).get("email")
            if email:
                stats["found"] += 1
                fields = {"email": email}
                # carry through any extras the waterfall returns without inventing them
                for k in ("first_name", "last_name", "email_source", "email_confidence"):
                    if (res or {}).get(k) and not row.get(k):
                        fields[k] = res[k]
                print(f"    -> {email}" + ("" if LIVE else "  [dry run, not written]"))
                if LIVE:
                    await update_row(client, row["id"], fields)
            else:
                stats["none"] += 1
                print("    -> no email found")
            await asyncio.sleep(SLEEP_BETWEEN)

        print(f"\n=== Done: {stats['found']} emails found, "
              f"{stats['none']} none, {stats['errors']} errors ===")
        if stats["found"] + stats["none"] > 0:
            rate = stats["found"] * 100 // max(stats["found"] + stats["none"], 1)
            print(f"Hit rate: {rate}%")


if __name__ == "__main__":
    asyncio.run(main())
