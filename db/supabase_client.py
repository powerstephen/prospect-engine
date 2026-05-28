"""
Supabase database layer for Prospect Engine.
Handles all contact CRUD, status tracking, and batch operations.
"""
import os
from supabase import create_client, Client

_client: Client | None = None

def get_client() -> Client:
    global _client
    if not _client:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        _client = create_client(url, key)
    return _client


# ── Contacts ──────────────────────────────────────────────────────────────────

def upsert_contacts(contacts: list[dict]) -> int:
    """
    Insert or update contacts. Matches on website domain.
    Returns count inserted.
    """
    db = get_client()
    # Normalise domains for dedup
    for c in contacts:
        if c.get("website"):
            c["website"] = c["website"].lower().strip().rstrip("/")
    result = db.table("contacts").upsert(contacts, on_conflict="website").execute()
    return len(result.data or [])


def get_contacts(
    status: str | None = None,
    industry: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """Fetch contacts with optional filters."""
    db = get_client()
    q = db.table("contacts").select("*")
    if status:
        q = q.eq("status", status)
    if industry:
        q = q.ilike("industry", f"%{industry}%")
    q = q.order("created_at", desc=True).range(offset, offset + limit - 1)
    result = q.execute()
    return result.data or []


def get_contact_stats() -> dict:
    """Return counts by status for dashboard."""
    db = get_client()
    result = db.table("contacts").select("status").execute()
    rows = result.data or []
    stats = {
        "total": len(rows),
        "new": 0, "scored": 0, "enriched": 0,
        "report_sent": 0, "nurture": 0, "archived": 0,
    }
    for r in rows:
        s = r.get("status", "new")
        if s in stats:
            stats[s] += 1
    return stats


def update_contact_score(
    contact_id: int,
    opportunity_score: int,
    icp_score: int,
    website_score: int,
    icp_tier: str,
    intel_pills: list,
    size_signals: list,
    revenue_leak: bool,
    status: str,
) -> None:
    """Update scoring results for a single contact."""
    db = get_client()
    db.table("contacts").update({
        "opportunity_score": opportunity_score,
        "icp_score": icp_score,
        "website_score": website_score,
        "icp_tier": icp_tier,
        "intel_pills": intel_pills,
        "size_signals": size_signals,
        "revenue_leak": revenue_leak,
        "status": status,
        "scored_at": "now()",
    }).eq("id", contact_id).execute()


def update_contact_status(contact_id: int, status: str, **extra) -> None:
    """Update status and any extra fields."""
    db = get_client()
    payload = {"status": status, **extra}
    db.table("contacts").update(payload).eq("id", contact_id).execute()


def get_contacts_for_batch_score(limit: int = 50) -> list[dict]:
    """Get contacts ready to be scored (status = new)."""
    return get_contacts(status="new", limit=limit)


def get_contacts_for_instantly(threshold: int = 70, limit: int = 100) -> list[dict]:
    """Get scored contacts above threshold ready for Instantly push."""
    db = get_client()
    result = db.table("contacts") \
        .select("*") \
        .eq("status", "enriched") \
        .gte("opportunity_score", threshold) \
        .limit(limit) \
        .execute()
    return result.data or []
