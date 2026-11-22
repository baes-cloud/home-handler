from datetime import datetime
from zoneinfo import ZoneInfo

from .. import TZ
from ..models import Observation
from . import REGISTRY
from .base import Watcher


def restart_seconds(drop_ts, started_ts):
    return started_ts - drop_ts


class HAHealthWatcher(Watcher):
    """Watcher 7: failover blips, slow restarts, repairs, integrations stuck retrying, pending updates."""

    name = "ha_health"
    interval_s = 900
    expect_s = 2700
    keep_open = ("ha_health:ws_drop:",)  # one per day, opened by the runner; closed here once the day is over

    def on_event(self, event, ctx):
        if event.get("event_type") != "homeassistant_started" or not ctx.last_ws_drop:
            return []
        ts = datetime.fromisoformat(event["time_fired"]).timestamp() if event.get("time_fired") else ctx.now()
        secs = restart_seconds(ctx.last_ws_drop, ts)
        ctx.last_ws_drop = None
        if not 0 < secs < 3600 or secs <= ctx.cfg["ha_health"]["restart_slow_s"]:
            return []
        return [Observation(self.name, "ha_health:restart_slow", 2,
                            f"HA took {secs:.0f} s to restart (the failover promotes at 420 s)",
                            {"seconds": round(secs)}, ts)]

    async def check(self, ctx):
        c = ctx.cfg["ha_health"]
        now = ctx.now()
        out = []
        data, _ = ctx.status()
        if data:
            bl = data.get("hosts", {}).get("failover-host", {})
            blips = bl.get("failover_blips_24h")
            if isinstance(blips, int):
                ctx.journal.add_sample("blips:24h", now, blips)
                if blips >= c["blips_24h"]:
                    trend = [int(v) for _, v in ctx.journal.samples("blips:24h", now - 7 * 86400)[::288]]
                    out.append(Observation(self.name, "ha_health:failover_blips", 2,
                                           f"The failover watchdog saw HA fail its health check {blips}x in 24 h",
                                           {"blips_24h": blips, "last": bl.get("failover_last"), "trend_7d": trend},
                                           now))
            ha = data.get("ha", {})
            behind = [k for k in ("core", "os", "supervisor")
                      if isinstance(ha.get(k), dict) and ha[k].get("version") != ha[k].get("latest")]
            if behind:
                out.append(Observation(self.name, "ha_health:updates", 0, f"HA updates available: {', '.join(behind)}",
                                       {k: ha[k] for k in behind}, now))
        today = datetime.fromtimestamp(now, ZoneInfo(TZ)).strftime("%Y-%m-%d")
        for inc in ctx.journal.incidents(status="open", source="watcher"):
            if inc.fingerprint.startswith("ha_health:ws_drop:") and inc.fingerprint[-10:] < today:
                ctx.journal.set_status(inc.id, "done", "day over", "agent")
        # fallback for restarts whose homeassistant_started event we missed: long websocket outages
        for inc in ctx.journal.incidents(status=("open", "waiting"), since=now - 86400):
            if not inc.fingerprint.startswith("ha_health:ws_drop:"):
                continue
            slow = [o for o in inc.detail.get("outages", []) if o.get("seconds", 0) > c["restart_slow_s"]]
            if slow:
                worst = max(o["seconds"] for o in slow)
                out.append(Observation(self.name, "ha_health:restart_slow", 2,
                                       f"HA was unreachable for {worst} s during a restart or outage "
                                       "(the failover promotes at 420 s)", {"seconds": worst}, now))
        issues = (await ctx.ha.call("repairs/list_issues") or {}).get("issues", [])
        for i in issues:
            if i.get("ignored") or i.get("dismissed_version"):
                continue
            out.append(Observation(self.name, f"ha_health:repair:{i['issue_id']}", 1,
                                   f"HA repair: {i.get('domain')} {i.get('translation_key')}",
                                   {"domain": i.get("domain"), "severity": i.get("severity")}, now))
        retry_since = ctx.journal.kv_get("ha_health_retry", {})
        seen = {}
        for e in await ctx.ha.call("config_entries/get") or []:
            if e.get("state") not in ("setup_retry", "setup_error"):
                continue
            since = retry_since.get(e["entry_id"], now)
            seen[e["entry_id"]] = since
            if now - since >= c["entry_retry_s"]:
                out.append(Observation(self.name, f"ha_health:entry:{e['entry_id']}", 2,
                                       f"{e.get('title')} ({e.get('domain')}) has been failing to set up for "
                                       f"{(now - since) / 60:.0f} min", {"state": e.get("state")}, now))
        ctx.journal.kv_set("ha_health_retry", seen)
        return out


REGISTRY.append(HAHealthWatcher)
