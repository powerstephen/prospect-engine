"""
Batch scorer — scores all 'new' contacts in background.
Reads from Supabase contacts table, runs website audit + ICP scoring,
then auto-routes by opportunity score threshold.
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.supabase_client import (
    get_contacts_for_batch_score,
    update_contact_score,
    update_contact_status,
)

# Import Roaster Bot scoring engine
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scraper"))


SCORE_THRESHOLD_HOT = int(os.environ.get("SCORE_THRESHOLD_HOT", "70"))
SCORE_THRESHOLD_WARM = int(os.environ.get("SCORE_THRESHOLD_WARM", "50"))


async def score_contact(contact: dict, log_cb=None) -> dict:
    """Score a single contact's website and return results."""
    async def log(msg):
        if log_cb:
            await log_cb(msg)

    website = contact.get("website", "")
    if not website:
        return {"error": "no website"}

    await log(f"Scoring {website}...")

    try:
        from scraper.engine import audit_url, detect_size_signals, detect_intelligence_signals, calculate_icp_score
        import httpx

        audit = await audit_url(website)

        size_signals = await detect_size_signals(website, contact.get("company", ""))
        audit.update(size_signals)

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as c:
                r = await c.get(website, headers=headers)
                html = r.text
        except Exception:
            html = ""

        intel = await detect_intelligence_signals(html, audit.get("website_score", 0))
        audit.update(intel)

        icp = calculate_icp_score(audit, contact)

        combined = round(audit.get("website_score", 0) * 0.4 + icp["icp_score"] * 0.6)

        # Auto-route by threshold
        if combined >= SCORE_THRESHOLD_HOT:
            status = "scored"  # → ready for enrichment
        elif combined >= SCORE_THRESHOLD_WARM:
            status = "nurture"  # → warm sequence
        else:
            status = "archived"  # → not worth pursuing

        return {
            "opportunity_score": combined,
            "icp_score": icp["icp_score"],
            "website_score": audit.get("website_score", 0),
            "icp_tier": icp["icp_tier"],
            "intel_pills": icp.get("icp_pills", []),
            "size_signals": audit.get("size_signals", []),
            "revenue_leak": audit.get("revenue_leak", False),
            "status": status,
        }

    except Exception as e:
        await log(f"Error scoring {website}: {e}")
        return {"error": str(e), "status": "new"}


async def run_batch_score(limit: int = 50, log_cb=None) -> dict:
    """
    Score up to `limit` new contacts.
    Returns summary of results.
    """
    async def log(msg):
        if log_cb:
            await log_cb(msg)

    contacts = get_contacts_for_batch_score(limit=limit)
    if not contacts:
        await log("No new contacts to score.")
        return {"scored": 0, "hot": 0, "warm": 0, "archived": 0}

    await log(f"Batch scoring {len(contacts)} contacts...")

    results = {"scored": 0, "hot": 0, "warm": 0, "archived": 0, "errors": 0}

    for contact in contacts:
        try:
            result = await score_contact(contact, log_cb=log_cb)

            if "error" in result and result.get("status") != "archived":
                results["errors"] += 1
                continue

            update_contact_score(
                contact_id=contact["id"],
                opportunity_score=result.get("opportunity_score", 0),
                icp_score=result.get("icp_score", 0),
                website_score=result.get("website_score", 0),
                icp_tier=result.get("icp_tier", "D"),
                intel_pills=result.get("intel_pills", []),
                size_signals=result.get("size_signals", []),
                revenue_leak=result.get("revenue_leak", False),
                status=result.get("status", "new"),
            )

            status = result.get("status", "new")
            results["scored"] += 1
            if status == "scored":
                results["hot"] += 1
            elif status == "nurture":
                results["warm"] += 1
            elif status == "archived":
                results["archived"] += 1

            await log(
                f"✓ {contact.get('company','?')} — "
                f"Score: {result.get('opportunity_score',0)} → {status.upper()}"
            )

            await asyncio.sleep(0.5)  # rate limit

        except Exception as e:
            await log(f"✗ Error on {contact.get('website','?')}: {e}")
            results["errors"] += 1

    await log(
        f"Batch complete — {results['scored']} scored | "
        f"{results['hot']} hot | {results['warm']} warm | "
        f"{results['archived']} archived | {results['errors']} errors"
    )
    return results


if __name__ == "__main__":
    async def main():
        async def log(msg):
            print(msg)
        await run_batch_score(limit=10, log_cb=log)

    asyncio.run(main())
