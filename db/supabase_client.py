"""
Supabase database layer using direct REST API calls.
Avoids the Python client library which has host allowlist issues.
"""
import os
import httpx

def _headers():
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _url(table: str) -> str:
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    return f"{base}/rest/v1/{table}"


def upsert_contacts(contacts: list[dict]) -> int:
    if not contacts:
        return 0
    for c in contacts:
        if c.get("website"):
            c["website"] = c["website"].lower().strip().rstrip("/")
    headers = {**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    r = httpx.post(_url("contacts"), json=contacts, headers=headers, timeout=30)
    r.raise_for_status()
    return len(r.json() or [])


def get_contacts(status=None, industry=None, limit=500, offset=0, source_like=None, domains=None) -> list[dict]:
    params = {"limit": limit, "offset": offset, "order": "created_at.desc"}
    if status:
        params["status"] = f"eq.{status}"
    if industry:
        params["industry"] = f"ilike.%{industry}%"
    if source_like:
        params["source"] = f"like.{source_like}"
    if domains:
        ors = ",".join(f"website.ilike.*{d.strip()}*" for d in domains if d.strip())
        params["or"] = f"({ors})"
    r = httpx.get(_url("contacts"), params=params, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json() or []


def _exact_count(params: dict) -> int:
    """Asks Postgres for a TRUE row count via the count=exact header,
    without fetching row data. get_contact_stats was fetching up to
    10000 rows and counting them in Python with len(), but PostgREST
    silently caps any row-returning request at its own server-side
    limit (commonly 1000) regardless of what limit the caller asks
    for, that is the entire "Moneyball shows 1000" bug. count=exact
    sidesteps the cap entirely, Postgres computes the real total and
    hands it back in a response header."""
    params = {**params, "limit": "1"}
    headers = {**_headers(), "Prefer": "count=exact"}
    r = httpx.get(_url("contacts"), params=params, headers=headers, timeout=30)
    r.raise_for_status()
    content_range = r.headers.get("content-range", "")
    try:
        return int(content_range.split("/")[-1])
    except (ValueError, IndexError):
        return 0


def get_contact_stats() -> dict:
    stats = {"total": _exact_count({})}
    for s in ("new", "scored", "enriched", "report_sent", "nurture", "archived"):
        stats[s] = _exact_count({"status": f"eq.{s}"})
    for stage in ("discovered", "enriched", "scored", "loaded", "archived"):
        stats["stage_" + stage] = _exact_count({"pipeline_stage": f"eq.{stage}"})
    return stats


def update_contact_score(contact_id, opportunity_score, icp_score, website_score,
                          icp_tier, intel_pills, size_signals, revenue_leak, status, **extra):
    payload = {
        "opportunity_score": opportunity_score,
        "icp_score": icp_score,
        "website_score": website_score,
        "icp_tier": icp_tier,
        "intel_pills": intel_pills,
        "size_signals": size_signals,
        "revenue_leak": revenue_leak,
        "status": status,
        "scored_at": "now()",
        **extra,
    }
    r = httpx.patch(f"{_url('contacts')}?id=eq.{contact_id}", json=payload, headers=_headers(), timeout=30)
    r.raise_for_status()


def update_contact_status(contact_id, status, **extra):
    payload = {"status": status, **extra}
    r = httpx.patch(f"{_url('contacts')}?id=eq.{contact_id}", json=payload, headers=_headers(), timeout=30)
    r.raise_for_status()


def get_contacts_for_batch_score(limit=50, vertical=None, domains=None) -> list[dict]:
    """Pool for the batch scorer. vertical=None keeps the original behaviour
    (all status=new rows); vertical="roofers" restricts to that scrape source.
    domains: optional list of website domains to target specific leads."""
    source_like = f"google_maps:{vertical}*" if vertical else None
    return get_contacts(status="new", limit=limit, source_like=source_like, domains=domains)


def get_contacts_for_instantly(threshold=70, limit=100) -> list[dict]:
    params = {
        "status": "eq.enriched",
        "opportunity_score": f"gte.{threshold}",
        "limit": limit,
    }
    r = httpx.get(_url("contacts"), params=params, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json() or []

def get_contact_by_slug(slug: str) -> dict | None:
    """Fetch a single contact by its report_slug, all fields. Returns None if not found.
    Used to render reports live from current Supabase data (no stale snapshots)."""
    if not slug:
        return None
    params = {"report_slug": f"eq.{slug}", "select": "*", "limit": 1}
    r = httpx.get(_url("contacts"), params=params, headers=_headers(), timeout=30)
    r.raise_for_status()
    rows = r.json() or []
    return rows[0] if rows else None
