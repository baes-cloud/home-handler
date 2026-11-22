"""Dry run: what the watchers would have caught over the last N days (HA keeps 10 days of history)."""

import asyncio
import json
import logging
import urllib.parse
from collections import Counter
from datetime import datetime, timezone

from .watchers.devices import DOWN, DevicesWatcher, episodes

log = logging.getLogger("home_handler.replay")
DAY = 86400.0


def device_findings(history, names, cfg, end_ts):
    """history: {device_id: [(ts, state)]} -> [(fingerprint, title, ts)]."""
    c = cfg["devices"]
    out, dead = [], []
    for dev, tr in history.items():
        if tr and all(st in DOWN for _, st in tr):
            dead.append(names.get(dev, dev))
            continue
        eps = [(s, (e if e is not None else end_ts) - s) for s, e in episodes(tr)]
        eps = [(s, d) for s, d in eps if d >= c["min_episode_s"]]
        name = names.get(dev, dev)
        for s, _ in eps:  # any 7-day window starting at an episode
            n = sum(1 for s2, _ in eps if s <= s2 < s + 7 * DAY)
            if n >= c["episodes_7d"]:
                out.append((f"devices:{dev}", f"{name} dropped off {n} times in a week", s))
                break
        else:
            long = [(s, d) for s, d in eps if d > c["long_episode_s"]]
            if long:
                s, d = max(long, key=lambda x: x[1])
                out.append((f"devices:{dev}", f"{name} was offline for {d / 3600:.0f} h", s))
    if dead:
        out.append(("devices:long_dead", f"{len(dead)} devices offline the whole time: {', '.join(sorted(dead)[:8])}",
                    end_ts))
    return out


def blip_findings(journal_text, per_day=2):
    days = Counter()
    for line in journal_text.splitlines():
        if "primary health check failing" in line:
            days[line[:10]] += 1
    return [(f"ha_health:failover_blips:{d}", f"Failover watchdog saw {n} HA health-check failures on {d}",
             datetime.fromisoformat(d).timestamp()) for d, n in sorted(days.items()) if n >= per_day]


def backup_findings(dates, max_gap_s=26 * 3600):
    ts = sorted(datetime.fromisoformat(d).timestamp() for d in dates)
    return [("quiet:ha_backup", f"No HA backup for {(b - a) / 3600:.0f} h", a)
            for a, b in zip(ts, ts[1:]) if b - a > max_gap_s]


async def _device_history(ctx, days):
    w = DevicesWatcher()
    await w._load_registries(ctx)
    start = datetime.fromtimestamp(ctx.now() - days * DAY, timezone.utc).isoformat()
    ents = list(w.by_entity)
    hist = {}
    for i in range(0, len(ents), 100):
        batch = ents[i:i + 100]
        q = urllib.parse.urlencode({"filter_entity_id": ",".join(batch)}) + "&minimal_response&no_attributes"
        rows = await asyncio.to_thread(ctx.ha.rest, "GET", f"/api/history/period/{urllib.parse.quote(start)}?{q}",
                                       None, 180)
        for series in rows or []:
            if not series:
                continue
            eid = series[0].get("entity_id")
            dev = w.by_entity.get(eid)
            if dev:
                hist[dev] = [(datetime.fromisoformat(r["last_changed"]).timestamp(), r["state"]) for r in series]
    return hist, w.names


async def main(ctx, days=10):
    task = asyncio.create_task(ctx.ha.run(lambda e: None, []))
    try:
        await asyncio.wait_for(ctx.ha.connected.wait(), 30)
        findings = []
        hist, names = await _device_history(ctx, days)
        findings += [("devices",) + f for f in device_findings(hist, names, ctx.cfg, ctx.now())]
        rc, out, _ = ctx.ssh("failover-host", f"journalctl -u ha-failover --since -{days}d --no-pager -o short-iso", 120)
        if rc == 0:
            findings += [("ha_health",) + f for f in blip_findings(out)]
        rc, out, _ = ctx.ssh("ha", "ha backups --raw-json", 60)
        if rc == 0:
            bks = [b for b in json.loads(out)["data"]["backups"]
                   if b["name"].startswith("Automatic backup") or b.get("type") == "full"]
            recent = [b["date"] for b in bks if datetime.fromisoformat(b["date"]).timestamp() > ctx.now() - days * DAY]
            findings += [("quiet",) + f for f in backup_findings(recent)]
        ctx.journal.clear_replay()
        for watcher, fp, title, ts in findings:
            ctx.journal.add_replay_finding(watcher, fp, title, ts)
        by = Counter(f[0] for f in findings)
        print(f"replay over {days} days: {len(findings)} findings", dict(by))
        for watcher, fp, title, ts in findings:
            print(f"  {watcher:10} {title}")
    finally:
        task.cancel()
