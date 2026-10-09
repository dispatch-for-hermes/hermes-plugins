"""Dashboard side: relay one browser's screencast to one phone, and carry the person's input while they hold it.

Wire format (text JSON both ways, one websocket per viewer):

server → phone
- ``state``  ``{browser, tabs, tab, follow, control: {state, mine, epoch}, ask, field}`` on open and whenever any of it
             changes. ``control.state`` is ``bot`` (the bot drives), ``waiting`` (a person asked; the bot's running
             browser call is finishing) or ``person``. ``ask`` is the bot's ``browser_ask_user`` reason, or null.
             ``field`` (while this viewer holds the browser) ``{under, focus}``: the kind of text field under the
             pointer and the one with the page's focus (``password``, ``username``, ``code``, ``email``, ``tel``,
             ``url``, ``number``, ``search``, ``text``, ``multiline``), or null: the phone raises a matching keyboard
             (a password field gets the phone's password AutoFill: Passwords, 1Password).
- ``copied`` ``{text}`` the page's selected text, answering ``copy``.
- ``frame``  ``{seq, tab, epoch, width, height, image: "data:image/jpeg;base64,…"}``; width/height are the page's
             CSS viewport the image shows, so the phone maps a tap to page coordinates as fractions.
- ``ended``  ``{reason: "closed" | "stopped" | "unresponsive" | "error"}``, then the socket closes (``unresponsive``:
             the browser's Chrome didn't answer; the phone tries again).
- ``error``  ``{message}`` (a refused request; the stream goes on).
- ``ping``

phone → server
- ``ack {seq}`` once a frame is on screen. At most ``IN_FLIGHT`` frames wait for an ack; until then Chrome's own
  frame ack is held, so Chrome slows to what the connection carries. Size and quality step down on slow acks.
- ``view {width, height, scale}`` the size it draws the page at, in CSS pixels (its stage, times its zoom), and its
  pixel density: frames are sized to match. The page keeps the bot's own layout, held or not (a phone-sized
  viewport would switch sites to their mobile layout). ``tab {id}`` watch one tab; ``follow`` follow the bot's tab.
- ``take`` / ``give`` ask for / hand back the browser. While this viewer holds it (state ``person``, matching
  ``epoch``): ``move {x, y}`` (the phone's pointer moved; hover), ``tap {x, y}`` (a click), ``down`` / ``up {x, y}``
  (a drag), ``scroll {x, y, dy, dx}`` (x, y as 0–1 fractions of the frame; dy, dx in page pixels), ``text {text}``,
  ``key {key}``, ``go {url}``, ``nav {action}``, ``copy`` (the selection, answered with ``copied``), ``select_all``,
  each with the ``epoch``. Only the newest pointer move waits. Taps close together are a double (or triple) click.
  Typing and scrolling that arrive faster than the page takes them are merged while they wait (consecutive ``text``
  into one insert, consecutive ``scroll`` into one wheel turn), and input that can't be queued is answered with an
  ``error``, never dropped silently.
- ``pong`` answers ``ping`` (any message counts): a phone that goes silent while frames go out is gone, and its
  stream ends (a half-open socket otherwise lingers until the server's own keepalive, 30 s or more).

Watching never changes which tab the bot's Chrome shows: a background tab is shown with stills. Only while the person
holds the browser is the tab they work in brought to the front (the tab that was in front comes back when they hand
it back). Following the bot means following the tab it brings forward, or the one it opens or loads a page in.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from collections import deque

PROTOCOL = 3
IN_FLIGHT = 2
PING_EVERY = 10.0
CLAIM_SECONDS = 120.0
QUIET_CHECK = 3.0  # seconds without a frame before checking that Chrome still answers
STILL_AFTER = 1.5        # no screencast frame for this long: send a still instead
STILL_HIDDEN = 0.5       # a background tab (Chrome paints only its front tab) is shown by stills this often
FOLLOW_SETTLE = 2.0      # after the view moves to another tab, another tab's new address waits this long to take it
PHONE_SILENT = 12.0      # frames went out and the phone said nothing for this long: it is gone (a half-open socket)
MAX_INBOX = 256          # phone input waiting for the page; merged typing and scrolling take one slot a burst
MAX_TEXT = 4096
MAX_WHEEL = 4000
GRACE = 2.0              # a request waits at least this long for the bots' pause promises (agents publish each second)   # a held browser stays held this long after its viewer drops (switching apps for a code)
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


MULTI_CLICK = 0.5   # taps this close (seconds, and FIELD_SLOP px) count as a double or triple click
CLICK_SLOP = 8.0
# Which text field a point (``"point"``) or the focus (``"focus"``) is in, or the selected text (``"selection"``).
# Same-origin frames and shadow roots are followed here; a cross-origin frame is returned as its element, and the
# dashboard goes inside it (its own target, or an isolated world).
PROBE = r"""(function (mode, x, y) {
  const SKIP = new Set(['button', 'submit', 'checkbox', 'radio', 'file', 'image', 'reset', 'range', 'color', 'hidden'])
  let doc = document, el = null
  for (let depth = 0; depth < 6; depth++) {
    el = mode === 'point' ? doc.elementFromPoint(x, y) : doc.activeElement
    for (let i = 0; el && el.shadowRoot && i < 6; i++) {
      const inner = mode === 'point' ? el.shadowRoot.elementFromPoint(x, y) : el.shadowRoot.activeElement
      if (!inner || inner === el) break
      el = inner
    }
    if (el && (el.tagName === 'IFRAME' || el.tagName === 'FRAME')) {
      const box = el.getBoundingClientRect()
      let inner = null
      try { inner = el.contentDocument } catch (e) {}
      if (!inner) return el
      doc = inner; x -= box.left + el.clientLeft; y -= box.top + el.clientTop
      continue
    }
    break
  }
  if (mode === 'selection') {
    if (el && el.tagName === 'INPUT' && el.type === 'password') return JSON.stringify({text: ''})
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.selectionEnd > el.selectionStart)
      return JSON.stringify({text: el.value.slice(el.selectionStart, el.selectionEnd)})
    return JSON.stringify({text: String(doc.getSelection ? doc.getSelection() : '')})
  }
  if (el && mode === 'point' && !(el.isContentEditable || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
    const label = el.closest && el.closest('label')
    if (label && label.control) el = label.control
  }
  if (!el || el.disabled || el.readOnly) return 'null'
  const editable = el.isContentEditable || el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !SKIP.has((el.type || '').toLowerCase()))
  if (!editable) return 'null'
  const type = (el.type || '').toLowerCase(), auto = (el.getAttribute('autocomplete') || '').toLowerCase()
  const hint = [el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder'), auto].join(' ').toLowerCase()
  let kind = el.tagName === 'INPUT' ? 'text' : 'multiline'
  if (type === 'password' || /(current|new)-password/.test(auto)) kind = 'password'
  else if (el.tagName === 'INPUT' && (auto.includes('one-time-code') || /\b(otp|one[-_ ]?time|verification|verify|2fa|mfa|totp|passcode|security[-_ ]?code|auth[-_ ]?code)\b/.test(hint))) kind = 'code'
  else if (el.tagName === 'INPUT' && (auto.includes('username') || /\b(user(name)?|login|account)\b/.test(hint))) kind = 'username'
  else if (type === 'email' || auto.includes('email')) kind = 'email'
  else if (['tel', 'url', 'number', 'search'].includes(type)) kind = type
  return JSON.stringify(kind)
})"""
FIELD_KINDS = {"password", "username", "code", "email", "tel", "url", "number", "search", "text", "multiline"}

ACTIVE_FOR = 90.0  # a bot's browser counts as in use this long after its last browser tool


def public(record: dict, now: float | None = None) -> dict:
    """What a phone sees of a browser. ``active``: a bot is using it now (a browser tool running, or one in the
    last ACTIVE_FOR seconds). A bot's own Chrome stays open between tasks, and an idle one is not news."""
    now = time.time() if now is None else now
    busy = int(record.get("busy") or 0)
    agent_at = float(record.get("agent_at") or 0)
    return {"id": record.get("id"), "profile": record.get("profile"), "session_id": record.get("session_id"),
            "session_ids": list(record.get("session_ids") or [])[-16:], "opened_at": record.get("opened_at"),
            "agent_at": record.get("agent_at"), "url": record.get("url"), "busy": busy,
            "active": busy > 0 or (agent_at > 0 and now - agent_at < ACTIVE_FOR)}


def blank(url) -> bool:
    """A tab with nothing in it yet (a bot's browser often keeps one beside the page it works in)."""
    return str(url or "") in ("", "about:blank") or str(url).startswith(("chrome://newtab", "chrome://new-tab-page"))


class Unresponsive(Exception):
    """The browser's Chrome didn't answer (busy, frozen or restarting)."""


class Viewer:
    """One phone watching one browser."""

    def __init__(self, spool, cdp, record: dict, send_text, siblings=None, refresh=None, recent=None):
        self.spool, self.cdp_module, self.record = spool, cdp, record
        self.recent = recent or (lambda: [])  # tab ids, the one the bot used most recently first (tabs.py)
        self.send_text = send_text
        self.refresh = refresh or (lambda: None)  # keeps the dashboard's own (standing) records current
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
        self.css = (0, 0, 1.0)  # the phone's stage in CSS pixels and its density
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
        self.inbox: deque = deque()  # phone input waiting for the page (see _queue)
        self.inbox_ready = asyncio.Event()
        self.overflowed = False  # input was refused for a full inbox: the phone is told
        self.heard_at = time.monotonic()  # when the phone last said anything
        self.hidden = False     # the watched tab isn't Chrome's front tab (stills, not screencast frames)
        self.seen_front = False  # the watched tab was the front tab since it was attached
        self.switched_at = 0.0  # when following last moved the view to another tab
        self.fronted = None     # (the tab brought forward for the person, the tab that was in front before) while held
        self.next_move = None   # the newest pointer move not yet sent to the page
        self.framed_at = 0.0    # when a frame last went to the phone
        self.stilled_at = 0.0   # when a fallback still was last taken
        self.checked_chrome = 0.0  # when a quiet Chrome was last asked whether it still answers
        self.pressed = False    # a drag holds the page's left button
        self.at = None          # where the page's pointer is (CSS px)
        self.field = {"under": None, "focus": None}  # text fields for the phone's keyboard (PROBE), while held
        self.last_click = (0.0, 0.0, 0.0, 0)  # (time, x, y, count): taps close together are a double click
        self.superseded = False  # the same phone opened a newer stream: this one ends quietly

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
            if not blank(info.get("url")):
                self._follow_to(str(info.get("targetId")))  # an empty new tab is followed once it opens a page
        elif method == "Target.targetInfoChanged" and info.get("type") == "page":
            before = next((t for t in self.tabs if t["id"] == info.get("targetId")), None)
            self._upsert(info)
            if before is not None and before["url"] != info.get("url"):
                self._follow_to(str(info.get("targetId")))
        elif method == "Target.targetDestroyed":
            gone = params.get("targetId")
            self.tabs = [t for t in self.tabs if t["id"] != gone]
            self.changed = True
            if self.tab == gone:
                self.tab = self._first_tab()
                if self.tab is None:
                    self.ended = "closed"
        elif method in ("Inspector.detached", "Target.detachedFromTarget"):
            if params.get("sessionId") == self.session:
                self.session = None
                self.attached = None
        else:
            return
        self.wake.set()

    def _follow_to(self, tab: str):
        """Following the bot, the view moves to a tab it opened or loaded a page in, but not again within
        FOLLOW_SETTLE: two subagents taking turns in two tabs would otherwise flip the view back and forth."""
        if not self.follow or self.tab is None or tab == self.tab or self._holding():
            return  # (no tab yet: discovery replaying the open tabs; run() picks the first one)
        now = time.monotonic()
        if self.switched_at and now - self.switched_at < FOLLOW_SETTLE:
            return
        self.tab, self.switched_at = tab, now

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
        asked = (self.claim or {}).get("asked_at") if (self.claim or {}).get("epoch") == epoch else None
        self.claim = {"state": state, "viewer": self.viewer_id, "epoch": epoch, "browser": self.record["id"],
                      "expires_at": time.time() + CLAIM_SECONDS, "at": time.time(), "asked_at": asked or time.time()}
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
        if current["state"] != "requested" or time.time() - float(current.get("asked_at") or 0) < GRACE:
            return  # the grace lets a bot call that began just before the request get published first
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
        reason = None
        for ident in self.siblings(self.record):  # a bot asks on its own record; the phone may watch the standing one
            ask = self.spool.read(self.spool.folder("asks") / f"{ident}.json")
            reason = ask.get("reason") if ask and float(ask.get("expires_at") or 0) > time.time() else None
            if isinstance(reason, str):
                break
        held = control["state"] == "person" and mine
        return {"type": "state", "protocol": PROTOCOL, "browser": public(self.record), "tabs": [dict(t) for t in self.tabs], "tab": self.tab,
                "follow": self.follow, "control": control, "ask": reason if isinstance(reason, str) else None,
                "field": dict(self.field) if held else None}

    # Phone messages
    def on_phone(self, message: dict):
        kind = message.get("type")
        self.heard_at = time.monotonic()
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
                # A shorter stage at the same width (the keyboard, a banner) keeps the frames it has: restarting the
                # screencast for it would drop a frame just as the person starts typing.
                shorter = (width, float(scale)) == (self.css[0], self.css[2]) and height <= self.css[1]
                if (width, height, scale) != self.css and not shorter:
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
        elif kind == "move":
            fresh = self.next_move is None
            self.next_move = message  # a pointer only needs its newest position
            if fresh and not self._queue({"type": "move"}, report=False):
                self.next_move = None
        elif kind in ("take", "give", "tap", "down", "up", "scroll", "text", "key", "go", "nav", "copy", "select_all"):
            self._queue(message)
        self.wake.set()

    def _queue(self, message: dict, report: bool = True) -> bool:
        """Queue phone input for the page. Typing that waits is merged into the text before it (one insert for the
        burst) and scrolling into the wheel turn before it, so a busy page that takes 30 ms an event keeps up with 25
        characters a second and a fling ends when the fingers lift. Input that doesn't fit is reported, not lost."""
        last = self.inbox[-1] if self.inbox else None
        kind = message.get("type")
        if last is not None and last.get("type") == kind and last.get("epoch") == message.get("epoch"):
            if kind == "text" and isinstance(last.get("text"), str) and isinstance(message.get("text"), str) \
                    and message["text"] and len(last["text"]) + len(message["text"]) <= MAX_TEXT:
                self.inbox[-1] = {**last, "text": last["text"] + message["text"]}
                return True
            numbers = all(isinstance(m.get(k, 0), (int, float)) and not isinstance(m.get(k, 0), bool)
                          for m in (last, message) for k in ("dy", "dx"))
            if kind == "scroll" and numbers:
                dy, dx = last.get("dy", 0) + message.get("dy", 0), last.get("dx", 0) + message.get("dx", 0)
                if abs(dy) <= MAX_WHEEL and abs(dx) <= MAX_WHEEL:
                    self.inbox[-1] = {**message, "dy": dy, "dx": dx}  # the newest point, the whole distance
                    return True
        if len(self.inbox) >= MAX_INBOX:
            if report:
                self.overflowed = True
            return False
        self.inbox.append(message)
        self.inbox_ready.set()
        return True

    async def _act(self, message: dict):
        kind = message.get("type")
        if kind == "take":
            self.field = {"under": None, "focus": None}
            problem = self.take()
            if problem:
                await self.send_text({"type": "error", "message": problem})
            return
        if kind == "give":
            await self._lift()
            self._release()
            return
        if kind == "move":
            message, self.next_move = self.next_move or message, None
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
        if kind == "move" and (at := point()):
            self.at = at
            await call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": at[0], "y": at[1],
                                                    **({"button": "left", "buttons": 1} if self.pressed else {})}, session)
        elif kind == "down" and (at := point()):
            self.at, self.pressed = at, True
            await call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": at[0], "y": at[1]}, session)
            await call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": at[0], "y": at[1], "button": "left", "buttons": 1, "clickCount": 1}, session)
        elif kind == "up":
            await self._lift(point())
        elif kind == "tap" and (at := point()):
            x, y = self.at = at
            then, px, py, count = self.last_click
            now = time.monotonic()
            count = count % 3 + 1 if now - then < MULTI_CLICK and abs(x - px) <= CLICK_SLOP and abs(y - py) <= CLICK_SLOP else 1
            self.last_click = (now, x, y, count)
            await call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
            await call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": count}, session)
            await call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": count}, session)
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
        elif kind == "select_all":
            await call("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65,
                                                  "modifiers": 4, "commands": ["selectAll"]}, session)
            await call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 4}, session)
        elif kind == "copy":
            found = await self._probe("selection")
            text = found.get("text") if isinstance(found, dict) else None
            await self.send_text({"type": "copied", "text": text[:65536] if isinstance(text, str) else ""})
        typing_on = any(m.get("type") in ("text", "key") for m in self.inbox)
        if kind == "move":
            await self._sense(under=True)
        elif kind in ("tap", "up", "go", "nav", "select_all"):
            await self._sense(under=kind in ("tap", "up"), focus=True)
        elif kind == "key" and message.get("key") in ("Enter", "Tab", "Escape") and not typing_on:
            await self._sense(focus=True)  # these can move the page's focus; asked once the typing stops
        elif kind == "text" and not typing_on and self.field["focus"] is None:
            await self._sense(focus=True, settle=False)  # typing doesn't move the focus: asked only when unknown
        self.wake.set()

    async def _probe(self, mode: str, at=None):
        """PROBE in the page, following a cross-origin frame into its own target (or an isolated world)."""
        call, session = self.cdp.call, self.session
        x, y = at or (0.0, 0.0)
        expression = f"{PROBE}({json.dumps(mode)}, {x!r}, {y!r})"
        result = (await call("Runtime.evaluate", {"expression": expression, "objectGroup": "hq-probe"}, session, timeout=3)).get("result") or {}
        try:
            for _ in range(3):  # frames in frames
                if result.get("type") == "string":
                    return json.loads(result["value"])
                if result.get("subtype") != "node" or not result.get("objectId"):
                    return None
                frame = result["objectId"]
                offset = await call("Runtime.callFunctionOn", {"objectId": frame, "returnByValue": True,
                                                               "functionDeclaration": "function () {const b = this.getBoundingClientRect(); return [b.left + this.clientLeft, b.top + this.clientTop]}"}, session, timeout=3)
                left, top = (offset.get("result") or {}).get("value") or (0, 0)
                node = (await call("DOM.describeNode", {"objectId": frame}, session, timeout=3)).get("node") or {}
                frame_id = node.get("frameId")
                if not frame_id:
                    return None
                x, y = x - left, y - top
                expression = f"{PROBE}({json.dumps(mode)}, {x!r}, {y!r})"
                targets = (await call("Target.getTargets", timeout=3)).get("targetInfos") or []
                if any(t.get("targetId") == frame_id and t.get("type") == "iframe" for t in targets):
                    inner = (await call("Target.attachToTarget", {"targetId": frame_id, "flatten": True}, timeout=3))["sessionId"]
                    try:
                        result = (await call("Runtime.evaluate", {"expression": expression}, inner, timeout=3)).get("result") or {}
                    finally:
                        self.cdp.send("Target.detachFromTarget", {"sessionId": inner})
                    if result.get("subtype") == "node":
                        return "multiline"  # a frame inside a frame from another site: some text field
                else:
                    world = await call("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "hq-probe"}, session, timeout=3)
                    result = (await call("Runtime.evaluate", {"expression": expression, "contextId": world["executionContextId"],
                                                              "objectGroup": "hq-probe"}, session, timeout=3)).get("result") or {}
            return None
        finally:
            self.cdp.send("Runtime.releaseObjectGroup", {"objectGroup": "hq-probe"}, session)

    async def _sense(self, under=False, focus=False, settle=True):
        """Keeps ``field`` current: the text field under the pointer, the one with the focus."""
        if not self.session:
            return
        if focus and settle:
            await asyncio.sleep(0.12)  # the page's own focus handlers run first
        for key, wanted, at in (("under", under, self.at), ("focus", focus, None)):
            if not wanted or (key == "under" and at is None):
                continue
            try:
                kind = await self._probe("point" if key == "under" else "focus", at)
            except Exception:  # noqa: BLE001 - a page mid-navigation; the next input asks again
                kind = None
            kind = kind if kind in FIELD_KINDS else None
            if self.field[key] != kind:
                self.field = {**self.field, key: kind}
                self.changed = True

    async def _lift(self, at=None):
        """Ends a drag (a give-back or a lost viewer never leaves the page's button held)."""
        if not self.pressed or not self.session:
            self.pressed = False
            return
        self.pressed = False
        x, y = at or self.at or (0, 0)
        try:
            await self.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1}, self.session, timeout=3)
        except Exception:  # noqa: BLE001
            pass

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

    async def _attach(self, target):
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
        self.hidden, self.seen_front = False, False
        # Chrome paints only its front tab: a background tab sends no screencast frames and is shown with stills.
        # Watching never brings a tab forward (that would hide the tab the bot or a subagent works in, and a hidden
        # tab gets no animation frames, so its page stalls). The person's tab comes forward while they hold it.
        if self._holding():
            await self._bring_forward(target)
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

    async def _bring_forward(self, target):
        """Show the person's tab while they hold the browser, remembering the tab that was in front."""
        before = self.fronted[1] if self.fronted else await self._front_tab()
        try:
            await self.cdp.call("Page.bringToFront", {}, self.session, timeout=3)
            self.fronted = (target, before)
        except Exception:  # noqa: BLE001
            pass

    async def _restore_front(self):
        """The person handed the browser back: the tab that was in front before comes back to the front."""
        fronted, self.fronted = self.fronted, None
        if not fronted or not fronted[1] or fronted[1] == fronted[0] or not any(t["id"] == fronted[1] for t in self.tabs):
            return
        try:
            await self.cdp.call("Target.activateTarget", {"targetId": fronted[1]}, timeout=3)
        except Exception:  # noqa: BLE001 - closed meanwhile
            pass

    async def _front_tab(self):
        """The tab in front of the bot's Chrome: Chrome lists its tabs most recently activated first."""
        found = re.match(r"^ws://((?:127\.0\.0\.1|localhost|\[::1\]):\d{1,5})/devtools/browser/", self.record.get("endpoint") or "")
        if not found:
            return None
        try:
            listing = await asyncio.to_thread(_get_json, f"http://{found.group(1)}/json/list")
        except Exception:  # noqa: BLE001
            return None
        tabs = self.cdp_module.page_tabs([{**t, "targetId": t.get("id")} for t in listing if isinstance(t, dict)])
        return tabs[0]["id"] if tabs else None

    async def _check_front(self):
        """Whether the watched tab is in front. Following the bot, a tab it moved away from (a Browser Use tab
        switch) hands the view to the tab it brought forward."""
        try:
            found = await self.cdp.call("Runtime.evaluate", {"expression": "document.visibilityState", "returnByValue": True},
                                        self.session, timeout=2)
        except Exception:  # noqa: BLE001 - mid-navigation; asked again next second
            return
        self.hidden = (found.get("result") or {}).get("value") == "hidden"
        if not self.hidden:
            self.seen_front = True
            return
        if self.seen_front and self.follow and not self._holding():
            self.seen_front = False
            front = await self._front_tab()
            if front and front != self.tab and any(t["id"] == front for t in self.tabs):
                self.tab, self.switched_at = front, time.monotonic()

    async def _still(self):
        try:
            shot = await self.cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": LEVELS[self.level][1],
                                                                  "optimizeForSpeed": True}, self.session, timeout=5)
            metrics = await self.cdp.call("Page.getLayoutMetrics", session=self.session, timeout=3)
            view = metrics.get("cssVisualViewport") or {}
            if self.pending is None and shot.get("data"):
                self.pending = (shot["data"], {"deviceWidth": view.get("clientWidth"), "deviceHeight": view.get("clientHeight")}, None)
        except Exception:  # noqa: BLE001 - the next pass tries again
            pass

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
        """The tab the bot used last (opened, or moved to a new address in), else the one it last opened a page in,
        else its newest tab with a page in it, else any. Empty tabs only when there is nothing else."""
        pages = {t["id"]: t for t in self.tabs if not blank(t["url"])}
        try:
            used = next((tab for tab in self.recent() if tab in pages), None)
        except Exception:  # noqa: BLE001
            used = None
        if used:
            return used
        url = self.record.get("url")
        match = [t for t in self.tabs if url and t["url"] == url]
        return (match or list(pages.values()) or self.tabs or [{"id": None}])[-1]["id"]

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
        try:
            self.cdp = await self.cdp_module.Cdp().connect(self.record["endpoint"])
        except Exception as error:  # noqa: BLE001 - refused, timed out or a bad handshake: Chrome isn't answering
            raise Unresponsive(type(error).__name__) from error
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
                    # Chrome dropped this connection. Gone for good, or restarting (the watchdog ends a frozen
                    # one): the phone comes back to whichever it finds.
                    self.ended = self._still_open()
                    if not self.ended:
                        raise Unresponsive("browser connection closed")
                if now - self.framed_at > QUIET_CHECK and now - self.checked_chrome > QUIET_CHECK:
                    # No frame for a while: a still page, or a Chrome that stopped answering. Ask it something cheap.
                    self.checked_chrome = now
                    try:
                        await self.cdp.call("Browser.getVersion", timeout=4)
                    except asyncio.TimeoutError as error:
                        raise Unresponsive("no answer") from error
                    except self.cdp_module.CdpError:
                        pass  # closed: handled on the next pass
                if now - checked >= 1.0:
                    checked = now
                    refreshed = self.refresh()
                    if asyncio.iscoroutine(refreshed):
                        await refreshed
                    self.ended = self.ended or self._still_open()
                    self._advance_claim()
                    holding = self._holding()
                    if holding and self.session and (not self.fronted or self.fronted[0] != self.attached):
                        await self._bring_forward(self.attached)  # control just came to this person
                    elif not holding and self.fronted:
                        await self._restore_front()
                    if self.session and not holding:
                        await self._check_front()
                    if self.claim is not None and self._state()["field"] is not None:
                        await self._sense(focus=True, settle=False)  # the page moves its focus by itself too
                    self.changed = True  # control files and asks change outside this socket
                if self.ended:
                    await self.send_text({"type": "ended", "reason": self.ended})
                    return
                if self.superseded:
                    return  # the same phone came back on a new socket: this one is a husk
                if self.seq and self.framed_at - self.heard_at > PHONE_SILENT:
                    return  # frames kept going out and nothing came back: a half-open socket, not a phone
                if self.overflowed:
                    self.overflowed = False
                    await self.send_text({"type": "error", "message": "Some of your input didn't reach the page. Check it and try again."})
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
                still_after = STILL_HIDDEN if self.hidden else STILL_AFTER
                if (self.session and self.pending is None and not self.in_flight
                        and now - self.framed_at > still_after and now - self.stilled_at > still_after):
                    # Chrome sent nothing for a while (a page it isn't painting): a still keeps the view honest.
                    self.stilled_at = now
                    await self._still()
                if self.pending is not None and len(self.in_flight) < IN_FLIGHT:
                    await self._send_frame()
                    self.framed_at = now
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
            await self._lift()
            if self.ended:  # nothing left to hold
                self._release()
            if self.fronted and not self._holding():
                try:
                    await self._restore_front()
                except Exception:  # noqa: BLE001
                    pass
            await self.cdp.close()

    async def _run_actions(self):
        while True:
            while not self.inbox:
                self.inbox_ready.clear()
                await self.inbox_ready.wait()
            message = self.inbox.popleft()
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


def _get_json(url: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:  # noqa: S310 - loopback only
        return json.loads(response.read(4 * 1024 * 1024))


async def tabs(module, endpoint: str) -> list[dict]:
    cdp = await module.Cdp().connect(endpoint, timeout=2)
    try:
        return module.page_tabs((await cdp.call("Target.getTargets", timeout=2)).get("targetInfos") or [])
    finally:
        await cdp.close()
