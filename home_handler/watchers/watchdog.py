from ..models import Observation
from . import REGISTRY
from .base import Watcher


class WatchdogWatcher(Watcher):
    """Watcher 10: flags watchers that went silent or keep erroring, and a stale status.json."""

    name = "watchdog"
    interval_s = 300
    expect_s = 1200

    async def check(self, ctx):
        now = ctx.now()
        cfg = ctx.cfg["watchdog"]
        out = []
        stats = {s["name"]: s for s in ctx.journal.watcher_stats()}
        for w in ctx.watchers:
            if w.name == self.name or not w.interval_s:
                continue
            s = stats.get(w.name, {})
            last_ok = s.get("last_ok") or ctx.started
            silent = now - last_ok > w.expect_s
            errors = ctx.journal.watcher_errors_since(w.name, now - 3600)
            if silent or errors >= cfg["errors_per_hour"]:
                why = (f"no good run for {int((now - last_ok) / 60)} min" if silent
                       else f"{errors} errors in the last hour")
                out.append(Observation(self.name, f"watchdog:{w.name}", 2, f"Watcher '{w.name}' is unwell: {why}",
                                       {"last_ok": s.get("last_ok"), "last_error": s.get("last_error"),
                                        "errors_1h": errors}, now))
        # The collector rewrites status.json in place every 5 min, so a single read can catch it half-written.
        # Judge by the newest good copy seen, not by one read.
        data, age = ctx.status()
        if data is not None and age is not None:
            ctx.journal.kv_set("status_last_good_generated", now - age)
        last_good = ctx.journal.kv_get("status_last_good_generated")
        stale_for = now - (last_good if last_good is not None else ctx.started)
        if stale_for > cfg["status_max_age_s"]:
            why = (f"{int(stale_for / 60)} min old" if last_good is not None
                   else "missing or unreadable")
            out.append(Observation(self.name, "watchdog:status_json", 2,
                                   f"Homelab collector output is {why}", {"age_s": round(stale_for)}, now))
        return out


REGISTRY.append(WatchdogWatcher)
