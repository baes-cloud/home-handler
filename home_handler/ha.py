import asyncio
import itertools
import json
import logging
import time
import urllib.request

import websockets

log = logging.getLogger("home_handler.ha")


class HAClient:
    """Read-mostly Home Assistant client: one websocket for events and commands, plus REST."""

    def __init__(self, ws_url, rest_url, token_path):
        self.ws_url = ws_url
        self.rest_url = rest_url.rstrip("/")
        with open(token_path) as f:
            self.token = f.read().strip()
        self._ws = None
        self._ids = itertools.count(1)
        self._pending = {}
        self.connected = asyncio.Event()

    async def _connect(self):
        ws = await websockets.connect(self.ws_url, max_size=None, ping_interval=30)
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected hello {hello.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        r = json.loads(await ws.recv())
        if r.get("type") != "auth_ok":
            raise PermissionError("HA auth failed")
        return ws

    async def run(self, on_event, event_types, on_connect=None, on_disconnect=None):
        backoff = 2
        while True:
            try:
                self._ws = await self._connect()
                sub_ids = []
                for et in event_types:
                    i = next(self._ids)
                    sub_ids.append(i)
                    await self._ws.send(json.dumps({"id": i, "type": "subscribe_events", "event_type": et}))
                self.connected.set()
                backoff = 2
                log.info("subscribed to %s", ",".join(event_types))
                if on_connect:
                    on_connect(time.time())
                async for raw in self._ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "event":
                        on_event(msg["event"])
                    elif msg.get("id") in self._pending:
                        fut = self._pending.pop(msg["id"])
                        if not fut.done():
                            if msg.get("success", True):
                                fut.set_result(msg.get("result"))
                            else:
                                fut.set_exception(RuntimeError(msg.get("error")))
            except asyncio.CancelledError:
                raise
            except PermissionError:
                log.error("HA token rejected; retrying in 300 s")
                backoff = 300
            except Exception as e:
                log.warning("HA websocket lost: %s", e)
            self.connected.clear()
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("websocket closed"))
            self._pending.clear()
            if on_disconnect:
                on_disconnect(time.time())
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def call(self, type_, timeout=60, **kw):
        await asyncio.wait_for(self.connected.wait(), timeout)
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await self._ws.send(json.dumps({"id": i, "type": type_, **kw}))
        return await asyncio.wait_for(fut, timeout)

    def rest(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.rest_url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else None
