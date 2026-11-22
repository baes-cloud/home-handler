import asyncio
import unittest

from home_handler.watchers.base import Watcher
from home_handler.watchers.watchdog import WatchdogWatcher
from tests.helpers import FakeClock, make_ctx


class Slow(Watcher):
    name = "slow"
    interval_s = 300
    expect_s = 600


class WatchdogTest(unittest.TestCase):
    def test_watchdog_flags_silent(self):
        clock = FakeClock()
        ctx = make_ctx(status={"generated": "x"}, clock=clock)
        ctx.started = clock.t - 5000
        ctx.watchers = [Slow()]
        ctx.journal.record_watcher_run("slow", True, 0, ts=clock.t - 700)
        out = asyncio.run(WatchdogWatcher().check(ctx))
        self.assertIn("watchdog:slow", [o.fingerprint for o in out])

    def test_recent_ok_is_silent(self):
        clock = FakeClock()
        ctx = make_ctx(status={"generated": "x"}, clock=clock)
        ctx.started = clock.t - 5000
        ctx.watchers = [Slow()]
        ctx.journal.record_watcher_run("slow", True, 0, ts=clock.t - 100)
        self.assertEqual(asyncio.run(WatchdogWatcher().check(ctx)), [])

    def test_errors_flag(self):
        clock = FakeClock()
        ctx = make_ctx(status={"generated": "x"}, clock=clock)
        ctx.started = clock.t - 5000
        ctx.watchers = [Slow()]
        ctx.journal.record_watcher_run("slow", True, 0, ts=clock.t - 100)
        for k in range(3):
            ctx.journal.record_watcher_run("slow", False, 0, error="x", ts=clock.t - 60 * k)
        self.assertIn("watchdog:slow", [o.fingerprint for o in asyncio.run(WatchdogWatcher().check(ctx))])

    def test_watchdog_stale_status(self):
        clock = FakeClock()
        ctx = make_ctx(status=lambda: ({"generated": "x"}, 1000.0), clock=clock)
        ctx.started = clock.t
        ctx.watchers = []
        out = asyncio.run(WatchdogWatcher().check(ctx))
        self.assertEqual([o.fingerprint for o in out], ["watchdog:status_json"])

    def test_missing_status_flags(self):
        ctx = make_ctx(status=None)
        ctx.started = ctx.now() - 1000  # missing for longer than 15 min since the agent started
        ctx.watchers = []
        self.assertEqual([o.fingerprint for o in asyncio.run(WatchdogWatcher().check(ctx))], ["watchdog:status_json"])


if __name__ == "__main__":
    unittest.main()
