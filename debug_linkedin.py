"""
Run this to dump the actual HTML of the LinkedIn Easy Apply modal
so we can see the real input selectors.
"""
import asyncio
import json
from pathlib import Path

async def dump_modal_html(job_url: str):
    from playwright.async_api import async_playwright

    cookies_file = Path("linkedin_cookies.json")
    if not cookies_file.exists():
        print("No cookies found")
        return

    cookies = json.loads(cookies_file.read_text())

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        print("Loading job page...")
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # Click Easy Apply
        for sel in ["button:has-text('Easy Apply')", "button[aria-label*='Easy Apply']"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    print("Clicked Easy Apply")
                    break
            except Exception:
                continue

        await page.wait_for_timeout(2000)

        # Get modal HTML
        modal = page.locator("[data-test-modal], .jobs-easy-apply-modal, [role='dialog']").first
        if await modal.count() > 0:
            html = await modal.inner_html()
        else:
            html = await page.content()

        # Save full HTML
        Path("output").mkdir(exist_ok=True)
        Path("output/modal_debug.html").write_text(html, encoding="utf-8")
        print(f"HTML saved to output/modal_debug.html ({len(html)} chars)")

        # Print all inputs with their attributes
        print("\n=== ALL INPUTS IN MODAL ===")
        inputs = await page.locator("[role='dialog'] input, [role='dialog'] select, [role='dialog'] textarea").all()
        for inp in inputs:
            try:
                tag  = await inp.evaluate("el => el.tagName.toLowerCase()")
                name = await inp.get_attribute("name") or ""
                id_  = await inp.get_attribute("id") or ""
                type_= await inp.get_attribute("type") or ""
                aria = await inp.get_attribute("aria-label") or ""
                ph   = await inp.get_attribute("placeholder") or ""
                val  = await inp.input_value() if tag != "select" else ""
                print(f"  <{tag}> name='{name}' id='{id_}' type='{type_}' "
                      f"aria-label='{aria}' placeholder='{ph}' value='{val[:30]}'")
            except Exception as e:
                print(f"  Error reading input: {e}")

        await page.wait_for_timeout(3000)
        await browser.close()

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/jobs/view/4387980268"
    asyncio.run(dump_modal_html(url))
