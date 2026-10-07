"""Which tab a bot worked in last, so Watch Browser opens on it.

A bot's Chrome collects tabs: every ``new_tab`` stays open, across chats and scheduled jobs. Chrome can't say which
one the bot is using (its own tab order follows activation, which moving within a tab doesn't change and which the
viewer itself changes), so the dashboard keeps one quiet DevTools connection per standing Chrome and notes when each
tab opened or went to a new address. ``recent(url)`` lists tab ids, most recently used first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("dispatch-browser")
RETRY = 5.0


class Tracker:
    def __init__(self, cdp_module):
        self.cdp_module = cdp_module
        self.used: dict[str, dict[str, float]] = {}  # Chrome (discovery root) -> tab id -> when it was last used
        self.watched: set[str] = set()
        self.lock = threading.Lock()
        self.loop = None

    def watch(self, url: str) -> None:
        """Start following a bot's Chrome (its ``browser.cdp_url`` discovery root). Cheap to call again."""
        key = root(url)
        with self.lock:
            if key in self.watched:
                return
            self.watched.add(key)
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, name="dispatch-browser-tabs", daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._follow(key), self.loop)

    def recent(self, url: str) -> list[str]:
        with self.lock:
            used = dict(self.used.get(root(url), {}))
        return sorted(used, key=lambda tab: -used[tab])

    def _mark(self, key: str, tab, when: float) -> None:
        if tab:
            with self.lock:
                self.used.setdefault(key, {})[str(tab)] = when

    async def _follow(self, key: str) -> None:
        while True:
            try:
                await self._session(key)
            except Exception:  # noqa: BLE001 - Chrome restarting or not up yet
                log.debug("dispatch-browser: tab tracking for %s paused", key, exc_info=True)
            await asyncio.sleep(RETRY)

    async def _session(self, key: str) -> None:
        listing = await asyncio.to_thread(_get, f"{key}/json/list")
        endpoint = (await asyncio.to_thread(_get, f"{key}/json/version")).get("webSocketDebuggerUrl")
        if not endpoint:
            return
        # Before this connection, Chrome's own order (newest activation first) is the best guess; anything seen from
        # now on is newer than it.
        start = time.time()
        pages = [t for t in listing if isinstance(t, dict) and t.get("type") == "page"]
        with self.lock:
            known = self.used.setdefault(key, {})
            for rank, tab in enumerate(pages):
                known.setdefault(str(tab.get("id")), start - 3600 - rank)
        urls: dict[str, str] = {str(t.get("id")): str(t.get("url") or "") for t in pages}
        cdp = await self.cdp_module.Cdp().connect(endpoint)

        def on_event(method, params, _session):
            info = params.get("targetInfo") or {}
            tab = str(info.get("targetId") or params.get("targetId") or "")
            if method == "Target.targetCreated" and info.get("type") == "page":
                urls[tab] = str(info.get("url") or "")
                self._mark(key, tab, time.time())
            elif method == "Target.targetInfoChanged" and info.get("type") == "page":
                url = str(info.get("url") or "")
                if urls.get(tab) != url:
                    urls[tab] = url
                    self._mark(key, tab, time.time())
            elif method == "Target.targetDestroyed":
                urls.pop(tab, None)
                with self.lock:
                    self.used.get(key, {}).pop(tab, None)

        cdp.listeners.append(on_event)
        try:
            await cdp.call("Target.setDiscoverTargets", {"discover": True})
            await cdp.closed.wait()
        finally:
            await cdp.close()


def root(url: str) -> str:
    return str(url).rstrip("/").replace("ws://", "http://").replace("localhost", "127.0.0.1")


def _get(url: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:  # noqa: S310 - loopback only
        return json.loads(response.read(4 * 1024 * 1024))
