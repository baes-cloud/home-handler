import unittest

from home_handler import config, replay

DAY = 86400.0
CFG = config.load("/nonexistent")


class ReplayTest(unittest.TestCase):
    def test_replay_devices_from_history(self):
        t0 = 1_700_000_000.0
        hist = {"d0": [(t0, "on"), (t0 + 3600, "unavailable"), (t0 + 3900, "on"),
                       (t0 + DAY, "unavailable"), (t0 + DAY + 400, "on"),
                       (t0 + 2 * DAY, "unknown"), (t0 + 2 * DAY + 500, "on")],
                "d1": [(t0, "on"), (t0 + 3600, "unavailable"), (t0 + 3650, "on")]}
        out = replay.device_findings(hist, {"d0": "Kitchen switch", "d1": "Lamp"}, CFG, t0 + 9 * DAY)
        self.assertEqual([f[0] for f in out], ["devices:d0"])
        self.assertIn("Kitchen switch", out[0][1])

    def test_dead_whole_window_grouped(self):
        t0 = 1_700_000_000.0
        hist = {f"d{i}": [(t0, "unavailable")] for i in range(5)}
        out = replay.device_findings(hist, {}, CFG, t0 + 10 * DAY)
        self.assertEqual([f[0] for f in out], ["devices:long_dead"])

    def test_blip_days(self):
        lines = ["2026-11-20T06:26:04+11:00 failover-host watchdog.sh[1]: primary health check failing",
                 "2026-11-20T09:01:04+11:00 failover-host watchdog.sh[1]: primary health check failing",
                 "2026-11-21T06:49:07+11:00 failover-host watchdog.sh[1]: primary health check failing",
                 "2026-11-21T06:53:00+11:00 failover-host watchdog.sh[1]: primary healthy again after 233s"]
        out = replay.blip_findings("\n".join(lines))
        self.assertEqual([f[0] for f in out], ["ha_health:failover_blips:2026-11-20"])

    def test_backup_gaps(self):
        dates = ["2026-11-18T18:00:00+00:00", "2026-11-19T18:00:00+00:00", "2026-11-21T18:00:00+00:00"]
        out = replay.backup_findings(dates)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
