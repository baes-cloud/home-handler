import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .. import TZ
from ..models import Observation
from . import REGISTRY
from .base import Watcher

H = 3600.0
LABELS = {"ha_backup": "HA backup", "nas_backup": "Immich backup to the NAS", "kometa": "Kometa's nightly Plex run",
          "sets_grab": "Friday Sets grab", "radar_replay": "Radar Replay recording", "smart": "Full SMART disk test"}


@dataclass
class Result:
    last: float | None = None   # epoch of the last good run
    failed: bool = False        # the last run reported failure
    skip: bool = False          # not applicable right now
    note: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class Job:
    name: str
    max_age_s: float
    probe: Callable


def _iso(s):
    return datetime.fromisoformat(s).timestamp()


def parse_kometa(text):
    m = re.findall(r"Finished: (\d\d:\d\d:\d\d) (\d{4}-\d\d-\d\d)", text or "")
    if not m:
        return None
    t, d = m[-1]
    return datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=ZoneInfo(TZ)).timestamp()


def parse_smart(text):
    """-> ({disk: hours since last completed extended test or None}, [disks with a failed test])."""
    disks, failed, cur, poh = {}, [], None, None
    for line in text.splitlines():
        if line.startswith("== "):
            cur, poh = line[3:].strip(), None
            continue
        if cur is None:
            continue
        if "Power_On_Hours" in line:
            poh = int(line.split()[-1])
            disks.setdefault(cur, None)
        elif re.match(r"#\s*\d+\s", line):
            if "read failure" in line:
                failed.append(cur)
            m = re.search(r"\s(\d+)\s+\S+\s*$", line)  # LifeTime(hours) column, then LBA_of_first_error
            if ("Extended offline" in line and "Completed without error" in line and poh is not None
                    and m and disks.get(cur) is None):
                disks[cur] = poh - int(m.group(1))
    return disks, sorted(set(failed))


# probes ----------------------------------------------------------------
def probe_ha_backup(ctx):
    data, _ = ctx.status()
    if data is None:
        return Result(skip=True)  # unreadable this time; the watchdog judges collector staleness
    lb = data.get("ha", {}).get("last_backup")
    return Result(last=_iso(lb) if lb else None)


def probe_nas_backup(ctx):
    data, _ = ctx.status()
    if data is None:
        return Result(skip=True)
    v = data.get("hosts", {}).get("failover-host", {}).get("nas_backup", "")
    if "|" not in v:
        return Result()
    res, when = v.split("|", 1)
    try:
        ts = _iso(when)
    except ValueError:
        ts = None
    return Result(last=ts, failed=res != "success", note=res)


def probe_kometa(ctx):
    rc, out, _ = ctx.ssh("nas", "docker logs --since 40h kometa 2>&1 | grep -o 'Finished: [0-9:]* [0-9-]*' | tail -1",
                         timeout=60)
    return Result(last=parse_kometa(out) if rc == 0 else None)


def _mtime(ctx, host, path):
    rc, out, _ = ctx.ssh(host, f"stat -c %Y {path}", timeout=30)
    try:
        return float(out.strip()) if rc == 0 else None
    except ValueError:
        return None


def probe_sets_grab(ctx):
    return Result(last=_mtime(ctx, "nas", "/mnt/user/appdata/music-scan/weekly-sets/last-run"))


def probe_radar_replay(ctx):
    try:
        home = ctx.ha.rest("GET", "/api/states/binary_sensor.bae_home").get("state")
    except Exception:
        home = None
    if home != "on":
        return Result(skip=True)
    return Result(last=_mtime(ctx, "ha", "/config/.storage/rmm_movement_history.jsonl"))


def probe_smart(ctx):
    rc, out, _ = ctx.ssh("nas", "for d in /dev/sd?; do echo \"== $d\"; smartctl -n standby -A -l selftest $d; done",
                         timeout=120)
    if rc not in (0, 2) and not out:
        return Result()
    disks, failed = parse_smart(out)
    if not disks:
        return Result(skip=True)  # all asleep or no SMART-capable disks seen
    never = [d for d, h in disks.items() if h is None]
    oldest = max((h for h in disks.values() if h is not None), default=None)
    if never:
        return Result(last=None, failed=bool(failed), note=f"never fully tested: {', '.join(never)}",
                      detail={"disks": disks, "failed": failed})
    return Result(last=ctx.now() - oldest * H, failed=bool(failed), detail={"disks": disks, "failed": failed})


JOBS = [
    Job("ha_backup", 26 * H, probe_ha_backup),
    Job("nas_backup", 26 * H, probe_nas_backup),
    Job("kometa", 30 * H, probe_kometa),
    Job("sets_grab", 8 * 24 * H, probe_sets_grab),
    Job("radar_replay", 1800, probe_radar_replay),
    Job("smart", 90 * 24 * H, probe_smart),
]


class QuietWatcher(Watcher):
    """Watcher 8: jobs that should have run and quietly didn't."""

    name = "quiet"
    interval_s = 3600
    expect_s = 7200

    def __init__(self, jobs=None):
        self.jobs = jobs if jobs is not None else JOBS

    async def check(self, ctx):
        now = ctx.now()
        factor = ctx.cfg["quiet"]["overdue_factor_urgent"]
        out = []
        for job in self.jobs:
            label = LABELS.get(job.name, job.name)
            try:
                r = await asyncio.to_thread(job.probe, ctx)
            except Exception as e:
                out.append(Observation(self.name, f"quiet:{job.name}", 2, f"Couldn't check the {label}: {e}"[:200],
                                       {"error": str(e)[:300]}, now))
                continue
            if r.skip:
                continue
            if job.name == "smart" and r.failed:
                out.append(Observation(self.name, "quiet:smart_failure", 3, "A NAS disk failed its SMART self-test",
                                       r.detail, now))
            if r.last is None:
                out.append(Observation(self.name, f"quiet:{job.name}", 2,
                                       f"{label}: no record of a good run{' (' + r.note + ')' if r.note else ''}",
                                       r.detail, now))
                continue
            age = now - r.last
            if r.failed and job.name != "smart":
                out.append(Observation(self.name, f"quiet:{job.name}", 2, f"{label} last run reported '{r.note}'",
                                       {"last": r.last}, now))
            elif age > job.max_age_s:
                sev = 3 if age > factor * job.max_age_s else 2
                out.append(Observation(self.name, f"quiet:{job.name}", sev,
                                       f"{label} hasn't run for {age / H:.0f} h (expected every {job.max_age_s / H:.0f} h)",
                                       {"last": r.last, **r.detail}, now))
        return out


REGISTRY.append(QuietWatcher)
