
"""
AI Vision Scorer — takes a mobile screenshot of a website and analyses it with GPT-4o Vision.
Returns structured scores for mobile experience, design quality, CTA visibility etc.
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
 
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
 
 
async def take_mobile_screenshot(url: str) -> bytes | None:
    """Take a 375px wide mobile screenshot using Playwright."""
    try:
        from playwright.async_api import async_playwright
 
        if not url.startswith("http"):
            url = "https://" + url
 
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                viewport={"width": 375, "height": 812},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                device_scale_factor=2,
            )
            page = await context.new_page()
 
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)  # Let JS render
            except Exception:
                try:
                    await page.goto(url, wait_until="commit", timeout=10000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    await browser.close()
                    return None
 
            screenshot = await page.screenshot(
                full_page=False,  # Just the viewport — what user sees first
                type="jpeg",
                quality=75,
            )
            await browser.close()
            return screenshot
 
    except Exception as e:
        print(f"Screenshot error: {e}")
        return None
 
 
async def analyse_screenshot_with_gpt4v(screenshot_bytes: bytes, company: str, url: str) -> dict:
    """Send screenshot to GPT-4o Vision and get structured analysis."""
    try:
        from openai import AsyncOpenAI
 
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return {"error": "No OpenAI API key"}
 
        client = AsyncOpenAI(api_key=api_key)
 
        # Encode screenshot as base64
        image_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
 
        prompt = """You are a web design expert analysing a mobile screenshot of a roofing company website.
This is what their customers see on a phone. Be brutally honest.
 
Analyse and return ONLY a JSON object with these exact fields:
 
{
  "ai_mobile_score": <integer 1-10, 10=perfect mobile experience>,
  "hero_broken": <true if hero image is missing/broken/not loading, false otherwise>,
  "cta_above_fold": <true if there is a clear call-to-action button visible without scrolling>,
  "phone_above_fold": <true if a phone number is visible without scrolling>,
  "design_quality": <integer 1-10, 10=professional design>,
  "images_rendering": <true if images are displaying correctly, false if broken/overflowing>,
  "biggest_problem": <one sentence, the single worst issue visible>,
  "ai_visual_summary": <2-3 sentences describing what a customer sees and the main opportunities for improvement>,
  "urgency": <"critical", "high", "medium", or "low">
}
 
Be specific about what you see. If images are overflowing the screen or broken, flag it. If the CTA is below the fold, flag it. If the design looks amateur or outdated, say so."""
 
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                                "detail": "high"
                            }
                        },
                        {
                            "type": "text",
                            "text": f"Company: {company}\nURL: {url}\n\n{prompt}"
                        }
                    ]
                }
            ],
            max_tokens=500,
            temperature=0.1,
        )
 
        raw = response.choices[0].message.content.strip()
 
        # Strip markdown if present
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
 
        result = json.loads(raw)
        return result
 
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"error": str(e)}
 
 
async def run_ai_vision_score(contact: dict, log_cb=None) -> dict:
    """
    Full AI vision pipeline for a single contact.
    Takes screenshot, analyses with GPT-4o, returns structured results.
    """
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass
 
    website = (contact.get("website") or "").strip()
    company = contact.get("company", "Unknown")
 
    if not website:
        await log(f"  ↳ {company} — no website, skipping AI vision")
        return {"error": "no website"}
 
    if not website.startswith("http"):
        website = "https://" + website
 
    await log(f"  📸 Screenshotting {company} at 375px mobile...")
 
    screenshot = await take_mobile_screenshot(website)
    if not screenshot:
        await log(f"  ✗ Could not screenshot {website}")
        return {"error": "screenshot failed"}
 
    await log(f"  🤖 Analysing with GPT-4o Vision...")
 
    result = await analyse_screenshot_with_gpt4v(screenshot, company, website)
 
    if "error" in result:
        await log(f"  ✗ Vision analysis failed: {result['error']}")
        return result
 
    await log(
        f"  ✓ {company} — Mobile: {result.get('ai_mobile_score')}/10 | "
        f"Design: {result.get('design_quality')}/10 | "
        f"Urgency: {result.get('urgency')} | "
        f"Problem: {result.get('biggest_problem', '—')}"
    )
 
    return result
 
 
async def run_bulk_ai_vision(ids: list[int], log_cb=None) -> dict:
    """Run AI vision analysis on a list of contact IDs."""
 
    async def log(msg):
        if log_cb:
            try:
                await log_cb(msg)
            except Exception:
                pass
 
    import httpx as _httpx
 
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
 
    from db.supabase_client import get_contacts
    all_contacts = get_contacts(limit=5000)
    id_set   = set(ids)
    contacts = [c for c in all_contacts if c["id"] in id_set and c.get("website")]
 
    await log(f"Running AI vision on {len(contacts)} contacts...")
 
    scored = 0
    errors = 0
 
    for contact in contacts:
        result = await run_ai_vision_score(contact, log_cb=log)
 
        if "error" in result:
            errors += 1
            continue
 
        # Write back to Supabase
        try:
            from datetime import datetime, timezone
            payload = {
                "ai_mobile_score":   result.get("ai_mobile_score"),
                "ai_visual_summary": result.get("ai_visual_summary"),
                "hero_broken":       result.get("hero_broken", False),
                "cta_above_fold":    result.get("cta_above_fold", False),
                "phone_above_fold":  result.get("phone_above_fold", False),
                "ai_scored_at":      datetime.now(timezone.utc).isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10) as c:
                r = await c.patch(
                    f"{supabase_url}/rest/v1/contacts?id=eq.{contact['id']}",
                    json=payload,
                    headers=headers,
                )
                r.raise_for_status()
            scored += 1
        except Exception as e:
            await log(f"  ✗ Failed to save: {e}")
            errors += 1
 
        # Rate limit — GPT-4o vision calls cost more
        await asyncio.sleep(2)
 
    await log(f"\n✓ AI Vision complete — {scored} analysed, {errors} errors")
    return {"scored": scored, "errors": errors}
 
