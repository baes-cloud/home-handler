import json
import subprocess
import unittest

from home_handler import brain
from tests.helpers import make_ctx

SCHEMA = {"type": "object", "properties": {"push": {"type": "string"}}, "required": ["push"]}


def fake_run(payload, rc=0):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=json.dumps(payload), stderr="")
    return run, calls


class BrainTest(unittest.TestCase):
    def test_ask_json_respects_cap(self):
        ctx = make_ctx()
        ctx.cfg["caps"]["evening"] = 1
        ctx.journal.record_run("evening", True, {})
        run, calls = fake_run({"structured_output": {"push": "hi"}})
        self.assertIsNone(brain.ask_json("p", SCHEMA, "evening", ctx.journal, ctx.cfg, runner=run))
        self.assertEqual(calls, [])

    def test_structured_output_parsed(self):
        ctx = make_ctx()
        run, calls = fake_run({"is_error": False, "structured_output": {"push": "hi"}})
        self.assertEqual(brain.ask_json("p", SCHEMA, "evening", ctx.journal, ctx.cfg, runner=run), {"push": "hi"})
        cmd = calls[0]
        self.assertTrue(cmd[0].endswith("claude"))
        self.assertEqual(cmd[1], "-p")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(ctx.journal.runs_today("evening"), 1)

    def test_result_text_json_fallback(self):
        ctx = make_ctx()
        run, _ = fake_run({"is_error": False, "result": '{"push": "from text"}'})
        self.assertEqual(brain.ask_json("p", SCHEMA, "evening", ctx.journal, ctx.cfg, runner=run)["push"], "from text")

    def test_ask_json_cli_error_none(self):
        ctx = make_ctx()
        run, _ = fake_run({"is_error": True, "result": "Failed to authenticate"})
        self.assertIsNone(brain.ask_json("p", SCHEMA, "evening", ctx.journal, ctx.cfg, runner=run))
        self.assertEqual(ctx.journal.runs_today("evening"), 1)  # failed attempts still count

    def test_timeout_none(self):
        ctx = make_ctx()

        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertIsNone(brain.ask_json("p", SCHEMA, "evening", ctx.journal, ctx.cfg, runner=run))


if __name__ == "__main__":
    unittest.main()
