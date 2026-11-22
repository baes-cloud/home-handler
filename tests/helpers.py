import os
import tempfile

from home_handler import config
from home_handler.journal import Journal
from home_handler.watchers.base import Context


class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeHA:
    """Records calls; answers WS commands and REST paths from dicts."""

    def __init__(self, ws=None, rest=None):
        self.ws = ws or {}
        self.rest_answers = rest or {}
        self.calls = []

    async def call(self, type_, **kw):
        self.calls.append((type_, kw))
        v = self.ws.get(type_)
        return v(kw) if callable(v) else v

    def rest(self, method, path, body=None):
        self.calls.append((method, path, body))
        for k, v in self.rest_answers.items():
            if path.startswith(k):
                return v(body) if callable(v) else v
        return None


def make_ctx(status=None, ha=None, ssh=None, clock=None):
    d = tempfile.mkdtemp()
    j = Journal(os.path.join(d, "j.db"))
    clock = clock or FakeClock()
    st = status if callable(status) else (lambda: (status, 10.0) if status is not None else (None, None))
    ctx = Context(journal=j, cfg=config.load("/nonexistent"), ha=ha or FakeHA(), clock=clock,
                  status_fn=st, ssh_fn=ssh or (lambda h, c, timeout=30: (1, "", "no ssh in tests")))
    return ctx
