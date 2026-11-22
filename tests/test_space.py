import asyncio
import os
import unittest

from home_handler import sources
from home_handler.watchers.space import SpaceWatcher, project_full
from tests.helpers import FakeClock, make_ctx

DAY = 86400.0
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "status.json")


class SpaceTest(unittest.TestCase):
    def test_project_full_linear(self):
        pts = [(i * DAY / 4, 2800 - 25 * i) for i in range(29)]  # 7 days, -100 GB/day
        secs = project_full(pts, 50)
        self.assertAlmostEqual(secs / DAY, 20.5, places=2)

    def test_rising_returns_none(self):
        self.assertIsNone(project_full([(i * 3600.0, 100 + i) for i in range(20)], 50))

    def test_too_few_points_silent(self):
        self.assertIsNone(project_full([(i * 3600.0, 100 - i) for i in range(5)], 50))

    def test_nas_avail_bytes_converted(self):
        data, _ = sources.read_status(FIX, 0)
        ctx = make_ctx(status=lambda: (data, 10.0))
        asyncio.run(SpaceWatcher().check(ctx))
        cache = [d for d in data["hosts"]["nas"]["disks"] if d["mount"] == "/mnt/cache"][0]
        last = ctx.journal.last_sample("free_gb:nas_cache")
        self.assertAlmostEqual(last[1], cache["avail"] / 1e9, places=3)

    def test_nas_fill_flags_warn(self):
        clock = FakeClock()
        status = {"hosts": {"nas": {"disks": [{"mount": "/mnt/user", "avail": 0}]}}, "ha": {}}
        ctx = make_ctx(status=lambda: (status, 1.0), clock=clock)
        for i in range(29):  # 7 days falling 100 GB/day to 2100
            ctx.journal.add_sample("free_gb:nas_user", clock.t - 7 * DAY + i * DAY / 4, 2800 - 25 * i)
        out = SpaceWatcher()._rules(ctx)
        nas = [o for o in out if o.fingerprint == "space:free_gb:nas_user"]
        self.assertEqual(len(nas), 1)
        self.assertEqual(nas[0].severity, 2)

    def test_ma_over_3gb_flags(self):
        clock = FakeClock()
        ctx = make_ctx(status=lambda: ({"hosts": {}, "ha": {}}, 1.0), clock=clock,
                       ssh=lambda h, c, timeout=30: (0, '{"result":"ok","data":{"memory_usage": 3500000000}}', ""))
        out = asyncio.run(SpaceWatcher().check(ctx))
        self.assertIn("space:music_assistant", [o.fingerprint for o in out])


if __name__ == "__main__":
    unittest.main()
