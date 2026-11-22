"""Tests for the final-review findings (each failed before its fix)."""
import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from home_handler import brain, evening, loose_ends, sources
from home_handler.models import Observation
from home_handler.runner import Runner
from home_handler.watchers import quiet
from home_handler.watchers.base import Watcher
from home_handler.watchers.devices import DevicesWatcher
from home_handler.watchers.ha_health import HAHealthWatcher
from home_handler.watchers.space import SpaceWatcher
from home_handler.watchers.watchdog import WatchdogWatcher
from tests.helpers import FakeClock, FakeHA, make_ctx
from tests.test_devices import ev, iso, registries
from tests.test_loose_ends import ITEMS, FakeStore

DAY = 86400.0
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "status.json")


def dev_setup(states, clock):
    ent, dev = registries(len(states))
    ha = FakeHA(ws={"config/entity_registry/list": ent, "config/device_registry/list": dev,
                    "get_states": lambda kw: states})
    ctx = make_ctx(ha=ha, clock=clock, ssh=lambda h, c, timeout=30: (0, "", ""))
    w = DevicesWatcher()
    asyncio.run(w.check(ctx))
    return w, ctx


class F1BaselineTest(unittest.TestCase):
    def test_recent_last_changed_still_long_dead(self):
        """HA resets last_changed on restart: every device down at first start is baselined, not alarmed."""
        clock = FakeClock()
        states = [{"entity_id": f"light.lamp_{i}", "state": "unavailable", "last_changed": iso(clock.t - 2 * DAY),
                   "attributes": {}} for i in range(14)]
        w, ctx = dev_setup(states, clock)
        self.assertEqual([i.fingerprint for i in ctx.journal.incidents()], ["devices:long_dead"])
        clock.t += 3600
        self.assertNotIn("devices:d0", [o.fingerprint for o in asyncio.run(w.check(ctx))])


class F2ReconcileTest(unittest.TestCase):
    def test_none_old_state_closes_open_spell(self):
        clock = FakeClock()
        states = [{"entity_id": "light.lamp_0", "state": "on", "last_changed": iso(clock.t - DAY), "attributes": {}}]
        w, ctx = dev_setup(states, clock)
        w.on_event(ev("light.lamp_0", "on", "unavailable", clock.t), ctx)
        clock.t += 600
        restart = {"event_type": "state_changed", "time_fired": iso(clock.t),
                   "data": {"entity_id": "light.lamp_0", "old_state": None, "new_state": {"state": "on"}}}
        w.on_event(restart, ctx)
        self.assertNotIn("d0", w.open)

    def test_none_to_unavailable_not_counted(self):
        clock = FakeClock()
        states = [{"entity_id": "light.lamp_0", "state": "on", "last_changed": iso(clock.t - DAY), "attributes": {}}]
        w, ctx = dev_setup(states, clock)
        startup = {"event_type": "state_changed", "time_fired": iso(clock.t),
                   "data": {"entity_id": "light.lamp_0", "old_state": None, "new_state": {"state": "unavailable"}}}
        w.on_event(startup, ctx)
        self.assertNotIn("d0", w.open)

    def test_reconcile_adds_device_that_went_down_unseen(self):
        clock = FakeClock()
        states = [{"entity_id": "light.lamp_0", "state": "on", "last_changed": iso(clock.t - DAY), "attributes": {}}]
        w, ctx = dev_setup(states, clock)
        clock.t += 8 * 3600
        states[0] = {"entity_id": "light.lamp_0", "state": "unavailable", "last_changed": iso(clock.t - 7 * 3600),
                     "attributes": {}}
        self.assertIn("devices:d0", [o.fingerprint for o in asyncio.run(w.check(ctx))])

    def test_reconcile_closes_spell_for_device_up_now(self):
        clock = FakeClock()
        states = [{"entity_id": "light.lamp_0", "state": "on", "last_changed": iso(clock.t - DAY), "attributes": {}}]
        w, ctx = dev_setup(states, clock)
        w.on_event(ev("light.lamp_0", "on", "unavailable", clock.t), ctx)
        clock.t += 7 * 3600  # came back during a websocket outage
        states[0] = {"entity_id": "light.lamp_0", "state": "on", "last_changed": iso(clock.t - 6.5 * 3600),
                     "attributes": {}}
        out = asyncio.run(w.check(ctx))
        self.assertNotIn("d0", w.open)
        self.assertFalse(any("offline for" in o.title and o.detail.get("offline_now") for o in out))


def eve_ctx(push_ok=True, party=lambda: {"state": "off"}):
    pushes = []

    def notify(body):
        if not push_ok:
            raise ConnectionError("HA down")
        pushes.append(body)
    ha = FakeHA(rest={"/api/states/input_boolean.auto_stay": lambda b: party(),
                      "/api/services/script/notify_phone": notify})
    return make_ctx(ha=ha, clock=FakeClock()), pushes


REPLY = {"push": "p", "headline": "h", "sections": []}


class F3QuietEveningTest(unittest.TestCase):
    def test_standing_condition_does_not_push_twice(self):
        ctx, pushes = eve_ctx()
        ctx.journal.add_observation(Observation("devices", "battery:x", 2, "Battery at 2%", {}, ctx.now()))
        self.assertEqual(asyncio.run(evening.run(ctx, "d1", ask=lambda *a: REPLY)), "sent")
        ctx._clock.t += DAY
        ctx.journal.add_observation(Observation("devices", "battery:x", 2, "Battery at 2%", {}, ctx.now()))
        self.assertEqual(asyncio.run(evening.run(ctx, "d2", ask=lambda *a: REPLY)), "quiet")
        self.assertEqual(len(pushes), 1)

    def test_escalation_pushes_again(self):
        ctx, pushes = eve_ctx()
        ctx.journal.add_observation(Observation("space", "space:x", 2, "full in 20 days", {}, ctx.now()))
        asyncio.run(evening.run(ctx, "d1", ask=lambda *a: REPLY))
        ctx._clock.t += DAY
        ctx.journal.add_observation(Observation("space", "space:x", 3, "full in 5 days", {}, ctx.now()))
        self.assertEqual(asyncio.run(evening.run(ctx, "d2", ask=lambda *a: REPLY)), "sent")

    def test_notice_only_is_quiet(self):
        ctx, pushes = eve_ctx()
        ctx.journal.add_observation(Observation("ha_health", "ha_health:ws_drop:2026-11-21", 1, "drop", {}, ctx.now()))
        self.assertEqual(asyncio.run(evening.run(ctx, "d1", ask=lambda *a: REPLY)), "quiet")


class F4AutoCloseTest(unittest.TestCase):
    class Flip(Watcher):
        name = "flip"
        interval_s = 60
        bad = True

        async def check(self, ctx):
            return [Observation("flip", "flip:1", 2, "bad", {}, ctx.now())] if self.bad else []

    def test_incident_closes_when_condition_clears(self):
        ctx = make_ctx()
        w = self.Flip()
        r = Runner(ctx, [w])
        asyncio.run(r.tick(force=True))
        w.bad = False
        asyncio.run(r.tick(force=True))
        inc = ctx.journal.incidents()[0]
        self.assertEqual(inc.status, "done")
        self.assertEqual(inc.notes[-1]["by"], "agent")

    def test_failed_run_does_not_close(self):
        class Boom(Watcher):
            name = "flip"
            interval_s = 60

            async def check(self, ctx):
                raise RuntimeError("x")
        ctx = make_ctx()
        ctx.journal.add_observation(Observation("flip", "flip:1", 2, "bad", {}, ctx.now()))
        asyncio.run(Runner(ctx, [Boom()]).tick(force=True))
        self.assertEqual(ctx.journal.incidents()[0].status, "open")

    def test_todays_ws_drop_stays_open_old_days_close(self):
        clock = FakeClock(datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp())
        ha = FakeHA(ws={"repairs/list_issues": {"issues": []}, "config_entries/get": []})
        ctx = make_ctx(status=lambda: ({"hosts": {}, "ha": {}}, 1.0), ha=ha, clock=clock)
        old = ctx.journal.add_observation(Observation("ha_health", "ha_health:ws_drop:2026-11-20", 1, "d", {}, clock.t))
        new = ctx.journal.add_observation(Observation("ha_health", "ha_health:ws_drop:2026-11-21", 1, "d", {}, clock.t))
        asyncio.run(Runner(ctx, [HAHealthWatcher()]).tick(force=True))
        self.assertEqual(ctx.journal.get(old).status, "done")
        self.assertEqual(ctx.journal.get(new).status, "open")


class F5PushFailureTest(unittest.TestCase):
    def test_failed_push_retries_later(self):
        ctx, pushes = eve_ctx(push_ok=False)
        ctx.journal.add_observation(Observation("space", "space:x", 2, "full soon", {}, ctx.now()))
        self.assertEqual(asyncio.run(evening.run(ctx, "d1", ask=lambda *a: REPLY)), "push_failed")
        self.assertIsNone(ctx.journal.get_summary("d1"))

    def test_party_check_unreachable_holds(self):
        def boom():
            raise ConnectionError("HA down")
        ctx, pushes = eve_ctx(party=boom)
        ctx.journal.add_observation(Observation("space", "space:x", 2, "full soon", {}, ctx.now()))
        self.assertEqual(asyncio.run(evening.run(ctx, "d1", ask=lambda *a: REPLY)), "held")


class F6StatusReadTest(unittest.TestCase):
    def test_one_bad_read_after_good_is_silent(self):
        clock = FakeClock()
        reads = [({"generated": "x"}, 60.0), (None, None)]
        ctx = make_ctx(status=lambda: reads.pop(0), clock=clock)
        ctx.started = clock.t - 5000
        ctx.watchers = []
        asyncio.run(WatchdogWatcher().check(ctx))
        clock.t += 300
        self.assertEqual(asyncio.run(WatchdogWatcher().check(ctx)), [])

    def test_quiet_backup_probes_skip_without_status(self):
        ctx = make_ctx(status=None)
        self.assertTrue(quiet.probe_ha_backup(ctx).skip)
        self.assertTrue(quiet.probe_nas_backup(ctx).skip)


class F7NonBlockingTest(unittest.TestCase):
    def test_space_ssh_runs_off_the_event_loop(self):
        seen = []

        def ssh(h, c, timeout=30):
            seen.append(threading.current_thread() is threading.main_thread())
            return 1, "", ""
        ctx = make_ctx(status=lambda: ({"hosts": {}, "ha": {}}, 1.0), ssh=ssh)
        asyncio.run(SpaceWatcher().check(ctx))
        self.assertEqual(seen, [False])

    def test_devices_ssh_runs_off_the_event_loop(self):
        seen = []

        def ssh(h, c, timeout=30):
            seen.append(threading.current_thread() is threading.main_thread())
            return 1, "", ""
        ent, dev = registries(1)
        ha = FakeHA(ws={"config/entity_registry/list": ent, "config/device_registry/list": dev, "get_states": []})
        ctx = make_ctx(ha=ha, ssh=ssh)
        asyncio.run(DevicesWatcher().check(ctx))
        self.assertEqual(seen, [False])


class F8SortPreviewTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "data.json"), "w") as f:
            json.dump({"items": ITEMS, "meta": {}}, f)
        self.ctx = make_ctx()
        self.ctx.cfg["loose_ends"]["dir"] = self.dir
        loose_ends.sync(self.ctx.journal, self.ctx.cfg)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_apply_uses_saved_preview_and_backs_up(self):
        answers = iter([{"items": [{"ext_id": "b", "label": "stale", "reason": "first"}]},
                        {"items": [{"ext_id": "a", "label": "stale", "reason": "second"}]}])
        ask = lambda *a: next(answers)
        loose_ends.sort_open(self.ctx.journal, self.ctx.cfg, ask=ask, dry_run=True)
        store = FakeStore()
        counts = loose_ends.apply_proposal(self.ctx.journal, self.ctx.cfg, store=store)
        self.assertEqual(counts, {"stale": 1})
        self.assertEqual([r[0] for r in store.resolved], ["b"])
        self.assertTrue(any(n.startswith("data.json.bak-home-handler-") for n in os.listdir(self.dir)))

    def test_apply_without_preview_refuses(self):
        self.assertIsNone(loose_ends.apply_proposal(self.ctx.journal, self.ctx.cfg, store=FakeStore()))


class F9ClaudePathTest(unittest.TestCase):
    def test_claude_called_by_absolute_path(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"structured_output": {"push": "x"}}),
                                               stderr="")
        ctx = make_ctx()
        brain.ask_json("p", {"type": "object"}, "evening", ctx.journal, ctx.cfg, runner=run)
        self.assertTrue(os.path.isabs(calls[0][0]))

    def test_units_set_path(self):
        here = os.path.join(os.path.dirname(__file__), "..", "deploy")
        for u in ("home-handler-evening.service", "home-handler-watch.service"):
            with open(os.path.join(here, u)) as f:
                self.assertIn("Environment=PATH=%h/.local/bin", f.read())


class F12FixtureTest(unittest.TestCase):
    def test_fixture_has_no_security_map(self):
        with open(FIX) as f:
            text = f.read()
        data = json.loads(text)
        for k in ("cloudflare", "security", "external", "services"):
            self.assertNotIn(k, data)
        self.assertNotIn("@", text)


if __name__ == "__main__":
    unittest.main()
