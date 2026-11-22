import asyncio
import unittest

from home_handler import evening
from home_handler.models import Observation
from tests.helpers import FakeClock, FakeHA, make_ctx


def ctx_with(auto_stay="off"):
    pushes = []

    def notify(body):
        pushes.append(body)
        return []
    ha = FakeHA(rest={"/api/states/input_boolean.auto_stay": {"state": auto_stay},
                      "/api/services/script/notify_phone": notify})
    ctx = make_ctx(ha=ha, clock=FakeClock())
    return ctx, pushes


def add_incident(ctx):
    ctx.journal.add_observation(Observation("space", "space:free_gb:nas_user", 2, "NAS array full by Sat 26 Dec",
                                            {}, ctx.now()))


class EveningTest(unittest.TestCase):
    def test_evening_quiet(self):
        ctx, pushes = ctx_with()
        self.assertEqual(asyncio.run(evening.run(ctx, "2026-11-21", ask=lambda *a: {"push": "x"})), "quiet")
        self.assertEqual(pushes, [])

    def test_evening_held_in_party_mode(self):
        ctx, pushes = ctx_with("on")
        add_incident(ctx)
        self.assertEqual(asyncio.run(evening.run(ctx, "2026-11-21", ask=lambda *a: {"push": "x"})), "held")
        self.assertEqual(pushes, [])

    def test_evening_sent_once_per_day(self):
        ctx, pushes = ctx_with()
        add_incident(ctx)
        reply = {"push": "1 thing: NAS full by 26 Dec", "headline": "One thing", "sections": [
            {"title": "Spotted", "items": ["NAS array full by Sat 26 Dec"]}]}
        self.assertEqual(asyncio.run(evening.run(ctx, "2026-11-21", ask=lambda *a: reply)), "sent")
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["message"], reply["push"])
        self.assertEqual(asyncio.run(evening.run(ctx, "2026-11-21", ask=lambda *a: reply)), "done")
        self.assertEqual(len(pushes), 1)

    def test_evening_fallback_when_brain_none(self):
        ctx, pushes = ctx_with()
        add_incident(ctx)
        self.assertEqual(asyncio.run(evening.run(ctx, "2026-11-21", ask=lambda *a: None)), "fallback")
        s = ctx.journal.latest_summary()
        self.assertIn("plain version", s["headline"] + " ".join(x for sec in s["sections"] for x in sec["items"]))
        self.assertEqual(len(pushes), 1)

    def test_push_text_has_no_em_dash(self):
        ctx, pushes = ctx_with()
        add_incident(ctx)
        asyncio.run(evening.run(ctx, "2026-11-21", ask=lambda *a: {"push": "NAS — full soon", "headline": "h",
                                                                   "sections": []}))
        self.assertNotIn("—", pushes[0]["message"])


if __name__ == "__main__":
    unittest.main()
