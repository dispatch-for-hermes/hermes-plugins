"""Mounted by ``hermes serve`` at ``/api/plugins/hermes-hq-browser`` (behind the dashboard login).

GET  /health                               what this gateway supports
GET  /sessions?owner=&session=a,b          live browsers, optionally one profile's or some chats' only
GET  /bots                                 each bot's own Chrome: ready, starting, not responding, or why it has none
WS   /activity?owner=                      the same list, pushed when it changes (no page images)
WS   /sessions/{id}/watch                  the live view and, while held, the person's input (stream.py)
"""
import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

log = logging.getLogger("hermes-hq-browser")
_ROOT = Path(__file__).parents[1]
VERSION = "3.4.4"
RETAIN_ENDED = 120.0
MAX_VIEWERS = 8
VIEWER_ID = re.compile(r"^[0-9a-f]{32}$")


def _load(name):
    key = f"hermes_hq_browser_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, _ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


spool = _load("spool")
cdp = _load("cdp")
stream = _load("stream")
fleet = _load("fleet")
tabs = _load("tabs")
tracker = tabs.Tracker(cdp)
router = APIRouter()
_viewers = 0
_supervisor = None


def _supervise():
    """Every bot gets this plugin and a Chrome of its own (fleet.py). One dashboard process per machine does it;
    ``HERMES_HQ_BROWSER_OWN_CHROME=0`` (or the older ``DISPATCH_BROWSER_OWN_CHROME=0``) turns it off."""
    global _supervisor
    if fleet.setting("OWN_CHROME", "1") == "0":
        return
    try:
        supervisor = fleet.Supervisor(fleet.hermes_root(), _ROOT)
        supervisor.idle_for = _idle_for
        if supervisor.begin():
            _supervisor = supervisor
    except Exception:  # noqa: BLE001 - watching still works without it
        log.warning("hermes-hq-browser: couldn't start looking after the bots' Chromes", exc_info=True)


_watching: dict = {}  # profile -> phones watching its browser right now


def _idle_for(profile) -> float:
    """Seconds since anyone used this bot's browser: 0 while a call runs, a person holds or was asked for it, or a
    phone watches it. The supervisor starts an old Chrome afresh only once this is long enough."""
    now = time.time()
    if _watching.get(profile):
        return 0.0
    last = 0.0
    for ident, record in spool.entries("browsers"):
        if record.get("profile") != profile or not spool.live(record, now):
            continue
        ask = spool.read(spool.folder("asks") / f"{ident}.json")
        if (int(record.get("busy") or 0) > 0 or spool.control(ident, now=now) is not None
                or (ask and float(ask.get("expires_at") or 0) > now)):
            return 0.0
        last = max(last, float(record.get("agent_at") or 0))
    home = dict(_profile_homes()).get(profile)
    url = _standing_url(home) if home else None
    if url:
        last = max(last, tracker.last_used(url))
    return now - last if last else float("inf")


_supervise()


def _missing():
    try:
        import websockets  # noqa: F401
    except ImportError:
        return "websockets"
    return None


def _drift():
    try:
        return _load("agent").problems()
    except Exception as error:  # noqa: BLE001
        return [f"agent side unavailable ({type(error).__name__})"]


def _ws_allowed(ws) -> bool:
    """Websocket upgrades skip the dashboard's HTTP login, so check it here. Never fail open."""
    try:
        from hermes_cli import web_server_chat as chat
        return bool(chat._ws_auth_ok(ws)) and bool(chat._ws_request_is_allowed(ws))
    except Exception:  # noqa: BLE001
        return False


_standing_at = 0.0
_standing_lock = threading.Lock()
_standing_seen: dict = {}  # standing id -> (endpoint, when its Chrome last answered)
STANDING_GRACE = 20.0  # a Chrome busy for a moment stays listed this long
LOCAL_CDP = ("ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:", "http://127.0.0.1:", "http://localhost:")


def _profile_homes():
    return fleet.profiles(fleet.hermes_root())  # the bots Hermes itself lists (no deleted or half-made ones)


def _standing_url(home: Path):
    try:
        import yaml
        config = yaml.safe_load((home / "config.yaml").read_text()) or {}
        url = str(((config.get("browser") or {}).get("cdp_url") or "")).strip()
    except Exception:  # noqa: BLE001
        return None
    return url if url.startswith(LOCAL_CDP) else None


def _debugger_url(url: str):
    if "/devtools/browser/" in url:
        return url
    import urllib.request
    try:
        with urllib.request.urlopen(url.replace("ws://", "http://").rstrip("/") + "/json/version", timeout=2) as response:  # noqa: S310 - loopback only
            found = json.loads(response.read(65536)).get("webSocketDebuggerUrl")
        return found if isinstance(found, str) and cdp.LOOPBACK.match(found) else None
    except Exception:  # noqa: BLE001
        return None


def _port_open(url: str) -> bool:
    import socket
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url.replace("ws://", "http://"))
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def _publish_standing(force=False):
    """List each bot's standing browser (the Chrome its config connects it to) while its port answers, so a
    person can open it and sign in before the bot has used it. Its pause promise is the dashboard's to give for
    itself; any agent using that Chrome publishes its own record, and a claim waits for those too.
    Blocking (it asks each Chrome over HTTP): call it off the event loop, as ``_standing_async`` does."""
    global _standing_at
    if not _standing_lock.acquire(blocking=force):
        return  # another thread is publishing right now
    try:
        now = time.time()
        if not force and now - _standing_at < 3.0:
            return
        _standing_at = now
        _write_standing(now)
    finally:
        _standing_lock.release()


async def _standing_async(force=False):
    await asyncio.to_thread(_publish_standing, force)


def _write_standing(now):
    homes = [(profile, home, _standing_url(home)) for profile, home in _profile_homes()]
    for profile, home, url in homes:
        if not url or _copied(profile, url, homes):
            continue
        try:  # one bot's trouble never hides the rest
            _write_one_standing(profile, url, now)
        except Exception:  # noqa: BLE001
            log.debug("hermes-hq-browser: standing browser for %s skipped", profile, exc_info=True)


def _copied(profile, url, homes) -> bool:
    """A bot pointing at another bot's Chrome (a clone copies browser.cdp_url, until fleet.py gives it its own) isn't
    listed with it: the bot the Chrome belongs to (its state file names the port) or, without one, the first is."""
    port = fleet.port_of(url)
    sharing = [p for p, _, other in homes if other and fleet.port_of(other) == port]
    if len(sharing) < 2:
        return False
    root = fleet.hermes_root()
    owner = next((p for p in sharing if fleet.read_state(root, p).get("port") == port), sharing[0])
    return profile != owner


def _write_one_standing(profile, url, now):
    tracker.watch(url)  # which tab the bot uses, from now on (tabs.py)
    ident = spool.standing_id(profile, url)
    endpoint = _debugger_url(url)
    if endpoint:
        _standing_seen[ident] = (endpoint, now)
    else:
        # Not answering. A frozen Chrome still holds its port (the phone should hear "not responding", and its
        # watchdog restarts it); one that is gone is listed a little longer in case it is coming straight back.
        endpoint, seen = _standing_seen.get(ident, (None, 0.0))
        if not endpoint or (now - seen > STANDING_GRACE and not _port_open(url)):
            return
    claim = spool.control(ident, now=now)
    spool.write(spool.folder("browsers") / f"{ident}.json", {
        "schema": spool.SCHEMA, "id": ident, "generation": "standing", "pid": os.getpid(), "profile": profile,
        "source": ["cdp", url], "keys": [], "session_ids": [], "session_id": "", "endpoint": endpoint, "standing": True,
        "opened_at": now, "agent_at": 0, "status": "live", "url": None, "busy": 0,
        "paused": claim["epoch"] if claim else None, "seen_at": now})


def _live_records():
    """Published browsers whose owner still vouches for them, one per Chrome, newest bot use first. A Chrome is
    published more than once when several processes use it (a shared real-profile Chrome) and when it is a bot's
    standing Chrome (the dashboard lists it, and each agent using it publishes its own record). One row stands for
    them all: the standing record when there is one, so a phone keeps watching the same id while bot turns come and
    go and the watchdog restarts a frozen Chrome; it carries the newest bot use, every running call and chat."""
    now = time.time()
    groups = {}
    for ident, record in spool.entries("browsers"):
        if not spool.live(record, now):
            if now - float(record.get("seen_at") or 0) > RETAIN_ENDED:
                for kind in ("browsers", "control", "asks"):
                    spool.remove(spool.folder(kind) / f"{ident}.json")
            continue
        if record.get("id") != ident or not cdp.LOOPBACK.match(record.get("endpoint") or ""):
            continue
        groups.setdefault(_chrome_of(record), []).append(record)
    rows = []
    for group in groups.values():
        agents = sorted((r for r in group if not r.get("standing")), key=lambda r: -float(r.get("agent_at") or 0))
        standing = next((r for r in group if r.get("standing")), None)
        newest = agents[0] if agents else standing
        row = dict(standing or newest)
        row.update(busy=sum(int(r.get("busy") or 0) for r in group), group=[r["id"] for r in group],
                   session_ids=list(dict.fromkeys(sid for r in reversed(agents) for sid in (r.get("session_ids") or [])))[-16:])
        if newest is not standing:
            row.update(agent_at=newest.get("agent_at"), url=newest.get("url"), session_id=newest.get("session_id"))
        rows.append(row)
    return sorted(rows, key=lambda r: -float(r.get("agent_at") or 0))


def _chrome_of(record):
    """Which Chrome a record is: its discovery root when it has one (stable across restarts), else its endpoint."""
    source = record.get("source") or []
    if len(source) == 2 and source[0] == "cdp" and "/devtools/browser/" not in str(source[1]):
        return str(source[1]).rstrip("/").replace("ws://", "http://").replace("localhost", "127.0.0.1")
    return record.get("endpoint")


def _siblings(record):
    """Every live published copy of this record's Chrome: a person's claim must pause all of them."""
    chrome = _chrome_of(record)
    idents = [ident for ident, other in spool.entries("browsers")
              if (other.get("endpoint") == record.get("endpoint") or _chrome_of(other) == chrome) and spool.live(other)]
    return idents or [record["id"]]


def _ask_of(idents, now):
    for ident in idents:
        ask = spool.read(spool.folder("asks") / f"{ident}.json")
        reason = ask.get("reason") if ask and float(ask.get("expires_at") or 0) > now else None
        if isinstance(reason, str):
            return reason
    return None


def _describe(record):
    now = time.time()
    claim = spool.control(record["id"], now=now)
    control = "bot" if claim is None else "person" if claim["state"] == "controlled" else "waiting"
    return {**stream.public(record), "control": control, "ask": _ask_of(record.get("group") or [record["id"]], now)}


def _listing(owner="", sessions=()):
    rows = []
    for record in _live_records():
        if owner and record.get("profile") != owner:
            continue
        if sessions and not set(sessions) & set(record.get("session_ids") or [record.get("session_id")]):
            continue
        rows.append(_describe(record))
    return {"schema": "dispatch-browser.list.v3", "protocol": stream.PROTOCOL, "browsers": rows[:24]}


def _record(ident):
    if not spool.ID.match(ident):
        raise HTTPException(404, "No such browser")
    _publish_standing(force=True)  # blocking: watch() calls this from a thread
    record = spool.read(spool.folder("browsers") / f"{ident}.json")
    if record is None or record.get("id") != ident or not spool.live(record) or not cdp.LOOPBACK.match(record.get("endpoint") or ""):
        raise HTTPException(404, "This browser has closed")
    # A Chrome found through its discovery root (browser.cdp_url) gets a new browser id when it restarts on the same
    # port; ask it now rather than trust an endpoint the bot side published before the restart.
    source = record.get("source") or []
    if len(source) == 2 and source[0] == "cdp" and str(source[1]).startswith(LOCAL_CDP) and "/devtools/browser/" not in str(source[1]):
        fresh = _debugger_url(str(source[1]))
        if fresh:
            record = {**record, "endpoint": fresh}
    return record


@router.get("/health")
def health():
    missing, drift = _missing(), _drift()
    return {"ok": True, "schema": "dispatch-browser.health.v3", "version": VERSION, "protocol": stream.PROTOCOL,
            "installed": not missing and not drift, "missing": missing, "drift": drift,
            "features": {"watch": True, "control": True, "ask": True, "own_chrome": _own_chrome()}}


def _own_chrome() -> bool:
    return _supervisor is not None or fleet.setting("OWN_CHROME", "1") != "0"


@router.get("/bots")
def bots():  # sync: FastAPI runs it on a worker thread
    """Each bot's own Chrome: ``ready``, ``starting``, ``not_responding``, ``external`` (a Chrome the person runs),
    or why it has none: ``off`` (plugin disabled there), ``real_profile``, ``remote``, ``fixed``, ``camofox``,
    ``released`` (the person removed its ``cdp_url``), ``no_chrome``, ``port_taken``, ``no_port``."""
    if _supervisor is not None and _supervisor.statuses:
        rows = list(_supervisor.statuses.values())
    else:
        rows = fleet.statuses(fleet.hermes_root())
    return {"schema": "dispatch-browser.bots.v1", "own_chrome": _own_chrome(), "bots": rows}


@router.get("/sessions")
def sessions(owner: str = "", session: str = ""):  # sync: FastAPI runs it on a worker thread
    _publish_standing()
    wanted = tuple(s for s in session.split(",") if s)[:32]
    return _listing(owner[:128], wanted)


@router.websocket("/activity")
async def activity(ws: WebSocket):
    if not _ws_allowed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    owner = (ws.query_params.get("owner") or "")[:128]
    last, pinged = None, time.monotonic()

    async def drain():
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return

    closed = asyncio.create_task(drain())
    try:
        while not closed.done():
            await _standing_async()
            listing = await asyncio.to_thread(_listing, owner)
            if listing != last:
                last = listing
                await ws.send_json(listing)
            elif time.monotonic() - pinged > 20:
                pinged = time.monotonic()
                await ws.send_json({"type": "ping"})
            await asyncio.wait([closed], timeout=1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        closed.cancel()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/sessions/{ident}/watch")
async def watch(ws: WebSocket, ident: str):
    global _viewers
    if not _ws_allowed(ws):
        await ws.close(code=4401)
        return
    try:
        record = await asyncio.to_thread(_record, ident)
    except HTTPException:
        log.info("hermes-hq-browser: watch %s refused: no such live browser", ident)
        await ws.close(code=4404, reason="This browser has closed")
        return
    if _missing() or _viewers >= MAX_VIEWERS:
        log.info("hermes-hq-browser: watch %s refused: %s", ident, _missing() or f"{_viewers} viewers open")
        await ws.close(code=1013, reason="Try again shortly")
        return
    await ws.accept()
    _viewers += 1

    async def receive():
        try:
            message = await ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if message.get("type") == "websocket.disconnect":
            return None
        return message.get("text") or ""

    viewer_id = uuid.uuid4().hex
    try:  # the phone names itself first, so a reconnect finds a browser it still holds
        first = await asyncio.wait_for(receive(), 5)
        hello = json.loads(first) if first else {}
        if isinstance(hello, dict) and hello.get("type") == "hello" and VIEWER_ID.match(str(hello.get("viewer") or "")):
            viewer_id = hello["viewer"]
    except (asyncio.TimeoutError, ValueError):
        pass
    source = record.get("source") or []
    chrome = str(source[1]) if len(source) == 2 and source[0] == "cdp" and "/devtools/browser/" not in str(source[1]) else ""
    viewer = stream.Viewer(spool, cdp, record, ws.send_json, _siblings, _standing_async,
                           recent=(lambda: tracker.recent(chrome)) if chrome else None)
    watched = record.get("profile")
    _watching[watched] = _watching.get(watched, 0) + 1  # an old Chrome isn't started afresh under a watching phone
    try:
        await viewer.run(receive, viewer_id)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as error:  # noqa: BLE001
        if ws.client_state.name != "CONNECTED" or ws.application_state.name != "CONNECTED":
            log.debug("hermes-hq-browser: the phone left %s mid-send", ident)  # nobody left to tell
            return
        # A Chrome that doesn't answer (busy, frozen or restarting) is the browser's trouble, not the stream's:
        # the phone says so and keeps trying.
        unresponsive = isinstance(error, (stream.Unresponsive, TimeoutError, asyncio.TimeoutError, OSError))
        log.log(logging.INFO if unresponsive else logging.WARNING, "hermes-hq-browser: stream for %s ended: %s",
                ident, type(error).__name__, exc_info=not unresponsive)
        try:
            await ws.send_json({"type": "ended", "reason": "unresponsive" if unresponsive else "error"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        _viewers -= 1
        _watching[watched] = max(0, _watching.get(watched, 1) - 1)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def _track_tabs():
    """Follow every bot's Chrome from the start, phone or not, so the tab a bot used is known when someone looks."""
    while True:
        try:
            for _, home in _profile_homes():
                url = _standing_url(home)
                if url:
                    tracker.watch(url)
        except Exception:  # noqa: BLE001
            log.debug("hermes-hq-browser: tab tracking setup failed", exc_info=True)
        _dump_stacks_if_asked()
        time.sleep(15)


def _dump_stacks_if_asked():
    """Support aid: touching <hermes root>/<spool>/dump-stacks writes every thread's stack beside it."""
    try:
        trigger = spool.root() / "dump-stacks"
        if not trigger.exists():
            return
        trigger.unlink()
        import traceback
        names = {t.ident: t.name for t in threading.enumerate()}
        lines = []
        for ident, frame in sys._current_frames().items():
            lines.append(f"--- {names.get(ident, ident)}")
            lines.extend(traceback.format_stack(frame))
        (spool.root() / "stacks.txt").write_text("".join(lines))
    except Exception:  # noqa: BLE001
        pass


if fleet.setting("OWN_CHROME", "1") != "0":
    threading.Thread(target=_track_tabs, name="hermes-hq-browser-track", daemon=True).start()
