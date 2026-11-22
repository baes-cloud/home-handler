import json
import threading
import unittest
import urllib.request

from home_handler import web
from home_handler.models import Observation
from tests.helpers import make_ctx
from tests.test_loose_ends import FakeStore


class AccessTest(unittest.TestCase):
    def test_state_requires_auth_from_lan_ip(self):
        ok = lambda token, aud: "owner@example.com" if token == "good" else None
        self.assertFalse(web.allowed("10.0.0.10", None, {"10.0.0.10"}, "aud", ok))
        self.assertTrue(web.allowed("10.0.0.10", "good", {"10.0.0.10"}, "aud", ok))
        self.assertFalse(web.allowed("10.0.0.99", "good", {"10.0.0.10"}, "aud", ok))
        self.assertTrue(web.allowed("127.0.0.1", None, {"10.0.0.10"}, "aud", ok))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.store = FakeStore()
        self.srv = web.make_server(self.ctx.journal, self.ctx.cfg, 0, {"10.0.0.10"}, "", bind="127.0.0.1",
                                   store=self.store, access_fn=lambda t, a: None)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()

    def url(self, p):
        return f"http://127.0.0.1:{self.port}{p}"

    def test_localhost_ok(self):
        with urllib.request.urlopen(self.url("/")) as r:
            self.assertIn(b"Waiting on you", r.read())
        with urllib.request.urlopen(self.url("/api/state")) as r:
            d = json.load(r)
        self.assertEqual(set(d), {"waiting", "open", "today", "watchers", "replay", "history", "counts"})

    def post(self, path, header=True):
        req = urllib.request.Request(self.url(path), data=b"{}", method="POST",
                                     headers={"X-Requested-With": "home_handler"} if header else {})
        return urllib.request.urlopen(req)

    def test_done_resolves_loose_end(self):
        iid, _ = self.ctx.journal.upsert_external("loose_ends", "abc", "Thing", "open", {})
        with self.post(f"/api/incident/{iid}/done") as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.store.set, [("abc", True, "you")])
        self.assertEqual(self.ctx.journal.get(iid).status, "done")

    def test_reopen_watcher_incident(self):
        iid = self.ctx.journal.add_observation(Observation("space", "space:x", 2, "t", {}, 1.0))
        self.ctx.journal.set_status(iid, "done", "x", "you")
        with self.post(f"/api/incident/{iid}/reopen") as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.ctx.journal.get(iid).status, "open")
        self.assertEqual(self.store.set, [])

    def test_post_needs_header(self):
        iid, _ = self.ctx.journal.upsert_external("loose_ends", "abc", "Thing", "open", {})
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(f"/api/incident/{iid}/done", header=False)
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
