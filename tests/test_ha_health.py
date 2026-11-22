import asyncio
import unittest
from datetime import datetime, timezone

from home_handler.watchers.ha_health import HAHealthWatcher, restart_seconds
from tests.helpers import FakeClock, FakeHA, make_ctx


def status(blips=0):
    return {"hosts": {"failover-host": {"failover_blips_24h": blips, "failover_last": "primary healthy again after 62s"}},
            "ha": {"core": {"version": "1", "latest": "1"}, "os": {"version": "1", "latest": "1"},
                   "supervisor": {"version": "1", "latest": "1"}}}


def ctx_with(blips=0, issues=None, entries=None, clock=None):
    ha = FakeHA(ws={"repairs/list_issues": {"issues": issues or []}, "config_entries/get": entries or []})
    return make_ctx(status=lambda: (status(blips), 10.0), ha=ha, clock=clock or FakeClock())


def fps(out):
    return [o.fingerprint for o in out]


class HAHealthTest(unittest.TestCase):
    def test_blips_threshold(self):
        self.assertNotIn("ha_health:failover_blips", fps(asyncio.run(HAHealthWatcher().check(ctx_with(1)))))
        self.assertIn("ha_health:failover_blips", fps(asyncio.run(HAHealthWatcher().check(ctx_with(2)))))

    def test_restart_slow(self):
        self.assertEqual(restart_seconds(100.0, 350.0), 250.0)
        for secs, expect in ((250, True), (150, False)):
            clock = FakeClock()
            ctx = ctx_with(clock=clock)
            ctx.last_ws_drop = clock.t - secs
            started = {"event_type": "homeassistant_started",
                       "time_fired": datetime.fromtimestamp(clock.t, timezone.utc).isoformat(), "data": {}}
            out = HAHealthWatcher().on_event(started, ctx)
            self.assertEqual("ha_health:restart_slow" in fps(out), expect)

    def test_long_outage_counts_as_slow_restart(self):
        from home_handler.runner import Runner
        clock = FakeClock()
        ctx = ctx_with(clock=clock)
        r = Runner(ctx, [])
        r.on_disconnect(clock.t)
        clock.t += 300
        r.on_connect(clock.t)
        self.assertIn("ha_health:restart_slow", fps(asyncio.run(HAHealthWatcher().check(ctx))))

    def test_entry_retry_needs_30min(self):
        clock = FakeClock()
        entries = [{"entry_id": "e1", "domain": "androidtv_remote", "title": "Bedroom TV", "state": "setup_retry"}]
        ctx = ctx_with(entries=entries, clock=clock)
        w = HAHealthWatcher()
        self.assertNotIn("ha_health:entry:e1", fps(asyncio.run(w.check(ctx))))
        clock.t += 1900
        self.assertIn("ha_health:entry:e1", fps(asyncio.run(w.check(ctx))))

    def test_ignored_repair_skipped(self):
        issues = [{"issue_id": "a", "domain": "roborock", "ignored": True, "dismissed_version": "2026.9.0",
                   "translation_key": "x", "severity": "warning"},
                  {"issue_id": "b", "domain": "hue", "ignored": False, "dismissed_version": None,
                   "translation_key": "y", "severity": "warning"}]
        out = fps(asyncio.run(HAHealthWatcher().check(ctx_with(issues=issues))))
        self.assertIn("ha_health:repair:b", out)
        self.assertNotIn("ha_health:repair:a", out)


if __name__ == "__main__":
    unittest.main()
