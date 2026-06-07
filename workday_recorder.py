"""
workday_recorder.py — Capture a real Workday application flow for building a
deterministic handler.

You drive ONE full application manually in the browser window (log in, fill,
click Next ... up to but NOT necessarily Submit). On every page change it dumps
that page's interactive elements — their stable `data-automation-id`, label,
type, and dropdown options — plus a screenshot, into workday_pages/.

Then send me workday_pages/ and I build a deterministic workday_handler.py from
the REAL selectors (no guessing).

Usage:
    python workday_recorder.py <workday_apply_url>
    # then drive the form manually; press ENTER to dump the current page,
    # type q + ENTER to finish.
"""

import sys
import json
import asyncio
from pathlib import Path

OUT = Path("workday_pages")

# Capture every element that has a stable automation id OR is a form control.
DUMP_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll(
    "[data-automation-id], input, select, textarea, button, [role=button], [role=option], [role=combobox]");
  for (const el of nodes) {
    if (seen.has(el)) continue; seen.add(el);
    const tag = (el.tagName || '').toLowerCase();
    const aid = el.getAttribute('data-automation-id');
    if (!aid && !['input','select','textarea','button'].includes(tag)) continue;
    const r = el.getBoundingClientRect();
    const visible = !!(el.offsetParent || el.getClientRects().length) && r.width > 1 && r.height > 1;

    let label = el.getAttribute('aria-label') || '';
    if (!label && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label) { const p = el.closest('label'); if (p) label = p.innerText; }
    if (!label) label = (el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '').trim();
    label = (label || '').replace(/\s+/g, ' ').trim().slice(0, 120);

    const rec = {
      automation_id: aid,
      tag,
      type: el.getAttribute('type') || null,
      label,
      visible,
      required: el.getAttribute('required') !== null || el.getAttribute('aria-required') === 'true',
    };
    if (tag === 'select') rec.options = Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean).slice(0, 60);
    out.push(rec);
  }
  return { url: location.href, title: document.title, count: out.length, elements: out };
}
"""


async def dump(page, idx: int):
    try:
        data = await page.evaluate(DUMP_JS)
    except Exception as e:
        print(f"  ! dump failed: {e}")
        return
    slug = (data["url"].split("?")[0].rstrip("/").split("/")[-1] or "page")[:40]
    base = OUT / f"{idx:02d}_{slug}"
    base.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    vis = sum(1 for e in data["elements"] if e["visible"])
    aids = sum(1 for e in data["elements"] if e["automation_id"])
    print(f"  ✓ {base.name}.json — {data['count']} elements ({vis} visible, {aids} with automation-id)")
    print(f"    {data['url'][:90]}")


async def main(url: str):
    from playwright.async_api import async_playwright
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            "browser_profile", headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        counter = {"n": 0}

        async def on_nav(frame):
            if frame == page.main_frame:
                counter["n"] += 1
                # Workday renders the form fields async AFTER navigation. Wait for the
                # network to settle and for real form controls (not just the loading
                # skeleton) to appear before dumping.
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                try:
                    await page.wait_for_selector(
                        "input:visible, [data-automation-id='pageFooterNextButton'], "
                        "[data-automation-id='createAccountSubmitButton']", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(2.0)
                await dump(page, counter["n"])

        page.on("framenavigated", lambda f: asyncio.create_task(on_nav(f)))

        await page.goto(url, wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("Drive the Workday application MANUALLY in the browser window.")
        print("It auto-dumps on each page change.")
        print("  • Press ENTER here to dump the CURRENT page (use after opening a")
        print("    dropdown, or when a section expands without a page change).")
        print("  • Type  q  + ENTER to finish.")
        print("=" * 70 + "\n")

        loop = asyncio.get_event_loop()
        while True:
            cmd = (await loop.run_in_executor(None, input)).strip().lower()
            if cmd == "q":
                break
            counter["n"] += 1
            await dump(page, counter["n"])

        await ctx.close()
        print(f"\nDone. Captured pages are in: {OUT.resolve()}")
        print("Send me the workday_pages/ folder and I'll build the deterministic handler.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    target = sys.argv[1] if len(sys.argv) > 1 else input("Workday apply URL: ").strip()
    asyncio.run(main(target))
