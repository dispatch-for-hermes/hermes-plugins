"""A small Chrome DevTools Protocol client (flattened sessions) for the dashboard side."""
from __future__ import annotations

import asyncio
import itertools
import json
import re

LOOPBACK = re.compile(r"^ws://(?:127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})/devtools/browser/[A-Za-z0-9-]{1,128}$")
MAX_MESSAGE = 32 * 1024 * 1024


class CdpError(Exception):
    pass


class Cdp:
    def __init__(self):
        self.socket = None
        self.ids = itertools.count(1)
        self.waiting: dict[int, asyncio.Future] = {}
        self.listeners = []  # callables(method, params, session_id)
        self.reader = None
        self.closed = asyncio.Event()

    async def connect(self, endpoint: str, timeout: float = 5.0):
        if not LOOPBACK.match(endpoint or ""):
            raise CdpError("not a local browser")
        import websockets
        self.socket = await asyncio.wait_for(
            websockets.connect(endpoint, max_size=MAX_MESSAGE, ping_interval=None, open_timeout=timeout), timeout)
        self.reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            async for raw in self.socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                ident = message.get("id")
                if ident is not None:
                    future = self.waiting.pop(ident, None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CdpError(str(message["error"].get("message", "CDP error"))))
                        else:
                            future.set_result(message.get("result") or {})
                    continue
                for listener in list(self.listeners):
                    try:
                        listener(message.get("method"), message.get("params") or {}, message.get("sessionId"))
                    except Exception:  # noqa: BLE001 - one listener never stops the reader
                        pass
        except Exception:  # noqa: BLE001 - a dropped browser ends every call below
            pass
        finally:
            self.closed.set()
            for future in self.waiting.values():
                if not future.done():
                    future.set_exception(CdpError("browser connection closed"))
            self.waiting.clear()

    async def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 10.0):
        if self.closed.is_set():
            raise CdpError("browser connection closed")
        ident = next(self.ids)
        message = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        self.waiting[ident] = future
        try:
            await self.socket.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiting.pop(ident, None)

    def send(self, method: str, params: dict | None = None, session: str | None = None):
        """Fire and forget (acks): failures surface through ``closed``."""
        message = {"id": next(self.ids), "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        return asyncio.ensure_future(self._quiet(json.dumps(message)))

    async def _quiet(self, text):
        try:
            await self.socket.send(text)
        except Exception:  # noqa: BLE001
            pass

    async def close(self):
        if self.socket is not None:
            try:
                await self.socket.close()
            except Exception:  # noqa: BLE001
                pass
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)


def page_tabs(targets: list) -> list[dict]:
    """Ordinary page tabs, in Chrome's order (devtools, extensions and workers left out)."""
    tabs = []
    for target in targets:
        if target.get("type") != "page":
            continue
        url = str(target.get("url") or "")
        if url.startswith(("devtools://", "chrome-extension://", "chrome://")):
            continue
        tabs.append({"id": str(target.get("targetId")), "title": str(target.get("title") or "")[:300], "url": url[:2048]})
    return tabs
