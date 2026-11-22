import unittest

from home_handler import commands, web
from home_handler.models import Observation
from tests.helpers import make_ctx
from tests.test_loose_ends import FakeStore


class CommandAuthTest(unittest.TestCase):
    def test_token_lan_only_not_tunnel(self):
        t = "s3cret"
        self.assertTrue(web.cmd_allowed("10.0.0.12", "Bearer s3cret", t, {"10.0.0.10"}))
        self.assertFalse(web.cmd_allowed("10.0.0.10", "Bearer s3cret", t, {"10.0.0.10"}))   # tunnel
        self.assertFalse(web.cmd_allowed("8.8.8.8", "Bearer s3cret", t, {"10.0.0.10"}))   # not a private address
        self.assertFalse(web.cmd_allowed("10.0.0.12", "Bearer nope", t, {"10.0.0.10"}))
        self.assertFalse(web.cmd_allowed("10.0.0.12", "Bearer s3cret", "", {"10.0.0.10"}))  # no token set
        self.assertTrue(web.cmd_allowed("127.0.0.1", None, "", set()))


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_help_and_status(self):
        self.assertEqual(commands.run("help", self.ctx.journal, self.ctx.cfg)["kind"], "fixed")
        r = commands.run("status", self.ctx.journal, self.ctx.cfg)
        self.assertIn("waiting on you 0", r["lines"][0])

    def test_free_text_goes_to_brain_read_only(self):
        seen = {}
        def ask(prompt, schema, kind, journal, cfg, timeout=0):
            seen["kind"], seen["prompt"] = kind, prompt
            return {"lines": ["all quiet"]}
        r = commands.run("anything up?", self.ctx.journal, self.ctx.cfg, ask=ask, state_fn=web.state)
        self.assertEqual(r, {"lines": ["all quiet"], "kind": "ask"})
        self.assertEqual(seen["kind"], "ask")
        self.assertIn("Question: anything up?", seen["prompt"])

    def test_brain_unavailable(self):
        r = commands.run("hello", self.ctx.journal, self.ctx.cfg, ask=lambda *a, **k: None)
        self.assertEqual(r["kind"], "error")

    def test_done_unknown(self):
        r = commands.run("done 999", self.ctx.journal, self.ctx.cfg, store=FakeStore())
        self.assertEqual(r["kind"], "error")


class ServiceTokenTest(unittest.TestCase):
    def test_rejects_missing_or_bad(self):
        self.assertFalse(web.service_token_ok(None, "aud", "id"))
        self.assertFalse(web.service_token_ok("not-a-jwt", "aud", "id"))
        self.assertFalse(web.service_token_ok("x", "aud", ""))
