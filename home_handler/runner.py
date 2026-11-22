import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import TZ
from .models import Observation

log = logging.getLogger("home_handler.runner")


class Runner:
    def __init__(self, ctx, watchers, event_hooks=None):
        self.ctx = ctx
        self.watchers = watchers
        ctx.watchers = watchers
        self.last_run = {}
        self.event_hooks = event_hooks or []  # callables(event) for non-watcher work
        self._drop_start = None

    def _record(self, w, observations):
        for o in observations:
            self.ctx.journal.add_observation(o)
        return len(observations)

    async def _run_one(self, w):
        try:
            obs = await w.check(self.ctx) or []
            fired = self._record(w, obs)
            self._close_cleared(w, {o.fingerprint for o in obs})
            self.ctx.journal.record_watcher_run(w.name, True, fired, ts=self.ctx.now())
        except Exception as e:  # isolation: one watcher never stops the others
            log.exception("watcher %s failed", w.name)
            self.ctx.journal.record_watcher_run(w.name, False, 0, error=f"{type(e).__name__}: {e}"[:500],
                                                ts=self.ctx.now())

    def _close_cleared(self, w, seen):
        """After a good run, close this watcher's open incidents it no longer sees (the condition cleared)."""
        keep = getattr(w, "keep_open", ())
        for inc in self.ctx.journal.incidents(status="open", source="watcher"):
            if inc.watcher != w.name or inc.fingerprint in seen or inc.fingerprint.startswith(keep or ("\0",)):
                continue
            self.ctx.journal.set_status(inc.id, "done", f"cleared: the {w.name} watcher no longer sees it", "agent")

    async def tick(self, now=None, force=False, only=None):
        now = now if now is not None else self.ctx.now()
        for w in self.watchers:
            if only and w.name not in only:
                continue
            if not w.interval_s and not force:
                continue
            last = self.last_run.get(w.name)
            if force or last is None or now - last >= w.interval_s:
                self.last_run[w.name] = now
                await self._run_one(w)

    def handle_event(self, event):
        for w in self.watchers:
            try:
                self._record(w, w.on_event(event, self.ctx) or [])
            except Exception as e:
                log.exception("watcher %s on_event failed", w.name)
                self.ctx.journal.record_watcher_run(w.name, False, 0, error=f"event: {e}"[:500], ts=self.ctx.now())
        for h in self.event_hooks:
            try:
                h(event)
            except Exception:
                log.exception("event hook failed")

    # websocket outages (Review Focus 2: one incident per day) --------
    def on_disconnect(self, ts):
        self.ctx.ws_connected = False
        if self._drop_start is None:
            self._drop_start = ts
            self.ctx.last_ws_drop = ts

    def on_connect(self, ts):
        self.ctx.ws_connected = True
        if self._drop_start is None:
            return
        start, self._drop_start = self._drop_start, None
        day = datetime.fromtimestamp(start, ZoneInfo(TZ)).strftime("%Y-%m-%d")
        fp = f"ha_health:ws_drop:{day}"
        prev = self.ctx.journal.find_active(fp)
        outages = (prev.detail.get("outages", []) if prev else []) + [{"start": start, "seconds": round(ts - start)}]
        self.ctx.journal.add_observation(Observation(
            "ha_health", fp, 1, f"HA connection dropped {len(outages)}x on {day}",
            {"outages": outages}, ts))

    async def run_forever(self, tick_s=15):
        ha_task = None
        if self.ctx.ha is not None and hasattr(self.ctx.ha, "run"):
            ha_task = asyncio.create_task(self.ctx.ha.run(
                self.handle_event, ["state_changed", "homeassistant_started"],
                on_connect=self.on_connect, on_disconnect=self.on_disconnect))
        try:
            while True:
                await self.tick()
                await asyncio.sleep(tick_s)
        finally:
            if ha_task:
                ha_task.cancel()


def utcnow():
    return time.time()
