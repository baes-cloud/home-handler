import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import TZ
from ..models import Observation
from . import REGISTRY
from .base import Watcher

DAY = 86400.0

LABELS = {
    "free_gb:agent-host": "agent-host disk", "free_gb:failover-host": "failover-host disk", "free_gb:pi": "Pi 400 disk",
    "free_gb:nas_user": "NAS array", "free_gb:nas_cache": "NAS cache SSD", "pct:ha_disk": "HA (Pi 5) disk",
}


def project_full(points, floor, min_points=12):
    """Seconds from the newest point until the least-squares line reaches floor; None if not falling."""
    if len(points) < min_points:
        return None
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    sxx = sum((t - mt) ** 2 for t, _ in points)
    if sxx == 0:
        return None
    slope = sum((t - mt) * (v - mv) for t, v in points) / sxx
    if slope >= 0:
        return None
    t_floor = mt + (floor - mv) / slope
    return max(0.0, t_floor - points[-1][0])


def _slope_per_day(points):
    if len(points) < 2:
        return 0.0
    (t0, v0), (t1, v1) = points[0], points[-1]
    return (v1 - v0) / ((t1 - t0) / DAY) if t1 > t0 else 0.0


class SpaceWatcher(Watcher):
    """Watcher 6: disks projected to fill, and Music Assistant memory."""

    name = "space"
    interval_s = 300
    expect_s = 1200

    def _sample(self, ctx, data):
        now = ctx.now()
        hosts = data.get("hosts", {})
        for h in ("agent-host", "failover-host", "pi"):
            v = hosts.get(h, {}).get("disk_root_free_gb")
            if isinstance(v, (int, float)):
                ctx.journal.add_sample(f"free_gb:{h}", now, v)
        for d in hosts.get("nas", {}).get("disks", []):
            key = {"/mnt/user": "free_gb:nas_user", "/mnt/cache": "free_gb:nas_cache"}.get(d.get("mount"))
            if key and isinstance(d.get("avail"), (int, float)):
                ctx.journal.add_sample(key, now, d["avail"] / 1e9)
        pct = data.get("ha", {}).get("disk_pct")
        if isinstance(pct, (int, float)):
            ctx.journal.add_sample("pct:ha_disk", now, pct)

    def _sample_ma(self, ctx):
        now = ctx.now()
        last = ctx.journal.last_sample("mem_mb:music_assistant")
        if last and now - last[0] < ctx.cfg["space"]["ma_every_s"]:
            return
        rc, out, _ = ctx.ssh("ha", "ha apps stats d5369777_music_assistant --raw-json 2>/dev/null", timeout=30)
        if rc == 0:
            try:
                mb = json.loads(out)["data"]["memory_usage"] / 1e6
                ctx.journal.add_sample("mem_mb:music_assistant", now, mb)
            except (ValueError, KeyError, TypeError):
                pass

    def _rules(self, ctx):
        c = ctx.cfg["space"]
        now = ctx.now()
        since = now - c["window_days"] * DAY
        out = []
        for key, label in LABELS.items():
            pts = ctx.journal.samples(key, since)
            if key == "pct:ha_disk":
                secs = project_full([(t, -v) for t, v in pts], -c["ha_disk_pct_ceiling"], c["min_points"])
            else:
                floor = c["floor_gb_nas"] if key.startswith("free_gb:nas") else c["floor_gb_host"]
                secs = project_full(pts, floor, c["min_points"])
            if secs is None:
                continue
            days = secs / DAY
            sev = 3 if days < c["urgent_days"] else 2 if days < c["warn_days"] else 0
            if not sev:
                continue
            when = datetime.fromtimestamp(now + secs, ZoneInfo(TZ)).strftime("%a %-d %b")
            out.append(Observation(self.name, f"space:{key}", sev,
                                   f"{label} full by {when} at the current rate",
                                   {"days_left": round(days, 1), "latest": pts[-1][1], "full_by": when}, now))
        ma = ctx.journal.samples("mem_mb:music_assistant", now - DAY)
        if ma:
            latest = ma[-1][1]
            rise = _slope_per_day(ma) if ma[-1][0] - ma[0][0] > 6 * 3600 else 0.0
            if latest > c["ma_mb_max"] or rise > c["ma_mb_per_day"]:
                out.append(Observation(self.name, "space:music_assistant", 2,
                                       f"Music Assistant is using {latest / 1000:.1f} GB",
                                       {"mb": round(latest), "mb_per_day": round(rise)}, now))
        return out

    async def check(self, ctx):
        data, _ = ctx.status()
        if data:
            self._sample(ctx, data)
        await asyncio.to_thread(self._sample_ma, ctx)  # ssh can take 30 s; keep the event loop free
        return self._rules(ctx)


REGISTRY.append(SpaceWatcher)
