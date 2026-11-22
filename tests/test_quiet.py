import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from home_handler.watchers import quiet
from home_handler.watchers.quiet import Job, QuietWatcher, Result, parse_kometa, parse_smart
from tests.helpers import FakeClock, make_ctx

H = 3600.0


def run(jobs, clock=None):
    w = QuietWatcher(jobs=jobs)
    ctx = make_ctx(clock=clock or FakeClock())
    return {o.fingerprint: o for o in asyncio.run(w.check(ctx))}, ctx


class QuietTest(unittest.TestCase):
    def test_overdue_backup_flags(self):
        clock = FakeClock()
        out, _ = run([Job("ha_backup", 26 * H, lambda ctx: Result(last=clock.t - 30 * H))], clock)
        self.assertEqual(out["quiet:ha_backup"].severity, 2)

    def test_three_times_overdue_is_urgent(self):
        clock = FakeClock()
        out, _ = run([Job("ha_backup", 26 * H, lambda ctx: Result(last=clock.t - 80 * H))], clock)
        self.assertEqual(out["quiet:ha_backup"].severity, 3)

    def test_recent_is_silent(self):
        clock = FakeClock()
        out, _ = run([Job("ha_backup", 26 * H, lambda ctx: Result(last=clock.t - H))], clock)
        self.assertEqual(out, {})

    def test_nas_backup_failed_result_flags_even_if_recent(self):
        status = {"hosts": {"failover-host": {"nas_backup": "failed|2026-11-21T03:37:00+11:00"}}}
        ctx = make_ctx(status=lambda: (status, 1.0))
        r = quiet.probe_nas_backup(ctx)
        self.assertTrue(r.failed)
        clock = FakeClock()
        out, _ = run([Job("nas_backup", 26 * H, lambda c: Result(last=clock.t - H, failed=True))], clock)
        self.assertIn("quiet:nas_backup", out)

    def test_kometa_parse(self):
        ts = parse_kometa("Finished: 04:47:36 2026-11-21")
        expect = datetime(2026, 11, 21, 4, 47, 36, tzinfo=ZoneInfo("Australia/Melbourne")).timestamp()
        self.assertEqual(ts, expect)
        self.assertIsNone(parse_kometa(""))

    def test_radar_replay_only_when_home(self):
        out, _ = run([Job("radar_replay", 1800, lambda ctx: Result(skip=True))])
        self.assertEqual(out, {})

    def test_probe_none_is_severity2_unknown(self):
        out, _ = run([Job("kometa", 30 * H, lambda ctx: Result(last=None))])
        self.assertEqual(out["quiet:kometa"].severity, 2)

    def test_crashing_probe_is_contained(self):
        def bad(ctx):
            raise OSError("ssh broke")
        clock = FakeClock()
        out, _ = run([Job("kometa", 30 * H, bad), Job("ha_backup", 26 * H, lambda c: Result(last=clock.t - 30 * H))],
                     clock)
        self.assertIn("quiet:ha_backup", out)
        self.assertEqual(out["quiet:kometa"].severity, 2)

    def test_parse_smart(self):
        text = ("== /dev/sdc\n  9 Power_On_Hours 0x0032 075 075 000 Old_age Always - 22367\n"
                "# 1  Extended offline    Completed without error       00%     20000         -\n"
                "== /dev/sdd\n  9 Power_On_Hours 0x0032 084 084 000 Old_age Always - 14313\n"
                "No self-tests have been logged.\n"
                "== /dev/sdf\n  9 Power_On_Hours 0x0032 075 075 000 Old_age Always - 22367\n"
                "# 1  Extended offline    Completed: read failure       90%     22300         -\n")
        disks, failed = parse_smart(text)
        self.assertEqual(disks["/dev/sdc"], 2367)
        self.assertIsNone(disks["/dev/sdd"])
        self.assertEqual(failed, ["/dev/sdf"])


if __name__ == "__main__":
    unittest.main()
