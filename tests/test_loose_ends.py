import json
import os
import shutil
import tempfile
import unittest

from home_handler import loose_ends
from tests.helpers import make_ctx

ITEMS = {
    "a": {"title": "Replace the NAS RAM", "done": False, "area": "NAS & Media", "priority": 1, "detail": "d"},
    "b": {"title": "Fix subtitles", "done": False, "area": "NAS & Media", "priority": 3, "detail": "d"},
    "c": {"title": "Old thing", "done": True, "area": "PC", "priority": 4, "detail": "d"},
}


class FakeStore:
    def __init__(self):
        self.resolved, self.set = [], []

    def resolve(self, item_id, note):
        self.resolved.append((item_id, note))
        return True

    def set_done(self, item_id, done, by):
        self.set.append((item_id, done, by))
        return True


class LooseEndsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "data.json"), "w") as f:
            json.dump({"items": ITEMS, "meta": {}}, f)
        self.ctx = make_ctx()
        self.ctx.cfg["loose_ends"]["dir"] = self.dir
        self.store = FakeStore()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_sync_idempotent(self):
        self.assertEqual(loose_ends.sync(self.ctx.journal, self.ctx.cfg), 3)
        self.assertEqual(loose_ends.sync(self.ctx.journal, self.ctx.cfg), 0)
        done = [i for i in self.ctx.journal.incidents(source="loose_ends") if i.ext_id == "c"][0]
        self.assertEqual(done.status, "done")

    def test_sync_picks_up_ticks(self):
        loose_ends.sync(self.ctx.journal, self.ctx.cfg)
        ITEMS2 = dict(ITEMS, a=dict(ITEMS["a"], done=True))
        with open(os.path.join(self.dir, "data.json"), "w") as f:
            json.dump({"items": ITEMS2, "meta": {}}, f)
        loose_ends.sync(self.ctx.journal, self.ctx.cfg)
        a = [i for i in self.ctx.journal.incidents(source="loose_ends") if i.ext_id == "a"][0]
        self.assertEqual(a.status, "done")

    def test_stale_closes_and_resolves(self):
        loose_ends.sync(self.ctx.journal, self.ctx.cfg)

        def ask(prompt, schema, kind, journal, cfg):
            return {"items": [{"ext_id": "a", "label": "needs_bae", "reason": "needs hands"},
                              {"ext_id": "b", "label": "stale", "reason": "done already"}]}
        counts = loose_ends.sort_open(self.ctx.journal, self.ctx.cfg, ask=ask, store=self.store)
        self.assertEqual(counts, {"needs_bae": 1, "stale": 1})
        self.assertEqual(self.store.resolved, [("b", "closed by house agent: done already")])
        b = [i for i in self.ctx.journal.incidents(source="loose_ends") if i.ext_id == "b"][0]
        self.assertEqual(b.status, "closed")

    def test_dry_run_changes_nothing(self):
        loose_ends.sync(self.ctx.journal, self.ctx.cfg)

        def ask(prompt, schema, kind, journal, cfg):
            return {"items": [{"ext_id": "b", "label": "stale", "reason": "x"}]}
        loose_ends.sort_open(self.ctx.journal, self.ctx.cfg, ask=ask, store=self.store, dry_run=True)
        self.assertEqual(self.store.resolved, [])
        self.assertTrue(all(i.sort_label is None for i in self.ctx.journal.incidents(source="loose_ends")))

    def test_brain_unavailable_sorts_nothing(self):
        loose_ends.sync(self.ctx.journal, self.ctx.cfg)
        counts = loose_ends.sort_open(self.ctx.journal, self.ctx.cfg, ask=lambda *a: None, store=self.store)
        self.assertEqual(counts, {})


if __name__ == "__main__":
    unittest.main()
