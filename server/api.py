import asyncio
import csv
import io
import json
import uuid
from pathlib import Path
 
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
 
from config import SERPAPI_KEY
from scraper.engine import run_roaster, audit_url
from server.db import init_db, save_results, get_result, get_latest_session, get_session_results, save_result_by_name, get_result_by_slug, make_slug, cleanup_old_results
 
import json as _json2
 
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = ""  # loaded from env at runtime
 
app = FastAPI(title="Roaster Bot")
 
init_db()
 
_running = False
_task = None
_results: list[dict] = []
_buffer: list[dict] = []
_subscribers: list[asyncio.Queue] = []
_current_session: str = ""
 
 
def get_supabase_key():
    import os
    return os.environ.get("SUPABASE_SERVICE_KEY", "")
 
 
async def _broadcast(ev: dict):
    _buffer.append(ev)
    if len(_buffer) > 500: _buffer[:] = _buffer[-400:]
    for q in list(_subscribers):
        try: q.put_nowait(ev)
        except asyncio.QueueFull: pass
 
 
class RoastParams(BaseModel):
    industry: str = Field(min_length=2, max_length=100)
    location: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=20, ge=1, le=50)
 
class SingleParams(BaseModel):
    url: str
    name: str = ""
 
class BulkScoreParams(BaseModel):
    ids: list[int]
 
 
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((UI_DIR / "index.html").read_text(encoding="utf-8"))
 
@app.get("/leads", response_class=HTMLResponse)
async def leads():
    return HTMLResponse((UI_DIR / "leads.html").read_text(encoding="utf-8"))
 
 
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
            results = await run_roaster(params.industry, params.location, params.limit, SERPAPI_KEY, log_cb)
            _results = results
            save_results(_current_session, results)
            for r in results:
                save_result_by_name(r)
            cleanup_old_results(7)
            status = "completed"
        except Exception as e:
            results = []
            status = "error"
            await _broadcast({"type": "log", "msg": f"ERROR: {e}"})
        for r in results:
            r["slug"] = make_slug(r.get("name", "business"))
        await _broadcast({"type": "done", "status": status, "count": len(results), "results": results, "session_id": _current_session})
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
        session = get_latest_session()
        if session: results = get_session_results(session)
    if not results:
        raise HTTPException(404, "No results")
    buf = io.StringIO()
    fields = ["priority_score","name","category","address","phone","website","rating","reviews","biz_quality","opportunity_score","health_score","grade","load_time","is_ssl","critical_count","needs_work_count","speed","mobile","cta","trust","booking","social","seo","ssl_score","google_url"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        dims = r.get("dimensions", {})
        writer.writerow({"priority_score":r.get("priority_score",0),"name":r.get("name",""),"category":r.get("category",""),"address":r.get("address",""),"phone":r.get("phone",""),"website":r.get("website",""),"rating":r.get("rating",""),"reviews":r.get("reviews",""),"biz_quality":r.get("biz_quality",0),"opportunity_score":r.get("opportunity_score",0),"health_score":r.get("health_score",0),"grade":r.get("grade",""),"load_time":r.get("load_time",""),"is_ssl":r.get("is_ssl",""),"critical_count":r.get("critical_count",0),"needs_work_count":r.get("needs_work_count",0),"speed":dims.get("speed",{}).get("score",""),"mobile":dims.get("mobile",{}).get("score",""),"cta":dims.get("cta",{}).get("score",""),"trust":dims.get("trust",{}).get("score",""),"booking":dims.get("booking",{}).get("score",""),"social":dims.get("social",{}).get("score",""),"seo":dims.get("seo",{}).get("score",""),"ssl_score":dims.get("ssl",{}).get("score",""),"google_url":r.get("google_url","")})
    return StreamingResponse(io.BytesIO(buf.getvalue().encode()), media_type="text/csv", headers={"Content-Disposition":'attachment; filename="roaster_bot_results.csv"'})
 
 
@app.get("/report/{session_id}/{idx}", response_class=HTMLResponse)
async def report_by_session(session_id: str, idx: int):
    biz = get_result(session_id, idx)
    if not biz: raise HTTPException(404, "Result not found")
    return _render_report(biz)
 
 
@app.post("/api/roast/single/save")
async def save_single_roast(request: Request):
    data = await request.json()
    url = data.get("url", "")
    name = data.get("name", url)
    import re as _re
    slug = _re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')[:50]
    biz = {"name":name,"website":url,"address":"","phone":"","website_score":data.get("website_score",data.get("health_score",0)),"grade":data.get("grade",""),"critical_count":data.get("critical_count",0),"needs_work_count":data.get("needs_work_count",0),"load_time":data.get("load_time",""),"is_ssl":data.get("is_ssl",False),"dimensions":data.get("dimensions",{})}
    save_result_by_name(biz)
    return {"slug": slug, "report_url": f"/r/{slug}"}
 
 
@app.get("/r/{slug}", response_class=HTMLResponse)
async def report_by_name(slug: str):
    biz = get_result_by_slug(slug)
    if not biz: raise HTTPException(404, f"Report for '{slug}' not found or expired.")
    return _render_report(biz)
 
 
@app.get("/report/{idx}", response_class=HTMLResponse)
async def report(idx: int):
    if idx < len(_results):
        biz = _results[idx]
    else:
        session = get_latest_session()
        if not session: raise HTTPException(404, "No results found.")
        biz = get_result(session, idx)
        if not biz: raise HTTPException(404, f"Result {idx} not found.")
    return _render_report(biz)
 
 
def _render_report(biz: dict) -> HTMLResponse:
    report_html = (UI_DIR / "report.html").read_text(encoding="utf-8")
    data_js = f"window.REPORT_DATA = {_json2.dumps(biz)};"
    report_html = report_html.replace("const reportData=(typeof window.REPORT_DATA", f"{data_js}\nconst reportData=(typeof window.REPORT_DATA")
    return HTMLResponse(report_html)
 
 
# ── Contacts & Pipeline ───────────────────────────────────────────────────────
 
import csv as _csv
import io as _io
 
class ContactsUpload(BaseModel):
    contacts: list[dict]
 
@app.post("/api/contacts/upload")
async def upload_contacts(payload: ContactsUpload):
    try:
        from db.supabase_client import upsert_contacts
        count = upsert_contacts(payload.contacts)
        return {"inserted": count, "total": len(payload.contacts)}
    except Exception as e:
        raise HTTPException(500, f"Database error: {e}")
 
@app.get("/api/contacts")
async def list_contacts(status: str = None, industry: str = None, limit: int = 200, offset: int = 0):
    try:
        from db.supabase_client import get_contacts
        return get_contacts(status=status, industry=industry, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(500, f"Database error: {e}")
 
@app.get("/api/contacts/stats")
async def contact_stats():
    try:
        from db.supabase_client import get_contact_stats
        return get_contact_stats()
    except Exception as e:
        raise HTTPException(500, f"Database error: {e}")
 
@app.patch("/api/contacts/{contact_id}")
async def update_contact(contact_id: int, payload: dict):
    try:
        from db.supabase_client import update_contact_status
        update_contact_status(contact_id, payload.get("status"), **{k: v for k, v in payload.items() if k != "status"})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
@app.get("/api/contacts/export.csv")
async def export_contacts(status: str = None, industry: str = None):
    from db.supabase_client import get_contacts
    contacts = get_contacts(status=status, industry=industry, limit=5000)
    buf = _io.StringIO()
    fields = ["company","website","industry","location","first_name","last_name","email","phone","job_title","opportunity_score","icp_tier","status","report_url"]
    writer = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for c in contacts:
        writer.writerow(c)
    return StreamingResponse(_io.BytesIO(buf.getvalue().encode("utf-8")), media_type="text/csv", headers={"Content-Disposition":f'attachment; filename="contacts_{status or "all"}.csv"'})
 
 
# ── Batch & Bulk Scoring ──────────────────────────────────────────────────────
 
_batch_running = False
_batch_log = []
 
 
async def _write_ai_fields(contact_id: int, result: dict, log):
    """Write AI vision fields back to Supabase contacts table."""
    import httpx
    import os
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    ai_fields = {}
    for k in ["mobile_screenshot_url", "mobile_mockup_url",
              "desktop_screenshot_url", "desktop_mockup_url",
              "ai_mobile_score", "ai_visual_summary",
              "hero_broken", "cta_above_fold",
              "phone_above_fold", "ai_scored_at"]:
        if result.get(k) is not None:
            ai_fields[k] = result[k]
 
    if not ai_fields:
        return
 
    try:
        await log(f"  💾 Saving AI fields: {list(ai_fields.keys())}")
        h = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.patch(
                f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact_id}",
                json=ai_fields,
                headers=h,
            )
            if r.status_code not in (200, 201, 204):
                await log(f"  ✗ AI PATCH failed: {r.status_code} — {r.text[:200]}")
            else:
                await log(f"  ✓ AI fields saved")
    except Exception as e:
        await log(f"  ✗ AI fields error: {e}")
 
 
@app.post("/api/batch/score")
async def start_batch_score(background_tasks: BackgroundTasks, limit: int = 50):
    global _batch_running, _batch_log
    if _batch_running: raise HTTPException(409, "Batch already running")
    _batch_running = True
    _batch_log = []
    background_tasks.add_task(_run_batch_score, limit)
    return {"started": True, "limit": limit}
 
async def _run_batch_score(limit: int):
    global _batch_running, _batch_log
    try:
        from db.batch_scorer import run_batch_score
        async def log(msg): _batch_log.append(msg)
        await run_batch_score(limit=limit, log_cb=log)
    finally:
        _batch_running = False
 
 
@app.post("/api/bulk/score")
async def bulk_score_selected(params: BulkScoreParams, background_tasks: BackgroundTasks):
    global _batch_running, _batch_log
    if _batch_running: raise HTTPException(409, "Scorer already running")
    if not params.ids: raise HTTPException(400, "No IDs provided")
    _batch_running = True
    _batch_log = []
    background_tasks.add_task(_run_bulk_score, params.ids)
    return {"started": True, "count": len(params.ids)}
 
async def _run_bulk_score(ids: list[int]):
    global _batch_running, _batch_log
    async def log(msg): _batch_log.append(msg)
    try:
        from db.supabase_client import get_contacts, update_contact_score
        from db.batch_scorer import score_contact
        await log(f"Fetching {len(ids)} selected contacts...")
        all_contacts = get_contacts(limit=5000)
        contacts = [c for c in all_contacts if c["id"] in set(ids)]
        await log(f"Scoring {len(contacts)} contacts...")
        scored = 0
        errors = 0
        for contact in contacts:
            try:
                result = await score_contact(contact, log_cb=log)
                if result.get("error") and result.get("status") == "new":
                    errors += 1
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
                # Write AI vision fields
                await _write_ai_fields(contact["id"], result, log)
                scored += 1
                await asyncio.sleep(1)
            except Exception as e:
                await log(f"  ✗ Error: {e}")
                errors += 1
        await log(f"\n✓ Done — {scored} scored, {errors} errors")
    finally:
        _batch_running = False
 
 
@app.post("/api/bulk/find-website")
async def bulk_find_website(params: BulkScoreParams, background_tasks: BackgroundTasks):
    global _batch_running, _batch_log
    if _batch_running: raise HTTPException(409, "Already running")
    if not params.ids: raise HTTPException(400, "No IDs provided")
    if not SERPAPI_KEY: raise HTTPException(400, "SERPAPI_KEY not configured")
    _batch_running = True
    _batch_log = []
    background_tasks.add_task(_run_find_and_score, params.ids)
    return {"started": True, "count": len(params.ids)}
 
async def _run_find_and_score(ids: list[int]):
    global _batch_running, _batch_log
    async def log(msg): _batch_log.append(msg)
    try:
        from db.find_website import run_find_websites
        from db.supabase_client import get_contacts, update_contact_score
        from db.batch_scorer import score_contact
        await log("Step 1: Finding websites via Google Maps...")
        await run_find_websites(ids, SERPAPI_KEY, log_cb=log)
        await log("\nStep 2: Scoring contacts with websites...")
        all_contacts = get_contacts(limit=5000)
        contacts = [c for c in all_contacts if c["id"] in set(ids) and c.get("website")]
        scored = 0
        errors = 0
        for contact in contacts:
            try:
                result = await score_contact(contact, log_cb=log)
                if result.get("error") and result.get("status") == "new":
                    errors += 1
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
                await _write_ai_fields(contact["id"], result, log)
                scored += 1
                await asyncio.sleep(1)
            except Exception as e:
                await log(f"  ✗ Score error: {e}")
                errors += 1
        await log(f"\n✓ All done — {scored} scored, {errors} errors")
    finally:
        _batch_running = False
 
 
@app.post("/api/bulk/ai-vision")
async def bulk_ai_vision(params: BulkScoreParams, background_tasks: BackgroundTasks):
    global _batch_running, _batch_log
    if _batch_running: raise HTTPException(409, "Already running")
    if not params.ids: raise HTTPException(400, "No IDs provided")
    _batch_running = True
    _batch_log = []
    background_tasks.add_task(_run_ai_vision, params.ids)
    return {"started": True, "count": len(params.ids)}
 
async def _run_ai_vision(ids: list[int]):
    global _batch_running, _batch_log
    async def log(msg): _batch_log.append(msg)
    try:
        from db.ai_vision import run_bulk_ai_vision
        await run_bulk_ai_vision(ids, log_cb=log)
    finally:
        _batch_running = False
 
 
@app.get("/api/batch/status")
async def batch_status():
    return {"running": _batch_running, "log": _batch_log[-20:]}
 
@app.get("/api/batch/stream")
async def batch_stream(request: Request):
    async def gen():
        last = 0
        while _batch_running or last < len(_batch_log):
            if last < len(_batch_log):
                for msg in _batch_log[last:]:
                    yield {"event": "log", "data": json.dumps({"msg": msg})}
                last = len(_batch_log)
            if not _batch_running:
                yield {"event": "done", "data": "{}"}
                break
            await asyncio.sleep(0.5)
    return EventSourceResponse(gen())
 
 
 
 
@app.post("/api/bulk/enrich")
async def bulk_enrich(params: BulkScoreParams, background_tasks: BackgroundTasks):
    """Enrich selected contacts via QuickEnrich."""
    global _batch_running, _batch_log
    if _batch_running: raise HTTPException(409, "Already running")
    if not params.ids: raise HTTPException(400, "No IDs provided")
    _batch_running = True
    _batch_log = []
    background_tasks.add_task(_run_enrich, params.ids)
    return {"started": True, "count": len(params.ids)}
 
async def _run_enrich(ids: list[int]):
    global _batch_running, _batch_log
    async def log(msg): _batch_log.append(msg)
    try:
        from db.enrich import run_bulk_enrich
        await run_bulk_enrich(ids, log_cb=log)
    finally:
        _batch_running = False
 
 
 
# ── Harvest ───────────────────────────────────────────────────────────────────
 
_harvest_running = False
_harvest_log = []
_harvest_result = {}
 
 
class HarvestParams(BaseModel):
    industry: str = Field(min_length=2, max_length=100)
    location: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=20, ge=1, le=50)
 
 
@app.get("/find", response_class=HTMLResponse)
async def find_leads():
    return HTMLResponse((UI_DIR / "find.html").read_text(encoding="utf-8"))
 
 
@app.post("/api/harvest")
async def start_harvest(params: HarvestParams, background_tasks: BackgroundTasks):
    global _harvest_running, _harvest_log, _harvest_result
    if _harvest_running:
        raise HTTPException(409, "Harvest already running")
    _harvest_running = True
    _harvest_log = []
    _harvest_result = {}
    background_tasks.add_task(_run_harvest, params.industry, params.location, params.limit)
    return {"started": True}
 
 
async def _run_harvest(industry: str, location: str, limit: int):
    global _harvest_running, _harvest_log, _harvest_result
    try:
        from db.harvester import harvest
        async def log(msg):
            _harvest_log.append(msg)
        result = await harvest(industry, location, limit, log_cb=log)
        _harvest_result = result
    finally:
        _harvest_running = False
 
 
@app.get("/api/harvest/stream")
async def harvest_stream(request: Request):
    async def gen():
        last = 0
        while _harvest_running or last < len(_harvest_log):
            if last < len(_harvest_log):
                for msg in _harvest_log[last:]:
                    yield {"event": "log", "data": json.dumps({"msg": msg})}
                last = len(_harvest_log)
            if not _harvest_running:
                yield {"event": "done", "data": json.dumps({"result": _harvest_result})}
                break
            await asyncio.sleep(0.4)
    return EventSourceResponse(gen())
 
 
@app.get("/api/harvest/status")
async def harvest_status():
    return {"running": _harvest_running, "log": _harvest_log[-20:], "result": _harvest_result}
 
 
if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")
