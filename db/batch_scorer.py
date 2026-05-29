"""
Batch scorer — scores all 'new' contacts in background.
Reads from Supabase contacts table, runs website audit + ICP scoring,
then writes results back.
"""
import asyncio
import os
import sys
 
# Ensure project root is on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
 
from db.supabase_client import (
    get_contacts_for_batch_score,
    update_contact_score,
)
 
SCORE_THRESHOLD_HOT  = int(os.environ.get("SCORE_THRESHOLD_HOT",  "70"))
SCORE_THRESHOLD_WARM = int(os.environ.get("SCORE_THRESHOLD_WARM", "50"))
 
 
async def score_contact(contact: dict, log_cb=None) -> dict:
    """Score a single contact's website and return results."""
 
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass
 
    website = (contact.get("website") or "").strip()
    if not website:
        await log(f"  ↳ {contact.get('company','?')} — no website, skipping")
        return {"error": "no website", "status": "new"}
 
    # Ensure scheme
    if not website.startswith("http"):
        website = "https://" + website
 
    await log(f"Scoring {contact.get('company','?')} ({website})...")
 
    try:
        from scraper.engine import (
            audit_url,
            detect_size_signals,
            detect_intelligence_signals,
            calculate_icp_score,
        )
        import httpx
 
        # 1. Website audit
        audit = await audit_url(website)
 
        # 2. Size signals
        size_signals = await detect_size_signals(website, contact.get("company", ""))
        audit.update(size_signals)
 
        # 3. Intelligence signals (re-fetch HTML)
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                )
            }
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, verify=False) as c:
                r = await c.get(website, headers=headers)
                html = r.text
        except Exception:
            html = ""
 
        intel = await detect_intelligence_signals(html, audit.get("website_score", 0))
        audit.update(intel)
 
        # 4. ICP score
        icp = calculate_icp_score(audit, contact)
 
        # 5. Combined opportunity score
        combined = round(audit.get("website_score", 0) * 0.4 + icp["icp_score"] * 0.6)
 
        # 6. Auto-route
        if combined >= SCORE_THRESHOLD_HOT:
            status = "scored"
        elif combined >= SCORE_THRESHOLD_WARM:
            status = "nurture"
        else:
            status = "archived"
 
        await log(
            f"  ✓ {contact.get('company','?')} — "
            f"Opp: {combined} | ICP: {icp['icp_score']} | "
            f"Web: {audit.get('website_score',0)} | "
            f"Tier: {icp['icp_tier']} → {status.upper()}"
        )
 
        return {
            "opportunity_score": combined,
            "icp_score":         icp["icp_score"],
            "website_score":     audit.get("website_score", 0),
            "icp_tier":          icp["icp_tier"],
            "intel_pills":       icp.get("icp_pills", []),
            "size_signals":      audit.get("size_signals", []),
            "revenue_leak":      audit.get("revenue_leak", False),
            "status":            status,
        }
 
    except Exception as e:
        await log(f"  ✗ Error scoring {website}: {e}")
        return {"error": str(e), "status": "new"}
 
 
async def run_batch_score(limit: int = 50, log_cb=None) -> dict:
    """Score up to `limit` new contacts. Returns summary."""
 
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass
 
    contacts = get_contacts_for_batch_score(limit=limit)
 
    if not contacts:
        await log("No new contacts to score.")
        return {"scored": 0, "hot": 0, "warm": 0, "archived": 0, "errors": 0}
 
    await log(f"Starting batch score — {len(contacts)} contacts queued...")
    results = {"scored": 0, "hot": 0, "warm": 0, "archived": 0, "errors": 0}
 
    for contact in contacts:
        try:
            result = await score_contact(contact, log_cb=log_cb)
 
            if result.get("error") and result.get("status") == "new":
                results["errors"] += 1
                continue
 
            # Write back to Supabase
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
            if status == "scored":   results["hot"] += 1
            elif status == "nurture": results["warm"] += 1
            elif status == "archived": results["archived"] += 1
 
            # Polite rate limit
            await asyncio.sleep(0.5)
 
        except Exception as e:
            await log(f"  ✗ Unexpected error: {e}")
            results["errors"] += 1
 
    await log(
        f"\nBatch complete — "
        f"{results['scored']} scored | "
        f"{results['hot']} hot | "
        f"{results['warm']} warm | "
        f"{results['archived']} archived | "
        f"{results['errors']} errors"
    )
    return results
 
 
if __name__ == "__main__":
    async def _main():
        async def log(msg): print(msg)
        await run_batch_score(limit=5, log_cb=log)
    asyncio.run(_main())
 
