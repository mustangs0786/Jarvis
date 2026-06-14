"""
screencast.py — continuous live view of the agent's browser
============================================================
Streams VIEWPORT screenshots of the agent's browser through the existing
on_screenshot callback (→ SSE "shot" events → the dashboard's #shot img,
which cache-busts with ?t=). No WebSocket, no extra server.

Usage:
    cast = start_screencast(ctx, on_screenshot, user_id)   # pass the CONTEXT
    try:
        ...                    # nothing else to do — it auto-follows tabs
    finally:
        await stop_screencast(cast)

WHY IT FOLLOWS THE CONTEXT, NOT A PAGE
--------------------------------------
The agent constantly changes which page/tab it works on: Workday's "Apply"
opens the real form in a NEW tab, multi-step portals navigate, etc. The old
design held a single `page` reference that every code path had to remember to
update (`cast.page = new_page`); one missed update and the live view froze on a
stale tab while the agent worked elsewhere (the gateway-page-stuck bug).

Instead we capture the CONTEXT and, every tick, screenshot whatever tab is
actually FRONTMOST (document.visibilityState === "visible"). The live view then
always shows exactly what's on screen — no manual tracking, no desync.

Viewport-only (full_page=False): every frame is a fast ~200-400 KB PNG —
no multi-second full-page renders, no huge files, no lag.
"""

import os
import asyncio
import logging

logger = logging.getLogger(__name__)


class Screencast:
    """Streams the FRONTMOST tab of a browser context. The orchestrator no longer
    needs to tell it which page is active — it figures that out each tick."""

    def __init__(self, ctx, on_screenshot, user_id, interval: float = 0.5):
        self._ctx = ctx
        self._on_screenshot = on_screenshot
        self._path = f"output/live_{user_id}.png"
        self._tmp = self._path + ".tmp"
        self._interval = interval
        self._task = asyncio.create_task(self._loop())
        logger.info(f"  Live screencast started → {self._path} ({interval}s interval)")

    async def _active_page(self):
        """The tab the user is actually looking at: the one whose
        document.visibilityState is 'visible'. Falls back to the newest open tab
        (new tabs are appended to ctx.pages), then to any open tab."""
        try:
            pages = [p for p in self._ctx.pages if not p.is_closed()]
        except Exception:
            return None
        if not pages:
            return None
        if len(pages) == 1:
            return pages[0]
        # Newest-first: if the agent just opened a tab, that's almost always the
        # one in focus — and it short-circuits the visibility probe in the common
        # case.
        for p in reversed(pages):
            try:
                if await p.evaluate("document.visibilityState") == "visible":
                    return p
            except Exception:
                continue
        return pages[-1]

    async def _loop(self):
        while True:
            try:
                pg = await self._active_page()
                if pg is not None:
                    data = await pg.screenshot(full_page=False, timeout=3000, type="png")
                    with open(self._tmp, "wb") as f:
                        f.write(data)
                    os.replace(self._tmp, self._path)
                    if self._on_screenshot:
                        await self._on_screenshot(self._path)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._interval)

    # pause()/resume() retained as no-ops so existing callers don't break, but the
    # live stream now runs continuously — it never freezes while a question is up,
    # so the user always sees the real browser and can act on it directly.
    def pause(self):
        pass

    def resume(self):
        pass

    @property
    def task(self):
        return self._task


def start_screencast(ctx, on_screenshot, user_id, interval: float = 0.5) -> "Screencast":
    """Start a background screencast that auto-follows the frontmost tab of `ctx`."""
    return Screencast(ctx, on_screenshot, user_id, interval)


async def stop_screencast(handle):
    """Cancel the screencast and wait for it to wind down.
    Accepts either a Screencast handle or a bare asyncio.Task (backward compat)."""
    if not handle:
        return
    task = handle.task if isinstance(handle, Screencast) else handle
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
