import time

from .. import sources


class Watcher:
    """One rule. Watchers only read; they never change anything."""

    name = ""
    interval_s: int | None = None  # None = event-only
    expect_s = 3600  # longest silence before the watchdog flags it

    def on_event(self, event, ctx):
        return []

    async def check(self, ctx):
        return []


class Context:
    def __init__(self, journal, cfg, ha=None, clock=time.time, status_fn=None, ssh_fn=sources.ssh):
        self.journal = journal
        self.cfg = cfg
        self.ha = ha
        self._clock = clock
        self._status_fn = status_fn or (lambda: sources.read_status(sources.STATUS_PATH, clock()))
        self._ssh = ssh_fn
        self.started = clock()
        self.watchers = []
        self.last_ws_drop = None
        self.ws_connected = False

    def now(self):
        return self._clock()

    def status(self):
        return self._status_fn()

    def ssh(self, host, cmd, timeout=30):
        return self._ssh(host, cmd, timeout=timeout)
