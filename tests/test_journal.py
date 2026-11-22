import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from home_handler.journal import Journal
from home_handler.models import Observation

MEL = ZoneInfo("Australia/Melbourne")


def obs(fp="x:1", sev=1, ts=None, title="t"):
    return Observation(watcher="w", fingerprint=fp, severity=sev, title=title, detail={}, ts=ts or time.time())


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.j = Journal(os.path.join(self.dir.name, "j.db"))

    def tearDown(self):
        self.dir.cleanup()

    def test_same_fingerprint_attaches(self):
        a = self.j.add_observation(obs(sev=1))
        b = self.j.add_observation(obs(sev=3))
        self.assertEqual(a, b)
        inc = self.j.get(a)
        self.assertEqual(inc.count, 2)
        self.assertEqual(inc.severity, 3)

    def test_closed_incident_reopens_new(self):
        a = self.j.add_observation(obs())
        self.j.set_status(a, "done", "fixed", "you")
        b = self.j.add_observation(obs())
        self.assertNotEqual(a, b)
        self.assertEqual(self.j.get(a).notes[-1]["by"], "you")

    def test_upsert_external_idempotent(self):
        i1, c1 = self.j.upsert_external("loose_ends", "abc", "Title", "open", {"area": "PC"})
        i2, c2 = self.j.upsert_external("loose_ends", "abc", "Title", "open", {"area": "PC"})
        self.assertEqual(i1, i2)
        self.assertTrue(c1)
        self.assertFalse(c2)

    def test_samples_window(self):
        for t, v in ((100, 1.0), (200, 2.0), (300, 3.0)):
            self.j.add_sample("k", t, v)
        self.assertEqual(self.j.samples("k", 200), [(200.0, 2.0), (300.0, 3.0)])

    def test_runs_today_uses_tz(self):
        now = datetime.now(MEL)
        yesterday_late = (now - timedelta(days=1)).replace(hour=23, minute=30)
        self.j.record_run("evening", True, {}, ts=yesterday_late.timestamp())
        self.assertEqual(self.j.runs_today("evening"), 0)
        self.j.record_run("evening", True, {})
        self.assertEqual(self.j.runs_today("evening"), 1)


if __name__ == "__main__":
    unittest.main()
