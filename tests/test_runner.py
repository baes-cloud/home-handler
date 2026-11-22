import asyncio
import unittest

from home_handler.models import Observation
from home_handler.runner import Runner
from home_handler.watchers.base import Watcher
from tests.helpers import FakeClock, make_ctx


class Boom(Watcher):
    name = "boom"
    interval_s = 60

    async def check(self, ctx):
        raise RuntimeError("kaboom")


class Fine(Watcher):
    name = "fine"
    interval_s = 60

    async def check(self, ctx):
        return [Observation("fine", "fine:1", 1, "ok", {}, ctx.now())]


class RunnerTest(unittest.TestCase):
    def test_crashing_watcher_isolated(self):
        ctx = make_ctx()
        r = Runner(ctx, [Boom(), Fine()])
        asyncio.run(r.tick())
        self.assertEqual(len(ctx.journal.incidents(source="watcher")), 1)
        stats = {s["name"]: s for s in ctx.journal.watcher_stats()}
        self.assertEqual(stats["boom"]["errors"], 1)
        self.assertEqual(stats["fine"]["fires"], 1)

    def test_interval_respected(self):
        clock = FakeClock()
        ctx = make_ctx(clock=clock)
        r = Runner(ctx, [Fine()])
        asyncio.run(r.tick())
        clock.t += 30
        asyncio.run(r.tick())
        self.assertEqual(ctx.journal.watcher_stats()[0]["runs"], 1)
        clock.t += 31
        asyncio.run(r.tick())
        self.assertEqual(ctx.journal.watcher_stats()[0]["runs"], 2)

    def test_ws_drops_group_per_day(self):
        clock = FakeClock()
        ctx = make_ctx(clock=clock)
        r = Runner(ctx, [])
        for _ in range(5):
            r.on_disconnect(clock.t)
            clock.t += 40
            r.on_connect(clock.t)
            clock.t += 600
        incs = [i for i in ctx.journal.incidents() if i.fingerprint.startswith("ha_health:ws_drop:")]
        self.assertEqual(len(incs), 1)
        self.assertEqual(incs[0].count, 5)
        self.assertEqual(len(incs[0].detail["outages"]), 5)
        self.assertEqual(ctx.last_ws_drop, clock.t - 640)

    def test_event_dispatch(self):
        class Ev(Watcher):
            name = "ev"

            def on_event(self, event, ctx):
                return [Observation("ev", "ev:1", 1, "seen", {}, ctx.now())]

        ctx = make_ctx()
        r = Runner(ctx, [Ev()])
        r.handle_event({"event_type": "state_changed", "data": {}})
        self.assertEqual(len(ctx.journal.incidents()), 1)


if __name__ == "__main__":
    unittest.main()
