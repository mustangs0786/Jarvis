"""
screencast.py — continuous live view of the agent's browser
============================================================
Streams VIEWPORT screenshots of a Playwright page through the existing
on_screenshot callback (→ SSE "shot" events → the dashboard's #shot img,
which cache-busts with ?t=). No WebSocket, no extra server.

Usage:
    cast = start_screencast(page, on_screenshot, user_id)
    try:
        ...                    # cast.page = new_page on tab switch
        cast.pause()           # freeze during user questions
        ...                    # _steer pushes its own full-page shot
        cast.resume()          # resume live view after answer
    finally:
        await stop_screencast(cast)

Viewport-only (full_page=False): every frame is a fast ~200-400 KB PNG at
1280×900 — no multi-second full-page renders, no huge files, no lag.
The agent's own scrolling (scroll_into_view_if_needed before every action)
keeps the viewport centred on the current field, so the user naturally
sees what the agent sees.

Standalone module so both engines (linkedin_easy_apply, apply_orchestrator)
can import it without touching their existing lazy-import cycle.
"""

import os
import asyncio
import logging

logger = logging.getLogger(__name__)


class Screencast:
    """Mutable handle so the orchestrator can point the screencast at a new page
    (e.g. after switch_if_new_tab) without stopping/restarting the loop."""

    def __init__(self, page, on_screenshot, user_id, interval: float = 0.5):
        self.page = page
        self._on_screenshot = on_screenshot
        self._path = f"output/live_{user_id}.png"
        self._tmp = self._path + ".tmp"
        self._interval = interval
        self._paused = False
        self._task = asyncio.create_task(self._loop())
        logger.info(f"  Live screencast started → {self._path} ({interval}s interval)")

    async def _loop(self):
        while True:
            if not self._paused:
                try:
                    pg = self.page
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

    def pause(self):
        """Freeze the live stream (e.g. while a user question is displayed)."""
        self._paused = True

    def resume(self):
        """Un-freeze the live stream after the user answers."""
        self._paused = False

    @property
    def task(self):
        return self._task


def start_screencast(page, on_screenshot, user_id, interval: float = 0.5) -> "Screencast":
    """Start a background screencast. Returns a Screencast handle — update
    ``handle.page`` whenever the active page changes (tab switch, etc.)."""
    return Screencast(page, on_screenshot, user_id, interval)


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
