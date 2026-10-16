"""Minimaler Nostr-Relay-Pool (websockets): subscribe, publish, Event-Queue."""
from __future__ import annotations
import asyncio, json, contextlib
import websockets


class Pool:
    def __init__(self, urls: list[str]):
        self.urls, self.ws, self.subs, self.handlers, self.tasks = urls, {}, {}, [], []
        self._seen = set()

    async def start(self, timeout: float = 8, min_ok: int = 1):
        async def conn(u):
            try:
                self.ws[u] = await asyncio.wait_for(websockets.connect(u, open_timeout=timeout, max_size=2**22), timeout)
                self.tasks.append(asyncio.create_task(self._reader(u)))
            except Exception:
                pass
        await asyncio.gather(*(conn(u) for u in self.urls))
        if len(self.ws) < min_ok:
            raise ConnectionError("keine Relay-Verbindung / no relay connection")

    async def _reader(self, u):
        with contextlib.suppress(Exception):
            async for raw in self.ws[u]:
                m = json.loads(raw)
                if m[0] == "EVENT" and len(m) >= 3 and m[2].get("id") not in self._seen:
                    self._seen.add(m[2]["id"])
                    for h in self.handlers:
                        await h(m[1], m[2])

    async def subscribe(self, sid: str, filters: list[dict]):
        for w in list(self.ws.values()):
            with contextlib.suppress(Exception):
                await w.send(json.dumps(["REQ", sid, *filters]))

    async def publish(self, ev: dict) -> int:
        n = 0
        for w in list(self.ws.values()):
            with contextlib.suppress(Exception):
                await w.send(json.dumps(["EVENT", ev])); n += 1
        return n

    async def close(self):
        for t in self.tasks: t.cancel()
        for w in self.ws.values():
            with contextlib.suppress(Exception):
                await w.close()
