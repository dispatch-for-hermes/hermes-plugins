"""Dashboard side: relay one browser's screencast to one phone, and carry the person's input while they hold it.

Wire format (text JSON both ways, one websocket per viewer):

server → phone
- ``state``  ``{browser, tabs, tab, follow, control: {state, mine, epoch}, ask}`` on open and whenever any of it changes.
             ``control.state`` is ``bot`` (the bot drives), ``waiting`` (a person asked; the bot's running browser
             call is finishing) or ``person``. ``ask`` is the bot's ``browser_ask_user`` reason, or null.
- ``frame``  ``{seq, tab, epoch, width, height, image: "data:image/jpeg;base64,…"}``; width/height are the page's
             CSS viewport the image shows, so the phone maps a tap to page coordinates as fractions.
- ``ended``  ``{reason: "closed" | "stopped" | "error"}``, then the socket closes.
- ``error``  ``{message}`` (a refused request; the stream goes on).
- ``ping``

phone → server
- ``ack {seq}`` once a frame is on screen. At most ``IN_FLIGHT`` frames wait for an ack; until then Chrome's own
  frame ack is held, so Chrome slows to what the connection carries. Size and quality step down on slow acks.
- ``view {width, height, scale}`` its stage in CSS pixels and its pixel density. While this viewer holds the
  browser, the page is laid out at that size (a phone-sized viewport), and back at its own once given back. ``tab {id}`` watch one tab; ``follow`` follow the bot's tab.
- ``take`` / ``give`` ask for / hand back the browser. While this viewer holds it (state ``person``, matching
  ``epoch``): ``tap {x, y, epoch}``, ``scroll {x, y, dy, epoch}`` (x, y as 0–1 fractions of the frame; dy in
  page pixels), ``text {text, epoch}``, ``key {key, epoch}``, ``go {url, epoch}``, ``nav {action, epoch}``.
"""
from __future__ import annotations

import asyncio
import json
import time

PROTOCOL = 3
IN_FLIGHT = 2
PING_EVERY = 10.0
CLAIM_SECONDS = 120.0   # a held browser stays held this long after its viewer drops (switching apps for a code)
RENEW_EVERY = 5.0
# (longest side cap, JPEG quality), slow to sharp
LEVELS = ((960, 50), (1280, 60), (1600, 70), (1920, 80))
SLOW_ACK, QUICK_ACK = 0.6, 0.2
KEYS = {  # key -> (code, windowsVirtualKeyCode, text)
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, ""), "Tab": ("Tab", 9, ""), "Escape": ("Escape", 27, ""),
    "Delete": ("Delete", 46, ""), "ArrowLeft": ("ArrowLeft", 37, ""), "ArrowRight": ("ArrowRight", 39, ""),
    "ArrowUp": ("ArrowUp", 38, ""), "ArrowDown": ("ArrowDown", 40, ""), "Home": ("Home", 36, ""), "End": ("End", 35, ""),
    "PageUp": ("PageUp", 33, ""), "PageDown": ("PageDown", 34, ""), " ": ("Space", 32, " "),
}


def public(record: dict) -> dict:
    return {"id": record.get("id"), "profile": record.get("profile"), "session_id": record.get("session_id"),
            "session_ids": list(record.get("session_ids") or [])[-16:], "opened_at": record.get("opened_at"),
            "agent_at": record.get("agent_at"), "url": record.get("url"), "busy": int(record.get("busy") or 0)}


class Viewer:
    """One phone watching one browser."""

    def __init__(self, spool, cdp, record: dict, send_text, siblings=None):
        self.spool, self.cdp_module, self.record = spool, cdp, record
        self.send_text = send_text
        self.siblings = siblings or (lambda record: [record["id"]])  # every published copy of this browser
        self.viewer_id = None
        self.cdp = None
        self.tabs: list[dict] = []
        self.tab = None
        self.follow = True
        self.session = None
        self.pending = None   # newest Chrome frame not yet sent: (data, metadata, ack id)
        self.in_flight: dict[int, float] = {}
        self.seq = 0
        self.level = 1
        self.view = (0, 0)      # the phone's stage in device pixels (screencast size caps)
        self.css = (0, 0, 1.0)  # the phone's stage in CSS pixels and its density (the held page's viewport)
        self.emulated = None    # the viewport override applied to the attached tab, or None
        self.bot_view = None    # the bot's own viewport (CSS px) before a held page took the phone's size
        self.rtt = None
        self.quick = 0
        self.wake = asyncio.Event()
        self.changed = True
        self.restart_at = 0.0
        self.ended = None
        self.attached = None
        self.params = None
        self.page = (0.0, 0.0)  # CSS viewport the last frame showed
        self.claim = None       # this viewer's control file contents, while it asks for or holds the browser
        self.sent_state = None
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=64)

    # Chrome events (called from the CDP reader)
    def on_event(self, method, params, session_id):
        if method == "Page.screencastFrame":
            if session_id and session_id == self.session:
                if self.pending is not None and self.pending[2] is not None:  # superseded: let Chrome move on
                    self.cdp.send("Page.screencastFrameAck", {"sessionId": self.pending[2]}, self.session)
                self.pending = (params.get("data") or "", params.get("metadata") or {}, params.get("sessionId"))
                self.wake.set()
            return
        info = params.get("targetInfo") or {}
        if method == "Target.targetCreated" and info.get("type") == "page":
            self._upsert(info)
            if self.follow and not self._holding():
                self.tab = str(info.get("targetId"))
        elif method == "Target.targetInfoChanged" and info.get("type") == "page":
            before = next((t for t in self.tabs if t["id"] == info.get("targetId")), None)
            self._upsert(info)
            if self.follow and not self._holding() and before is not None and before["url"] != info.get("url"):
                self.tab = str(info.get("targetId"))
        elif method == "Target.targetDestroyed":
            gone = params.get("targetId")
            self.tabs = [t for t in self.tabs if t["id"] != gone]
            self.changed = True
            if self.tab == gone:
                self.tab = self.tabs[-1]["id"] if self.tabs else None
                if self.tab is None:
                    self.ended = "closed"
        elif method in ("Inspector.detached", "Target.detachedFromTarget"):
            if params.get("sessionId") == self.session:
                self.session = None
                self.attached = None
        else:
            return
        self.wake.set()

    def _upsert(self, info):
        tab = self.cdp_module.page_tabs([info])
        if not tab:
            return
        tab = tab[0]
        for index, existing in enumerate(self.tabs):
            if existing["id"] == tab["id"]:
                self.tabs[index] = tab
                break
        else:
            self.tabs.append(tab)
        self.changed = True

    # Control
    def _control(self):
        """The current claim on this browser (anyone's), from its control file."""
        return self.spool.control(self.record["id"])

    def _holding(self) -> bool:
        claim = self._control()
        return bool(claim and claim.get("viewer") == self.viewer_id and claim.get("state") == "controlled")

    def _write_claim(self, state: str, epoch: int):
        self.claim = {"state": state, "viewer": self.viewer_id, "epoch": epoch, "browser": self.record["id"],
                      "expires_at": time.time() + CLAIM_SECONDS, "at": time.time()}
        for ident in self.siblings(self.record):
            self.spool.write(self.spool.folder("control") / f"{ident}.json", {**self.claim, "browser": ident})
        self.changed = True

    def _release(self):
        current = self._control()
        if current and current.get("viewer") == self.viewer_id:
            for ident in self.siblings(self.record):
                self.spool.remove(self.spool.folder("control") / f"{ident}.json")
                if current.get("state") == "controlled":  # a bot waiting in browser_ask_user sees it at once
                    ask = self.spool.folder("asks") / f"{ident}.json"
                    value = self.spool.read(ask)
                    if value is not None:
                        self.spool.write(ask, {**value, "handed_back_at": time.time()})
        self.claim = None
        self.changed = True

    def take(self):
        current = self._control()
        if current and current.get("viewer") != self.viewer_id:
            return "Someone else is using this browser right now."
        epoch = int(current["epoch"]) if current else int(time.time() * 1000)
        self._write_claim("controlled" if current and current["state"] == "controlled" else "requested", epoch)
        return None

    def _advance_claim(self):
        """``requested`` becomes ``controlled`` once every process using this browser has promised the bot is
        paused for this claim: it saw the claim with no browser call running (``paused`` in its record)."""
        if self.claim is None:
            return
        current = self._control()
        if current is None or current.get("viewer") != self.viewer_id:
            self.claim = None
            self.changed = True
            return
        if current["state"] != "requested":
            return
        records = [self.spool.read(self.spool.folder("browsers") / f"{ident}.json") for ident in self.siblings(self.record)]
        if records and all(record is not None and record.get("paused") == current["epoch"] for record in records):
            self._write_claim("controlled", int(current["epoch"]))

    def _state(self):
        claim = self._control()
        mine = bool(claim and claim.get("viewer") == self.viewer_id)
        if claim is None:
            control = {"state": "bot", "mine": False, "epoch": None}
        else:
            control = {"state": "person" if claim["state"] == "controlled" else "waiting", "mine": mine, "epoch": claim["epoch"]}
        ask = self.spool.read(self.spool.folder("asks") / f"{self.record['id']}.json")
        reason = ask.get("reason") if ask and float(ask.get("expires_at") or 0) > time.time() else None
        return {"type": "state", "protocol": PROTOCOL, "browser": public(self.record), "tabs": [dict(t) for t in self.tabs], "tab": self.tab,
                "follow": self.follow, "control": control, "ask": reason if isinstance(reason, str) else None}

    # Phone messages
    def on_phone(self, message: dict):
        kind = message.get("type")
        if kind == "ack":
            sent = self.in_flight.pop(message.get("seq"), None)
            if sent is not None:
                rtt = time.monotonic() - sent
                self.rtt = rtt if self.rtt is None else self.rtt * 0.7 + rtt * 0.3
                self._adapt()
        elif kind == "view":
            width, height, scale = message.get("width"), message.get("height"), message.get("scale", 1)
            if (isinstance(width, int) and isinstance(height, int) and 0 < width <= 4096 and 0 < height <= 4096
                    and isinstance(scale, (int, float)) and 1 <= scale <= 3):
                if (width, height, scale) != self.css:
                    self.css = (width, height, float(scale))
                    self.view = (round(width * scale), round(height * scale))
                    self.restart_at = time.monotonic()
        elif kind == "tab" and isinstance(message.get("id"), str):
            if any(t["id"] == message["id"] for t in self.tabs):
                self.follow, self.tab = False, message["id"]
                self.changed = True
        elif kind == "follow":
            self.follow = True
            self.changed = True
        elif kind in ("take", "give", "tap", "scroll", "text", "key", "go", "nav"):
            try:
                self.inbox.put_nowait(message)
            except asyncio.QueueFull:
                pass
        self.wake.set()

    async def _act(self, message: dict):
        kind = message.get("type")
        if kind == "take":
            problem = self.take()
            if problem:
                await self.send_text({"type": "error", "message": problem})
            return
        if kind == "give":
            self._release()
            return
        claim = self._control()
        if (not claim or claim.get("viewer") != self.viewer_id or claim.get("state") != "controlled"
                or message.get("epoch") != claim.get("epoch") or not self.session):
            await self.send_text({"type": "error", "message": "Take control first."})
            self.changed = True
            return
        session, (width, height) = self.session, self.page

        def point():
            x, y = message.get("x"), message.get("y")
            if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (x, y)) or not width or not height:
                return None
            return float(x) * width, float(y) * height

        call = self.cdp.call
        if kind == "tap" and (at := point()):
            x, y = at
            await call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
            await call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, session)
            await call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, session)
        elif kind == "scroll" and (at := point()):
            dy, dx = message.get("dy"), message.get("dx", 0)
            if isinstance(dy, (int, float)) and isinstance(dx, (int, float)):
                await call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": at[0], "y": at[1],
                                                        "deltaX": max(-4000, min(4000, dx)), "deltaY": max(-4000, min(4000, dy))}, session)
        elif kind == "text" and isinstance(message.get("text"), str) and 0 < len(message["text"]) <= 4096:
            await call("Input.insertText", {"text": message["text"]}, session)
        elif kind == "key" and message.get("key") in KEYS:
            code, number, text = KEYS[message["key"]]
            base = {"key": message["key"], "code": code, "windowsVirtualKeyCode": number, "nativeVirtualKeyCode": number}
            await call("Input.dispatchKeyEvent", {"type": "keyDown" if text else "rawKeyDown", **base, **({"text": text} if text else {})}, session)
            await call("Input.dispatchKeyEvent", {"type": "keyUp", **base}, session)
        elif kind == "go" and isinstance(message.get("url"), str):
            url = message["url"].strip()[:2048]
            if url.startswith(("http://", "https://")):
                await call("Page.navigate", {"url": url}, session)
        elif kind == "nav" and message.get("action") in ("back", "forward", "reload"):
            if message["action"] == "reload":
                await call("Page.reload", {}, session)
            else:
                history = await call("Page.getNavigationHistory", {}, session)
                index = int(history.get("currentIndex") or 0) + (-1 if message["action"] == "back" else 1)
                entries = history.get("entries") or []
                if 0 <= index < len(entries):
                    await call("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}, session)
        self.wake.set()

    def _adapt(self):
        now = time.monotonic()
        if self.rtt > SLOW_ACK and self.level > 0 and now - self.restart_at > 3:
            self.level -= 1
            self.quick = 0
            self.restart_at = now
        elif self.rtt < QUICK_ACK and self.level < len(LEVELS) - 1:
            self.quick += 1
            if self.quick >= 20 and now - self.restart_at > 3:
                self.level += 1
                self.quick = 0
                self.restart_at = now
        else:
            self.quick = 0

    def _params(self):
        cap, quality = LEVELS[self.level]
        width, height = self.view
        return {"format": "jpeg", "quality": quality, "everyNthFrame": 1,
                "maxWidth": min(cap, width) if width else cap, "maxHeight": min(cap, height) if height else cap}

    async def _inner(self):
        result = await self.cdp.call("Runtime.evaluate", {"expression": "[innerWidth, innerHeight]", "returnByValue": True},
                                     self.session, timeout=3)
        width, height = result["result"]["value"]
        return int(width), int(height)

    async def _unfit(self):
        """Back to the bot's own viewport. Chrome keeps one viewport override per page: setting ours replaced the
        bot's (Playwright's 1280x720), and clearing ours, or detaching, leaves the bare window. So the window is
        resized to the viewport the bot had, which outlasts this connection."""
        await self.cdp.call("Emulation.clearDeviceMetricsOverride", {}, self.session, timeout=3)
        if not self.bot_view:
            return
        width, height = await self._inner()
        dw, dh = self.bot_view[0] - width, self.bot_view[1] - height
        if dw or dh:
            window = await self.cdp.call("Browser.getWindowForTarget", {"targetId": self.attached}, timeout=3)
            bounds = window.get("bounds") or {}
            await self.cdp.call("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {
                "width": int(bounds.get("width") or width) + dw, "height": int(bounds.get("height") or height) + dh}}, timeout=3)

    async def _fit(self):
        """A held page takes the phone's size; anyone else's view of it, and a page given back, the bot's."""
        want = None
        if self.session and self._holding() and self.css[0] >= 200 and self.css[1] >= 200:
            want = {"width": self.css[0], "height": self.css[1], "deviceScaleFactor": self.css[2], "mobile": True}
        if want == self.emulated:
            return
        try:
            if want:
                if self.emulated is None:
                    self.bot_view = await self._inner()
                await self.cdp.call("Emulation.setDeviceMetricsOverride", want, self.session, timeout=3)
            elif self.session:
                await self._unfit()
            self.emulated = want
        except Exception:  # noqa: BLE001 - tried again on the next pass
            pass

    async def _attach(self, target):
        if self.emulated and self.session:  # the tab being left gets the bot's viewport back first
            try:
                await self._unfit()
            except Exception:  # noqa: BLE001 - it may already be gone
                pass
        self.emulated = self.bot_view = None  # an override belongs to the session and tab it was set on
        if self.session:
            old, self.session = self.session, None
            self.pending = None
            for method, params in (("Page.stopScreencast", {}), ("Target.detachFromTarget", {"sessionId": old})):
                try:
                    await self.cdp.call(method, params, old if method.startswith("Page") else None, timeout=3)
                except Exception:  # noqa: BLE001 - the tab may already be gone
                    pass
        self.attached = target
        self.changed = True
        if target is None:
            return
        result = await self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
        self.session = result["sessionId"]
        self.params = self._params()
        # One still first: a page that isn't changing sends no screencast frames.
        try:
            metrics = await self.cdp.call("Page.getLayoutMetrics", session=self.session, timeout=3)
            view = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
            width, height = float(view.get("clientWidth") or 0), float(view.get("clientHeight") or 0)
            shot = await self.cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": self.params["quality"],
                                                                  "optimizeForSpeed": True}, self.session, timeout=5)
            self.pending = (shot.get("data") or "", {"deviceWidth": width, "deviceHeight": height}, None)
        except Exception:  # noqa: BLE001
            pass
        await self.cdp.call("Page.startScreencast", self.params, self.session)
        self.wake.set()

    async def _send_frame(self):
        data, metadata, ack = self.pending
        self.pending = None
        if ack is not None:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ack}, self.session)
        if not data or len(data) > 3 * 1024 * 1024:
            return
        width, height = float(metadata.get("deviceWidth") or 0), float(metadata.get("deviceHeight") or 0)
        if width > 0 and height > 0:
            self.page = (width, height)
        claim = self._control()
        self.seq += 1
        self.in_flight[self.seq] = time.monotonic()
        await self.send_text({"type": "frame", "seq": self.seq, "tab": self.attached, "epoch": claim.get("epoch") if claim else None,
                              "width": round(self.page[0], 2), "height": round(self.page[1], 2),
                              "image": "data:image/jpeg;base64," + data})

    def _first_tab(self):
        url = self.record.get("url")
        match = [t for t in self.tabs if url and t["url"] == url]
        return (match or self.tabs or [{"id": None}])[-1]["id"]

    def _still_open(self):
        """None while the owner vouches for the browser; why it ended once it is gone (two misses in a row,
        or an explicit end, so one slow heartbeat never closes a person's view)."""
        current = self.spool.read(self.spool.folder("browsers") / f"{self.record['id']}.json")
        if current is not None and self.spool.live(current):
            self.record, self.misses = current, 0
            return None
        if current is not None and current.get("status") == "ended":
            return "closed"
        self.misses = getattr(self, "misses", 0) + 1
        return "closed" if self.misses >= 2 else None

    async def run(self, receive, viewer_id: str, tab=None, follow=True):
        """Serve until the phone leaves or the browser closes. ``receive()`` returns the phone's next text."""
        self.viewer_id = viewer_id
        current = self._control()
        if current and current.get("viewer") == viewer_id:  # a phone coming back to a browser it still holds
            self.claim = current
        self.cdp = await self.cdp_module.Cdp().connect(self.record["endpoint"])
        self.cdp.listeners.append(self.on_event)
        reader = actor = None
        try:
            await self.cdp.call("Target.setDiscoverTargets", {"discover": True})
            self.tabs = self.cdp_module.page_tabs((await self.cdp.call("Target.getTargets")).get("targetInfos") or [])
            if tab and any(t["id"] == tab for t in self.tabs):
                self.tab, self.follow = tab, bool(follow)
            else:
                self.tab = self._first_tab()
            if self.tab is None:
                await self.send_text({"type": "ended", "reason": "closed"})
                return
            await self._attach(self.tab)
            reader = asyncio.create_task(self._read_phone(receive))
            actor = asyncio.create_task(self._run_actions())
            checked = pinged = renewed = time.monotonic()
            while not reader.done():
                try:
                    await asyncio.wait_for(self.wake.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
                now = time.monotonic()
                if self.cdp.closed.is_set() and not self.ended:
                    self.ended = self._still_open() or "closed"
                if now - checked >= 1.0:
                    checked = now
                    self.ended = self.ended or self._still_open()
                    self._advance_claim()
                    self.changed = True  # control files and asks change outside this socket
                if self.ended:
                    await self.send_text({"type": "ended", "reason": self.ended})
                    return
                if self.claim is not None and now - renewed >= RENEW_EVERY:
                    renewed = now
                    current = self._control()
                    if current and current.get("viewer") == self.viewer_id:
                        self._write_claim(current["state"], int(current["epoch"]))
                if self.tab != self.attached:
                    try:
                        await self._attach(self.tab)
                    except self.cdp_module.CdpError:  # the tab went away first (a popup that closed)
                        gone = self.tab
                        self.tabs = [t for t in self.tabs if t["id"] != gone]
                        self.tab, self.attached, self.session = (self.tabs[-1]["id"] if self.tabs else None), None, None
                        self.changed = True
                        if self.tab is None:
                            self.ended = "closed"
                        continue
                if now - self.restart_at > 0.3:
                    await self._fit()
                if self.session and self._params() != self.params and now - self.restart_at > 0.3:
                    self.params = self._params()
                    try:  # Chrome refuses a second start while one runs
                        await self.cdp.call("Page.stopScreencast", {}, self.session, timeout=3)
                        await self.cdp.call("Page.startScreencast", self.params, self.session)
                    except self.cdp_module.CdpError:
                        self.attached = None  # reattach on the next pass
                if self.changed:
                    self.changed = False
                    state = self._state()
                    if state != self.sent_state:
                        self.sent_state = state
                        await self.send_text(state)
                if self.pending is not None and len(self.in_flight) < IN_FLIGHT:
                    await self._send_frame()
                for seq, sent in list(self.in_flight.items()):  # a lost ack must not stall the stream
                    if now - sent > 5:
                        self.in_flight.pop(seq, None)
                if now - pinged >= PING_EVERY:
                    pinged = now
                    await self.send_text({"type": "ping"})
        finally:
            for task in (reader, actor):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (reader, actor) if t is not None), return_exceptions=True)
            if self.ended:  # nothing left to hold
                self._release()
            if self.emulated and self.session and not self.cdp.closed.is_set():
                try:
                    await self._unfit()
                except Exception:  # noqa: BLE001
                    pass
            await self.cdp.close()

    async def _run_actions(self):
        while True:
            message = await self.inbox.get()
            try:
                await self._act(message)
            except Exception:  # noqa: BLE001 - one failed input never ends the stream
                try:
                    await self.send_text({"type": "error", "message": "The browser didn't take that. Try again."})
                except Exception:  # noqa: BLE001
                    return

    async def _read_phone(self, receive):
        while True:
            text = await receive()
            if text is None:
                return
            if len(text) > 8192:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.on_phone(message)


async def tabs(module, endpoint: str) -> list[dict]:
    cdp = await module.Cdp().connect(endpoint, timeout=2)
    try:
        return module.page_tabs((await cdp.call("Target.getTargets", timeout=2)).get("targetInfos") or [])
    finally:
        await cdp.close()
