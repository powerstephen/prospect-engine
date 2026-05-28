import asyncio
import csv
import io
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from config import SERPAPI_KEY
from scraper.engine import run_roaster, audit_url
from server.db import init_db, save_results, get_result, get_latest_session, get_session_results, save_result_by_name, get_result_by_slug, make_slug, cleanup_old_results

import json as _json2

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

app = FastAPI(title="Roaster Bot")

# Init DB on startup
init_db()

# ── In-memory state for streaming ─────────────────────────────────────────────
_running = False
_task = None
_results: list[dict] = []
_buffer: list[dict] = []
_subscribers: list[asyncio.Queue] = []
_current_session: str = ""


async def _broadcast(ev: dict):
    _buffer.append(ev)
    if len(_buffer) > 500: _buffer[:] = _buffer[-400:]
    for q in list(_subscribers):
        try: q.put_nowait(ev)
        except asyncio.QueueFull: pass


# ── Models ────────────────────────────────────────────────────────────────────
class RoastParams(BaseModel):
    industry: str = Field(min_length=2, max_length=100)
    location: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=20, ge=1, le=50)


class SingleParams(BaseModel):
    url: str
    name: str = ""


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((UI_DIR / "index.html").read_text(encoding="utf-8"))


@app.post("/api/roast")
async def start_roast(params: RoastParams):
    global _running, _task, _results, _buffer, _current_session

    if _running:
        raise HTTPException(409, "Already running")
    if not SERPAPI_KEY:
        raise HTTPException(400, "SERPAPI_KEY not configured")

    _running = True
    _results = []
    _buffer = []
    _current_session = str(uuid.uuid4())

    async def _job():
        global _running, _results

        async def log_cb(msg):
            await _broadcast({"type": "log", "msg": msg})

        try:
            results = await run_roaster(
                params.industry, params.location, params.limit,
                SERPAPI_KEY, log_cb
            )
            _results = results
            # Save to SQLite by session and by company name
            save_results(_current_session, results)
            for r in results:
                save_result_by_name(r)
            cleanup_old_results(7)
            status = "completed"
        except Exception as e:
            results = []
            status = "error"
            await _broadcast({"type": "log", "msg": f"ERROR: {e}"})

        # Add slug to each result for named URLs
        for r in results:
            r["slug"] = make_slug(r.get("name", "business"))
        await _broadcast({
            "type": "done",
            "status": status,
            "count": len(results),
            "results": results,
            "session_id": _current_session,
        })
        _running = False

    _task = asyncio.create_task(_job())
    return {"ok": True, "session_id": _current_session}


@app.post("/api/roast/single")
async def single_roast(params: SingleParams):
    result = await audit_url(params.url)
    return {**result, "name": params.name, "url": params.url}


@app.get("/api/stream")
async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    for ev in _buffer:
        try: q.put_nowait(ev)
        except: pass
    _subscribers.append(q)

    async def gen():
        try:
            while True:
                if await request.is_disconnected(): break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield {"event": ev["type"], "data": json.dumps(ev)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if q in _subscribers: _subscribers.remove(q)

    return EventSourceResponse(gen())


@app.get("/api/status")
async def status():
    return {"running": _running, "count": len(_results)}


@app.get("/api/results")
async def get_results():
    return _results


@app.get("/api/export.csv")
async def export_csv():
    results = _results
    if not results:
        # Try latest session from DB
        session = get_latest_session()
        if session:
            results = get_session_results(session)
    if not results:
        raise HTTPException(404, "No results")

    buf = io.StringIO()
    fields = [
        "priority_score", "name", "category", "address", "phone", "website",
        "rating", "reviews", "biz_quality",
        "opportunity_score", "health_score", "grade",
        "load_time", "is_ssl", "critical_count", "needs_work_count",
        "speed", "mobile", "cta", "trust", "booking", "social", "seo", "ssl_score",
        "google_url",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()

    for r in results:
        dims = r.get("dimensions", {})
        writer.writerow({
            "priority_score": r.get("priority_score", 0),
            "name": r.get("name", ""),
            "category": r.get("category", ""),
            "address": r.get("address", ""),
            "phone": r.get("phone", ""),
            "website": r.get("website", ""),
            "rating": r.get("rating", ""),
            "reviews": r.get("reviews", ""),
            "biz_quality": r.get("biz_quality", 0),
            "opportunity_score": r.get("opportunity_score", 0),
            "health_score": r.get("health_score", 0),
            "grade": r.get("grade", ""),
            "load_time": r.get("load_time", ""),
            "is_ssl": r.get("is_ssl", ""),
            "critical_count": r.get("critical_count", 0),
            "needs_work_count": r.get("needs_work_count", 0),
            "speed": dims.get("speed", {}).get("score", ""),
            "mobile": dims.get("mobile", {}).get("score", ""),
            "cta": dims.get("cta", {}).get("score", ""),
            "trust": dims.get("trust", {}).get("score", ""),
            "booking": dims.get("booking", {}).get("score", ""),
            "social": dims.get("social", {}).get("score", ""),
            "seo": dims.get("seo", {}).get("score", ""),
            "ssl_score": dims.get("ssl", {}).get("score", ""),
            "google_url": r.get("google_url", ""),
        })

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="roaster_bot_results.csv"'}
    )


@app.get("/report/{session_id}/{idx}", response_class=HTMLResponse)
async def report_by_session(session_id: str, idx: int):
    biz = get_result(session_id, idx)
    if not biz:
        raise HTTPException(404, "Result not found")
    return _render_report(biz)




@app.post("/api/roast/single/save")
async def save_single_roast(request: Request):
    """Save a single URL audit result so it can be viewed as a full report."""
    data = await request.json()
    url = data.get("url", "")
    name = data.get("name", url)
    
    import re as _re
    slug = _re.sub(r'[^a-z0-9]+', '-', name.lower().strip())
    slug = slug.strip('-')[:50]
    
    # Build a proper result object
    biz = {
        "name": name,
        "website": url,
        "address": "",
        "phone": "",
        "website_score": data.get("website_score", data.get("health_score", 0)),
        "grade": data.get("grade", ""),
        "critical_count": data.get("critical_count", 0),
        "needs_work_count": data.get("needs_work_count", 0),
        "load_time": data.get("load_time", ""),
        "is_ssl": data.get("is_ssl", False),
        "dimensions": data.get("dimensions", {}),
    }
    
    save_result_by_name(biz)
    return {"slug": slug, "report_url": f"/r/{slug}"}

@app.get("/r/{slug}", response_class=HTMLResponse)
async def report_by_name(slug: str):
    biz = get_result_by_slug(slug)
    if not biz:
        raise HTTPException(404, f"Report for '{slug}' not found or expired.")
    return _render_report(biz)

@app.get("/report/{idx}", response_class=HTMLResponse)
async def report(idx: int):
    # Try in-memory first
    if idx < len(_results):
        biz = _results[idx]
    else:
        # Fall back to latest session in DB
        session = get_latest_session()
        if not session:
            raise HTTPException(404, "No results found. Run a search first.")
        biz = get_result(session, idx)
        if not biz:
            raise HTTPException(404, f"Result {idx} not found. Run a search first.")
    return _render_report(biz)


def _render_report(biz: dict) -> HTMLResponse:
    report_html = (UI_DIR / "report.html").read_text(encoding="utf-8")
    # Inject data directly before populate() call
    data_js = f"window.REPORT_DATA = {_json2.dumps(biz)};"
    report_html = report_html.replace(
        "const reportData=(typeof window.REPORT_DATA",
        f"{data_js}\nconst reportData=(typeof window.REPORT_DATA"
    )
    return HTMLResponse(report_html)


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")
