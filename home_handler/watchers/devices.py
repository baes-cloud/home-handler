import asyncio
import json
from datetime import datetime

from ..models import Observation
from . import REGISTRY
from .base import Watcher
from .space import project_full

DAY = 86400.0
DOWN = ("unavailable", "unknown")


def primary_entities(entity_reg, device_reg):
    """device_id -> one representative entity_id (non-diagnostic first, enabled only)."""
    enabled = {d["id"] for d in device_reg if not d.get("disabled_by")}
    best = {}
    for e in sorted(entity_reg, key=lambda e: e["entity_id"]):
        dev = e.get("device_id")
        if not dev or dev not in enabled or e.get("disabled_by"):
            continue
        rank = 0 if e.get("entity_category") is None else 1
        if dev not in best or rank < best[dev][0]:
            best[dev] = (rank, e["entity_id"])
    return {d: eid for d, (_, eid) in best.items()}


def episodes(transitions):
    """[(ts, state)] -> [(start, end|None)] spells spent unavailable/unknown."""
    out, start = [], None
    for ts, st in transitions:
        if st in DOWN and start is None:
            start = ts
        elif st not in DOWN and start is not None:
            out.append((start, ts))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def battery_days_to(points, floor=15):
    secs = project_full(points, floor, min_points=3)
    return None if secs is None else secs / DAY


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


class DevicesWatcher(Watcher):
    """Watcher 5: devices dropping off more than usual, routers first, and batteries running down."""

    name = "devices"
    interval_s = 3600
    expect_s = 7200

    def __init__(self):
        self.primary = {}      # device_id -> entity_id
        self.by_entity = {}    # entity_id -> device_id
        self.names = {}        # device_id -> name
        self.ieee = {}         # device_id -> ieee
        self.routers = set()
        self.loaded_at = 0
        self.open = None       # device_id -> episode start (persisted in kv)
        self.long_dead = None  # device ids excluded until they come back

    # state -----------------------------------------------------------
    def _load_state(self, ctx):
        if self.open is None:
            self.open = {k: float(v) for k, v in ctx.journal.kv_get("devices_open", {}).items()}
            self.long_dead = set(ctx.journal.kv_get("devices_long_dead", []))

    def _save_state(self, ctx):
        ctx.journal.kv_set("devices_open", self.open)
        ctx.journal.kv_set("devices_long_dead", sorted(self.long_dead))

    async def _load_registries(self, ctx):
        ent = await ctx.ha.call("config/entity_registry/list")
        dev = await ctx.ha.call("config/device_registry/list")
        self.primary = primary_entities(ent, dev)
        self.by_entity = {e: d for d, e in self.primary.items()}
        self.names = {d["id"]: d.get("name_by_user") or d.get("name") or d["id"] for d in dev}
        self.ieee = {}
        for d in dev:
            for ident in d.get("identifiers", []):
                s = str(ident[-1]) if ident else ""
                if s.startswith("zigbee2mqtt_0x"):
                    self.ieee[d["id"]] = s.split("_", 1)[1].lower()
        rc, out, _ = await asyncio.to_thread(ctx.ssh, "failover-host", "docker exec zigbee2mqtt cat /app/data/database.db",
                                             30)
        if rc == 0 and out:
            routers = set()
            for line in out.splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "Router":
                    routers.add(str(o.get("ieeeAddr", "")).lower())
            self.routers = {d for d, a in self.ieee.items() if a in routers}
        self.loaded_at = ctx.now()

    def _baseline(self, ctx, states):
        if ctx.journal.kv_get("devices_baseline"):
            return
        # HA resets last_changed on every restart, so it can't tell "down a week" from "down since the
        # last reboot". Every device that is down when the agent first starts is baselined as long-dead;
        # only spells that start after this moment open their own incidents.
        for s in states:
            dev = self.by_entity.get(s["entity_id"])
            if dev and s.get("state") in DOWN:
                self.long_dead.add(dev)
        if self.long_dead:
            names = sorted(self.names.get(d, d) for d in self.long_dead)
            ctx.journal.add_observation(Observation(
                self.name, "devices:long_dead", 1, f"{len(names)} devices were already offline when the agent started",
                {"devices": names}, ctx.now()))
        ctx.journal.kv_set("devices_baseline", True)
        ctx.journal.kv_set("devices_baseline_ts", ctx.now())
        self._save_state(ctx)

    def _reconcile(self, ctx, states):
        """Catch changes missed while the websocket was down or HA was restarting."""
        base = ctx.journal.kv_get("devices_baseline_ts", 0)
        changed = False
        for s in states:
            dev = self.by_entity.get(s["entity_id"])
            if not dev:
                continue
            since = _ts(s["last_changed"]) if s.get("last_changed") else ctx.now()
            if s.get("state") in DOWN:
                if dev not in self.open and dev not in self.long_dead:
                    self.open[dev] = max(since, base)
                    changed = True
            else:
                if dev in self.long_dead:
                    self.long_dead.discard(dev)
                    changed = True
                start = self.open.pop(dev, None)
                if start is not None:
                    changed = True
                    if since - start >= ctx.cfg["devices"]["min_episode_s"]:
                        ctx.journal.add_sample(f"episode:{dev}", start, since - start)
        if changed:
            self._save_state(ctx)

    # events ----------------------------------------------------------
    def on_event(self, event, ctx):
        if event.get("event_type") != "state_changed" or self.open is None:
            return []
        d = event.get("data", {})
        dev = self.by_entity.get(d.get("entity_id"))
        if not dev:
            return []
        new = (d.get("new_state") or {}).get("state")
        old = (d.get("old_state") or {}).get("state")
        ts = _ts(event["time_fired"]) if event.get("time_fired") else ctx.now()
        if d.get("old_state") is None:
            # HA (re)starting: the previous state is unknown, so this is not a drop. Coming up closes a spell.
            if new not in DOWN and (dev in self.open or dev in self.long_dead):
                old = "unavailable"
            else:
                return []
        if new in DOWN and old not in DOWN:
            if dev not in self.long_dead:
                self.open[dev] = ts
                self._save_state(ctx)
        elif new not in DOWN and old in DOWN:
            if dev in self.long_dead:
                self.long_dead.discard(dev)
            start = self.open.pop(dev, None)
            if start is not None and ts - start >= ctx.cfg["devices"]["min_episode_s"]:
                ctx.journal.add_sample(f"episode:{dev}", start, ts - start)
            self._save_state(ctx)
        return []

    # checks ----------------------------------------------------------
    def _batteries(self, ctx, states):
        c = ctx.cfg["devices"]
        now = ctx.now()
        out = []
        for s in states:
            if (s.get("attributes") or {}).get("device_class") != "battery":
                continue
            try:
                level = float(s["state"])
            except (TypeError, ValueError):
                continue
            key = f"battery:{s['entity_id']}"
            last = ctx.journal.last_sample(key)
            if not last or now - last[0] > 20 * 3600:
                ctx.journal.add_sample(key, now, level)
            days = battery_days_to(ctx.journal.samples(key, now - 30 * DAY), c["battery_floor"])
            if level < c["battery_floor"] or (days is not None and days < c["battery_days"]):
                dev = self.by_entity.get(s["entity_id"]) or s["entity_id"]
                name = (s.get("attributes") or {}).get("friendly_name", s["entity_id"])
                why = f"at {level:.0f}%" if level < c["battery_floor"] else f"about {days:.0f} days to {c['battery_floor']}%"
                label = name if "battery" in name.lower() else f"{name} battery"
                out.append(Observation(self.name, f"battery:{dev}", 2, f"{label} {why}",
                                       {"level": level, "days_to_floor": days}, now))
        return out

    def _rules(self, ctx):
        c = ctx.cfg["devices"]
        now = ctx.now()
        out = []
        for dev in self.primary:
            if dev in self.long_dead:
                continue
            eps = ctx.journal.samples(f"episode:{dev}", now - 7 * DAY)
            n = len(eps)
            longest = max((dur for _, dur in eps), default=0)
            start = self.open.get(dev)
            ongoing = (now - start) if start is not None and now - start < 7 * DAY else 0
            if start is not None and now - start >= c["min_episode_s"]:
                n += 1
            if n >= c["episodes_7d"] or max(longest, ongoing) > c["long_episode_s"]:
                router = dev in self.routers
                name = self.names.get(dev, dev)
                title = (f"{name} dropped off {n} times this week" if n >= c["episodes_7d"]
                         else f"{name} has been offline for {max(longest, ongoing) / 3600:.0f} h")
                if router:
                    title += " (it's a Zigbee router)"
                out.append(Observation(self.name, f"devices:{dev}", 3 if router else 2, title,
                                       {"episodes_7d": n, "longest_s": round(max(longest, ongoing)),
                                        "offline_now": start is not None, "router": router}, now))
        if self.long_dead:
            names = sorted(self.names.get(d, d) for d in self.long_dead)
            out.append(Observation(self.name, "devices:long_dead", 1,
                                   f"{len(names)} devices were already offline when the agent started",
                                   {"devices": names}, now))
        return out

    async def check(self, ctx):
        self._load_state(ctx)
        if not self.primary or ctx.now() - self.loaded_at > DAY:
            await self._load_registries(ctx)
        states = await ctx.ha.call("get_states") or []
        self._baseline(ctx, states)
        self._reconcile(ctx, states)
        return self._rules(ctx) + self._batteries(ctx, states)


REGISTRY.append(DevicesWatcher)
