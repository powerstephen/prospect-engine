"""
PageSpeed Insights — real measured load time (mobile + desktop).
Falls back gracefully: if PSI fails or no key, returns {} and the
caller keeps using the httpx load_time estimate.
"""
import os
import asyncio
import httpx

PSI_KEY = os.environ.get("PSI_KEY", "")
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


async def _run(url: str, strategy: str) -> dict:
    params = {"url": url, "strategy": strategy, "category": "performance"}
    if PSI_KEY:
        params["key"] = PSI_KEY
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(PSI_URL, params=params)
        r.raise_for_status()
        data = r.json()
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    lcp_ms = audits.get("largest-contentful-paint", {}).get("numericValue")
    perf = lh.get("categories", {}).get("performance", {}).get("score")
    return {
        "lcp": round(lcp_ms / 1000, 1) if lcp_ms else None,
        "perf": round(perf * 100) if perf is not None else None,
    }


async def measure_speed(url: str) -> dict:
    """
    Returns real measured numbers when PSI succeeds, else {} so the
    caller falls back to the httpx estimate. Never raises.
    """
    if not url:
        return {}
    if not url.startswith("http"):
        url = "https://" + url
    if not PSI_KEY:
        return {}  # no key configured — caller uses estimate
    try:
        mobile, desktop = await asyncio.gather(
            _run(url, "mobile"),
            _run(url, "desktop"),
            return_exceptions=True,
        )
        out = {}
        if isinstance(mobile, dict):
            out["psi_mobile_lcp"] = mobile.get("lcp")
            out["psi_mobile_perf"] = mobile.get("perf")
        if isinstance(desktop, dict):
            out["psi_desktop_lcp"] = desktop.get("lcp")
            out["psi_desktop_perf"] = desktop.get("perf")
        return {k: v for k, v in out.items() if v is not None}
    except Exception:
        return {}
