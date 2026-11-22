import asyncio
import unittest
from datetime import datetime, timezone

from home_handler.watchers.devices import DevicesWatcher, battery_days_to, episodes, primary_entities
from tests.helpers import FakeClock, FakeHA, make_ctx

DAY = 86400.0


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def registries(n):
    dev = [{"id": f"d{i}", "name": f"Lamp {i}", "name_by_user": None, "disabled_by": None,
            "identifiers": [["mqtt", f"zigbee2mqtt_0x{i:016x}"]]} for i in range(n)]
    ent = [{"entity_id": f"light.lamp_{i}", "device_id": f"d{i}", "entity_category": None, "disabled_by": None}
           for i in range(n)]
    ent.append({"entity_id": "sensor.lamp_0_lqi", "device_id": "d0", "entity_category": "diagnostic",
                "disabled_by": None})
    return ent, dev


def ev(entity, old, new, ts):
    return {"event_type": "state_changed", "time_fired": iso(ts),
            "data": {"entity_id": entity, "old_state": {"state": old}, "new_state": {"state": new}}}


def setup(n=1, states=None, clock=None):
    ent, dev = registries(n)
    clock = clock or FakeClock()
    sts = states if states is not None else [
        {"entity_id": f"light.lamp_{i}", "state": "on", "last_changed": iso(clock.t - DAY), "attributes": {}}
        for i in range(n)]
    ha = FakeHA(ws={"config/entity_registry/list": ent, "config/device_registry/list": dev, "get_states": sts})
    ctx = make_ctx(ha=ha, clock=clock, ssh=lambda h, c, timeout=30: (0, "", ""))
    w = DevicesWatcher()
    asyncio.run(w.check(ctx))  # first start: loads registries and baseline
    return w, ctx, clock


class HelpersTest(unittest.TestCase):
    def test_primary_prefers_non_diagnostic(self):
        ent, dev = registries(1)
        self.assertEqual(primary_entities(ent, dev), {"d0": "light.lamp_0"})

    def test_episodes(self):
        tr = [(0, "on"), (10, "unavailable"), (100, "on"), (200, "unknown")]
        self.assertEqual(episodes(tr), [(10, 100), (200, None)])

    def test_battery_projection(self):
        pts = [(i * DAY, 40 - 2 * i) for i in range(10)]  # 22% now, -2/day
        self.assertAlmostEqual(battery_days_to(pts, 15), 3.5, places=1)
        self.assertIsNone(battery_days_to([(i * DAY, 80) for i in range(10)], 15))


class DevicesTest(unittest.TestCase):
    def drop(self, w, ctx, clock, secs):
        w.on_event(ev("light.lamp_0", "on", "unavailable", clock.t), ctx)
        clock.t += secs
        w.on_event(ev("light.lamp_0", "unavailable", "on", clock.t), ctx)
        clock.t += 3600

    def test_three_episodes_flags(self):
        w, ctx, clock = setup()
        for _ in range(3):
            self.drop(w, ctx, clock, 300)
        out = asyncio.run(w.check(ctx))
        self.assertIn("devices:d0", [o.fingerprint for o in out])

    def test_short_blips_ignored(self):
        w, ctx, clock = setup()
        for _ in range(5):
            self.drop(w, ctx, clock, 90)
        self.assertNotIn("devices:d0", [o.fingerprint for o in asyncio.run(w.check(ctx))])

    def test_long_dead_grouped(self):
        clock = FakeClock()
        states = [{"entity_id": f"light.lamp_{i}", "state": "unavailable", "last_changed": iso(clock.t - 10 * DAY),
                   "attributes": {}} for i in range(50)]
        w, ctx, clock = setup(50, states, clock)
        incs = ctx.journal.incidents()
        self.assertEqual([i.fingerprint for i in incs], ["devices:long_dead"])
        self.assertEqual(len(incs[0].detail["devices"]), 50)
        # later checks only re-confirm the one grouped incident (so it auto-closes once they're all back)
        self.assertEqual([o.fingerprint for o in asyncio.run(w.check(ctx))], ["devices:long_dead"])

    def test_long_dead_returns_then_drops(self):
        clock = FakeClock()
        states = [{"entity_id": "light.lamp_0", "state": "unavailable", "last_changed": iso(clock.t - 10 * DAY),
                   "attributes": {}}]
        w, ctx, clock = setup(1, states, clock)
        w.on_event(ev("light.lamp_0", "unavailable", "on", clock.t), ctx)
        clock.t += 60
        for _ in range(3):
            self.drop(w, ctx, clock, 300)
        self.assertIn("devices:d0", [o.fingerprint for o in asyncio.run(w.check(ctx))])

    def test_router_is_urgent(self):
        w, ctx, clock = setup()
        w.routers = {"d0"}
        for _ in range(3):
            self.drop(w, ctx, clock, 300)
        out = [o for o in asyncio.run(w.check(ctx)) if o.fingerprint == "devices:d0"]
        self.assertEqual(out[0].severity, 3)


if __name__ == "__main__":
    unittest.main()
