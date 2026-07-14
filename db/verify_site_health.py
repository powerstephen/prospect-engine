"""
Site-Health Verifier v1 (Playwright)
Static HTML can see structure but not behaviour. This script loads each
FLAGGED site in a real browser (JavaScript running) and verifies:

  1. Dead CTAs: finds each flagged button by its text and CLICKS it.
     Observes: URL change, new tab, modal/dialog appearing, DOM growth.
     Verdict "dead" only when nothing observable happens.
  2. Broken images: after full load, counts images the BROWSER failed to
     render (naturalWidth == 0 on completed, visible images). Lazy-loaded
     placeholders resolve correctly here, so this is behaviour-true.

Writes: cta_verified_dead (bool), broken_images_verified (int),
        health_verified_detail (jsonb), health_verified_at (timestamptz)

Claims policy downstream: flat "doesn't work" claims may ONLY be made from
these verified fields, never from the static counts.

Usage:
  python db\\verify_site_health.py --limit 5      (test)
  python db\\verify_site_health.py --limit 100    (all flagged)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY
Requires: playwright (pip install playwright; playwright install chromium)
"""
import os
import sys
import json
import argparse

import httpx
from playwright.sync_api import sync_playwright

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

PAGE_TIMEOUT_MS = 30000
POST_CLICK_WAIT_MS = 2500


def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def fetch_flagged(limit: int) -> list:
    params = {
        "select": "id,company,website,dead_cta_count,broken_image_count,site_health_detail",
        "source": "like.google_maps:hvac*",
        "in_instantly": "eq.false",
        "first_name": "is.null",
        "email": "neq.",
        "health_verified_at": "is.null",
        "or": "(dead_cta_count.gt.0,broken_image_count.gt.0)",
        "limit": str(limit),
    }
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/contacts", params=params, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def flagged_cta_texts(contact: dict) -> list:
    detail = contact.get("site_health_detail") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            return []
    out = []
    for c in detail.get("dead_ctas") or []:
        t = (c.get("text") if isinstance(c, dict) else str(c)) or ""
        t = t.strip()
        if 2 < len(t) <= 50:
            out.append(t)
    return out[:3]


def page_state(page) -> dict:
    return page.evaluate("""() => ({
        url: location.href,
        bodyChildren: document.body ? document.body.querySelectorAll('*').length : 0,
        overlays: document.querySelectorAll(
            '[role=dialog],[class*=modal],[class*=popup],[class*=overlay],[id*=modal],iframe[src*=book],iframe[src*=schedul]'
        ).length,
    })""")


def verify_ctas(page, context, texts: list) -> list:
    results = []
    for text in texts:
        result = {"text": text, "verdict": "not_found"}
        try:
            locator = page.get_by_text(text, exact=False).first
            if locator.count() == 0:
                results.append(result)
                continue
            before = page_state(page)
            popup_holder = {}
            def on_page(p):
                popup_holder["new_tab"] = True
            context.on("page", on_page)
            try:
                locator.click(timeout=5000, force=True)
            except Exception as e:
                result["verdict"] = "click_failed"
                result["error"] = str(e)[:120]
                results.append(result)
                continue
            page.wait_for_timeout(POST_CLICK_WAIT_MS)
            after = page_state(page)
            alive = (
                popup_holder.get("new_tab")
                or after["url"] != before["url"]
                or after["overlays"] > before["overlays"]
                or after["bodyChildren"] > before["bodyChildren"] + 5
            )
            result["verdict"] = "alive" if alive else "dead"
            result["signals"] = {
                "url_changed": after["url"] != before["url"],
                "new_tab": bool(popup_holder.get("new_tab")),
                "overlay_appeared": after["overlays"] > before["overlays"],
                "dom_grew": after["bodyChildren"] - before["bodyChildren"],
            }
            # navigate back if the click took us somewhere, so next CTA test is clean
            if after["url"] != before["url"]:
                try:
                    page.go_back(timeout=10000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
        except Exception as e:
            result["verdict"] = "error"
            result["error"] = str(e)[:120]
        results.append(result)
    return results


def verify_images(page) -> dict:
    return page.evaluate("""() => {
        const imgs = Array.from(document.images);
        const broken = imgs.filter(i => {
            const r = i.getBoundingClientRect();
            const visible = r.width > 10 && r.height > 10;
            return i.complete && i.naturalWidth === 0 && visible;
        });
        return {
            total_images: imgs.length,
            broken_verified: broken.length,
            broken_srcs: broken.slice(0, 5).map(i => (i.currentSrc || i.src || '(no src)').slice(0, 120)),
        };
    }""")


def save(contact_id: int, fields: dict) -> bool:
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/contacts?id=eq.{contact_id}",
                    json=fields, headers=headers(), timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SAVE FAILED {contact_id}: {r.status_code} {r.text[:200]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    contacts = fetch_flagged(args.limit)
    if not contacts:
        print("No flagged contacts pending verification.")
        return
    print(f"Verifying {len(contacts)} flagged sites in a real browser...")

    stats = {"verified": 0, "cta_dead": 0, "cta_alive": 0, "img_broken_sites": 0, "errors": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for c in contacts:
            url = c.get("website") or ""
            if not url.startswith("http"):
                url = "https://" + url
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            try:
                page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)  # let lazy-load and widgets settle

                cta_results = verify_ctas(page, context, flagged_cta_texts(c)) if (c.get("dead_cta_count") or 0) > 0 else []
                img_result = verify_images(page)

                any_dead = any(r["verdict"] == "dead" for r in cta_results)
                any_alive = any(r["verdict"] == "alive" for r in cta_results)
                fields = {
                    "cta_verified_dead": any_dead,
                    "broken_images_verified": img_result["broken_verified"],
                    "health_verified_detail": {"ctas": cta_results, "images": img_result},
                    "health_verified_at": "now()",
                }
                if save(c["id"], fields):
                    stats["verified"] += 1
                    stats["cta_dead"] += int(any_dead)
                    stats["cta_alive"] += int(any_alive and not any_dead)
                    stats["img_broken_sites"] += int(img_result["broken_verified"] > 0)
                    cta_note = ", ".join(f"{r['text'][:20]}:{r['verdict']}" for r in cta_results) or "no ctas flagged"
                    print(f"  {c.get('company','?'):40.40s} imgs_broken={img_result['broken_verified']:3d} | {cta_note}")
            except Exception as e:
                stats["errors"] += 1
                print(f"  ERROR {c.get('company','?')}: {str(e)[:100]}")
                save(c["id"], {"health_verified_detail": {"error": str(e)[:200]},
                               "health_verified_at": "now()"})
            finally:
                context.close()
        browser.close()

    print(f"\nDone. verified={stats['verified']} cta_dead={stats['cta_dead']} "
          f"cta_alive={stats['cta_alive']} sites_with_broken_imgs={stats['img_broken_sites']} errors={stats['errors']}")


if __name__ == "__main__":
    main()
