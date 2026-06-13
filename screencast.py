"""
screencast.py — continuous live view of the agent's browser
============================================================
Streams viewport screenshots of a Playwright page through the existing
on_screenshot callback (→ SSE "shot" events → the dashboard's #ap-shot img,
which cache-busts with ?t=). No WebSocket, no extra server.

Usage:
    cast = start_screencast(page, on_screenshot, user_id)
    try:
        ...
    finally:
        await stop_screencast(cast)

Standalone module so both engines (linkedin_easy_apply, apply_orchestrator)
can import it without touching their existing lazy-import cycle.
"""

import os
import asyncio
import logging

logger = logging.getLogger(__name__)


def start_screencast(page, on_screenshot, user_id, interval: float = 0.7) -> asyncio.Task:
    """Start a background task streaming `page` screenshots ~every `interval`s.
    Returns the task; stop it with stop_screencast(). Per-frame errors
    (navigation in flight, page busy) are swallowed — the loop keeps going
    until cancelled."""
    path = f"output/live_{user_id}.png"
    tmp  = path + ".tmp"

    async def _loop():
        while True:
            try:
                # full_page=True so the live view shows the WHOLE form, not just
                # the visible viewport half. bytes → tmp file → atomic replace so
                # the UI never fetches a half-written PNG. (Don't pass a .tmp path
                # to Playwright — it infers image type from the extension.)
                try:
                    data = await page.screenshot(full_page=True, timeout=3000, type="png")
                except Exception:
                    data = await page.screenshot(full_page=False, timeout=3000, type="png")
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                if on_screenshot:
                    await on_screenshot(path)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # transient: mid-navigation, dialog open, page closing
            await asyncio.sleep(interval)

    logger.info(f"  Live screencast started → {path} ({interval}s interval)")
    return asyncio.create_task(_loop())


async def stop_screencast(task: asyncio.Task | None):
    """Cancel the screencast task and wait for it to wind down."""
    if not task:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
