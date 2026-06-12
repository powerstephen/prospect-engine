"""
PageSpeed Insights - real measured load time (mobile + desktop).
Falls back gracefully: if PSI fails or no key, returns {} and the
caller keeps using the httpx load_time estimate.
"""
import os
import asyncio
import statistics
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
    fcp_ms = audits.get("first-contentful-paint", {}).get("numericValue")
    perf = lh.get("categories", {}).get("performance", {}).get("score")
    return {
        "lcp": round(lcp_ms / 1000, 1) if lcp_ms else None,
        "fcp": round(fcp_ms / 1000, 1) if fcp_ms else None,
        "perf": round(perf * 100) if perf is not None else None,
    }


async def _run_with_retry(url: str, strategy: str, attempts: int = 2) -> dict:
    """Run a PSI audit, retrying once on failure. Returns {} if all attempts fail.
    Mobile audits time out / rate-limit more often than desktop, so a single
    retry recovers most transient failures."""
    last_exc = None
    for i in range(attempts):
        try:
            return await _run(url, strategy)
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                await asyncio.sleep(2)  # brief backoff before retry
    return {}


async def _run_median(url: str, strategy: str, runs: int = 3) -> dict:
    """Run PSI several times and return the median of each metric.
    Smooths out PSI run-to-run variance (LCP especially can swing a lot).
    Medians each metric independently over whatever runs succeeded; if all
    runs fail, returns an empty dict so the caller falls back gracefully."""
    lcps, fcps, perfs = [], [], []
    for i in range(runs):
        res = await _run_with_retry(url, strategy)
        if res.get("lcp") is not None:  lcps.append(res["lcp"])
        if res.get("fcp") is not None:  fcps.append(res["fcp"])
        if res.get("perf") is not None: perfs.append(res["perf"])
        if i < runs - 1:
            await asyncio.sleep(1)  # brief gap between runs
    return {
        "lcp":  round(statistics.median(lcps), 1) if lcps else None,
        "fcp":  round(statistics.median(fcps), 1) if fcps else None,
        "perf": round(statistics.median(perfs)) if perfs else None,
    }

async def measure_speed(url: str) -> dict:
    """
    Returns real measured numbers when PSI succeeds, else {} so the
    caller falls back to the httpx estimate. Never raises.
    Mobile and desktop are run sequentially (mobile first, as the priority
    metric) with a retry each, which is far more reliable than firing both
    at once - that tended to leave mobile null while desktop succeeded.
    """
    if not url:
        return {}
    if not url.startswith("http"):
        url = "https://" + url
    if not PSI_KEY:
        return {}
    try:
        mobile = await _run_median(url, "mobile")
        desktop = await _run_median(url, "desktop")
        out = {}
        if isinstance(mobile, dict):
            out["psi_mobile_lcp"] = mobile.get("lcp")
            out["psi_mobile_fcp"] = mobile.get("fcp")
            out["psi_mobile_perf"] = mobile.get("perf")
        if isinstance(desktop, dict):
            out["psi_desktop_lcp"] = desktop.get("lcp")
            out["psi_desktop_fcp"] = desktop.get("fcp")
            out["psi_desktop_perf"] = desktop.get("perf")
        return {k: v for k, v in out.items() if v is not None}
    except Exception:
        return {}
