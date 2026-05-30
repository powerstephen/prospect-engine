"""
Batch scorer v3 — full pipeline:
1. Website audit (HTML, mobile, tech stack)
2. Size + intelligence signals  
3. ICP scoring
4. AI Vision — Playwright screenshot + GPT-4o analysis
5. Phone mockup composite + upload to Supabase Storage
6. All results written back to Supabase in one shot
"""
import asyncio
import base64
import os
import sys
from datetime import datetime, timezone
 
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
 
from db.supabase_client import get_contacts_for_batch_score, update_contact_score
 
SCORE_THRESHOLD_HOT  = int(os.environ.get("SCORE_THRESHOLD_HOT",  "50"))
SCORE_THRESHOLD_WARM = int(os.environ.get("SCORE_THRESHOLD_WARM", "30"))
SUPABASE_URL = "https://neonmrgszujadgfidlbj.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = "roast-screenshots"
 
 
async def upload_screenshot(contact_id: int, image_bytes: bytes, suffix: str = "mobile") -> str | None:
    """Upload screenshot to Supabase Storage and return public URL."""
    try:
        import httpx
        filename = f"{contact_id}_{suffix}.jpg"
        upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/jpeg",
            "x-upsert": "true",
        }
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(upload_url, content=image_bytes, headers=headers)
            if r.status_code not in (200, 201):
                print(f"Upload failed {suffix}: {r.status_code} — {r.text[:200]}")
                return None
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{filename}"
        print(f"Upload success {suffix}: {public_url}")
        return public_url
    except Exception as e:
        print(f"Upload error {suffix}: {e}")
        return None
 
 
async def create_phone_mockup(screenshot_bytes: bytes) -> bytes | None:
    """Composite screenshot into a phone frame using PIL."""
    try:
        from PIL import Image, ImageDraw
        import io
 
        screen = Image.open(io.BytesIO(screenshot_bytes)).convert("RGBA")
        sw, sh = screen.size
 
        pad_top = 80
        pad_bottom = 80
        pad_side = 30
        corner_r = 40
 
        frame_w = sw + pad_side * 2
        frame_h = sh + pad_top + pad_bottom
 
        phone = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(phone)
 
        draw.rounded_rectangle(
            [(0, 0), (frame_w - 1, frame_h - 1)],
            radius=corner_r,
            fill=(30, 30, 35, 255),
            outline=(60, 60, 65, 255),
            width=2,
        )
        draw.rectangle(
            [(pad_side, pad_top), (pad_side + sw - 1, pad_top + sh - 1)],
            fill=(0, 0, 0, 255),
        )
        grille_w = 60
        grille_x = (frame_w - grille_w) // 2
        draw.rounded_rectangle(
            [(grille_x, 28), (grille_x + grille_w, 36)],
            radius=4, fill=(60, 60, 65, 255),
        )
        ind_w = 100
        ind_x = (frame_w - ind_w) // 2
        draw.rounded_rectangle(
            [(ind_x, frame_h - 28), (ind_x + ind_w, frame_h - 22)],
            radius=3, fill=(80, 80, 85, 255),
        )
        phone.paste(screen.convert("RGBA"), (pad_side, pad_top))
 
        out = Image.new("RGB", (frame_w, frame_h), (245, 245, 247))
        out.paste(phone, mask=phone.split()[3])
 
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
 
    except ImportError:
        return screenshot_bytes
    except Exception as e:
        print(f"Mockup error: {e}")
        return screenshot_bytes
 
 
async def run_ai_vision(contact: dict, log_cb=None) -> dict:
    """Screenshot + GPT-4o Vision + mockup + upload to Storage."""
 
    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass
 
    website = (contact.get("website") or "").strip()
    if not website:
        return {}
    if not website.startswith("http"):
        website = "https://" + website
 
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        await log("  ↳ No OpenAI key — skipping AI vision")
        return {}
 
    # ── Screenshot ──
    await log(f"  📸 Screenshotting {contact.get('company','?')} at 375px mobile...")
    screenshot = None
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(
                viewport={"width": 375, "height": 812},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                device_scale_factor=2,
            )
            page = await ctx.new_page()
            try:
                await page.goto(website, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
            except Exception:
                try:
                    await page.goto(website, wait_until="commit", timeout=10000)
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    await log(f"  ✗ Page load failed: {e}")
                    await browser.close()
                    return {}
            screenshot = await page.screenshot(full_page=False, type="jpeg", quality=75)
            await browser.close()
            await log(f"  ✓ Screenshot taken ({len(screenshot)} bytes)")
    except Exception as e:
        await log(f"  ✗ Screenshot failed: {e}")
        return {}
 
    if not screenshot:
        return {}
 
    # ── GPT-4o Vision ──
    await log(f"  🤖 Analysing with GPT-4o Vision...")
    vision_result = {}
    try:
        from openai import AsyncOpenAI
        import json
 
        client = AsyncOpenAI(api_key=openai_key)
        image_b64 = base64.b64encode(screenshot).decode("utf-8")
 
        prompt = """You are a web design expert analysing a mobile screenshot of a roofing company website.
Be brutally honest. Return ONLY valid JSON with these exact fields:
{
  "ai_mobile_score": <1-10, 10=perfect mobile experience>,
  "hero_broken": <true if hero image missing or broken or white screen>,
  "cta_above_fold": <true if clear CTA button visible without scrolling>,
  "phone_above_fold": <true if phone number visible without scrolling>,
  "design_quality": <1-10>,
  "images_rendering": <true if images display correctly>,
  "biggest_problem": <one sentence worst visible issue>,
  "ai_visual_summary": <2-3 sentences: what customer sees + main improvements needed>,
  "urgency": <"critical", "high", "medium", or "low">
}"""
 
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}", "detail": "high"}},
                    {"type": "text", "text": f"Company: {contact.get('company','')}\nURL: {website}\n\n{prompt}"}
                ]
            }],
            max_tokens=500,
            temperature=0.1,
        )
 
        raw = response.choices[0].message.content.strip()
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
 
        vision_result = json.loads(raw)
        await log(
            f"  ✓ AI Vision — Mobile: {vision_result.get('ai_mobile_score')}/10 | "
            f"Design: {vision_result.get('design_quality')}/10 | "
            f"Urgency: {vision_result.get('urgency')} | "
            f"{vision_result.get('biggest_problem','')}"
        )
    except Exception as e:
        await log(f"  ✗ GPT-4o failed: {e}")
 
    # ── Mockup ──
    await log(f"  📱 Creating phone mockup...")
    mockup = await create_phone_mockup(screenshot)
    await log(f"  ✓ Mockup created ({len(mockup) if mockup else 0} bytes)")
 
    # ── Upload to Supabase Storage ──
    contact_id = contact.get("id")
    screenshot_url = None
    mockup_url = None
 
    if contact_id:
        await log(f"  ☁ Uploading screenshots to Supabase Storage...")
        screenshot_url = await upload_screenshot(contact_id, screenshot, "mobile")
        if mockup:
            mockup_url = await upload_screenshot(contact_id, mockup, "mockup")
        await log(f"  Screenshot URL: {screenshot_url}")
        await log(f"  Mockup URL: {mockup_url}")
 
    return {
        **vision_result,
        "mobile_screenshot_url": screenshot_url,
        "mobile_mockup_url": mockup_url,
    }
 
 
async def score_contact(contact: dict, log_cb=None) -> dict:
    """Full scoring pipeline — website audit + AI vision."""
 
    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass
 
    website = (contact.get("website") or "").strip()
    if not website:
        await log(f"  ↳ {contact.get('company','?')} — no website, skipping")
        return {"error": "no website", "status": "new"}
 
    if not website.startswith("http"):
        website = "https://" + website
 
    await log(f"Scoring {contact.get('company','?')} ({website})...")
 
    try:
        from scraper.engine import audit_url, detect_size_signals, detect_intelligence_signals, calculate_icp_score
        import httpx
 
        # 1. Website audit
        audit = await audit_url(website)
 
        # 2. Size signals
        size_signals = await detect_size_signals(website, contact.get("company", ""))
        audit.update(size_signals)
 
        # 3. Intelligence signals
        try:
            headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, verify=False) as c:
                r = await c.get(website, headers=headers)
                html = r.text
        except Exception:
            html = ""
 
        intel = await detect_intelligence_signals(html, audit.get("website_score", 0))
        audit.update(intel)
        audit["mobile_issues"] = audit.get("dimensions", {}).get("mobile", {}).get("mobile_issues", [])
 
        # 4. ICP score
        icp = calculate_icp_score(audit, contact)
        combined = icp.get("combined_score", icp["icp_score"])
 
        # 5. AI Vision
        vision = await run_ai_vision(contact, log_cb=log)
 
        # 6. Boost from AI findings
        ai_boost = 0
        if vision.get("ai_mobile_score"):
            ai_ms = vision["ai_mobile_score"]
            if ai_ms <= 3:   ai_boost += 10
            elif ai_ms <= 5: ai_boost += 6
            elif ai_ms <= 7: ai_boost += 3
        if vision.get("hero_broken"):          ai_boost += 5
        if not vision.get("cta_above_fold"):   ai_boost += 4
        if not vision.get("phone_above_fold"): ai_boost += 3
 
        combined = min(99, combined + ai_boost)
 
        # 7. Auto-route
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
            f"AI Mobile: {vision.get('ai_mobile_score','—')}/10 | "
            f"Tier: {icp['icp_tier']} → {status.upper()}"
        )
 
        return {
            "opportunity_score":     combined,
            "icp_score":             icp["icp_score"],
            "website_score":         audit.get("website_score", 0),
            "icp_tier":              icp["icp_tier"],
            "intel_pills":           icp.get("icp_pills", []),
            "size_signals":          audit.get("size_signals", []),
            "revenue_leak":          audit.get("revenue_leak", False),
            "status":                status,
            "mobile_screenshot_url": vision.get("mobile_screenshot_url"),
            "mobile_mockup_url":     vision.get("mobile_mockup_url"),
            "ai_mobile_score":       vision.get("ai_mobile_score"),
            "ai_visual_summary":     vision.get("ai_visual_summary"),
            "hero_broken":           vision.get("hero_broken", False),
            "cta_above_fold":        vision.get("cta_above_fold", False),
            "phone_above_fold":      vision.get("phone_above_fold", False),
            "ai_scored_at":          datetime.now(timezone.utc).isoformat(),
        }
 
    except Exception as e:
        await log(f"  ✗ Error scoring {website}: {e}")
        return {"error": str(e), "status": "new"}
 
 
async def run_batch_score(limit: int = 50, log_cb=None) -> dict:
    """Score up to `limit` new contacts."""
 
    async def log(msg):
        if log_cb:
            try: await log_cb(msg)
            except Exception: pass
 
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
 
            # Write core scores
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
            ai_fields = {}
            for k in ["mobile_screenshot_url", "mobile_mockup_url", "ai_mobile_score",
                      "ai_visual_summary", "hero_broken", "cta_above_fold",
                      "phone_above_fold", "ai_scored_at"]:
                if result.get(k) is not None:
                    ai_fields[k] = result[k]
 
            if ai_fields:
                try:
                    await log(f"  💾 Saving AI fields to Supabase: {list(ai_fields.keys())}")
                    import httpx
                    h = {
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    }
                    async with httpx.AsyncClient(timeout=10) as c:
                        r = await c.patch(
                            f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact['id']}",
                            json=ai_fields,
                            headers=h,
                        )
                        if r.status_code not in (200, 201, 204):
                            await log(f"  ✗ Supabase PATCH failed: {r.status_code} — {r.text[:200]}")
                        else:
                            await log(f"  ✓ AI fields saved successfully")
                except Exception as e:
                    await log(f"  ✗ AI fields save error: {e}")
 
            status = result.get("status", "new")
            results["scored"] += 1
            if status == "scored":     results["hot"] += 1
            elif status == "nurture":  results["warm"] += 1
            elif status == "archived": results["archived"] += 1
 
            await asyncio.sleep(1)
 
        except Exception as e:
            await log(f"  ✗ Unexpected error: {e}")
            results["errors"] += 1
 
    await log(
        f"\nBatch complete — {results['scored']} scored | "
        f"{results['hot']} hot | {results['warm']} warm | "
        f"{results['archived']} archived | {results['errors']} errors"
    )
    return results
 
 
if __name__ == "__main__":
    async def _main():
        async def log(msg): print(msg)
        await run_batch_score(limit=2, log_cb=log)
    asyncio.run(_main())
 
