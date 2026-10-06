"""Mounted by ``hermes serve`` at ``/api/plugins/dispatch-browser`` (behind the dashboard login).

GET  /health                               what this gateway supports
GET  /sessions?owner=&session=a,b          live browsers, optionally one profile's or some chats' only
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
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

log = logging.getLogger("dispatch-browser")
_ROOT = Path(__file__).parents[1]
VERSION = "3.0.0"
RETAIN_ENDED = 120.0
MAX_VIEWERS = 8
VIEWER_ID = re.compile(r"^[0-9a-f]{32}$")


def _load(name):
    key = f"dispatch_browser_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, _ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


spool = _load("spool")
cdp = _load("cdp")
stream = _load("stream")
router = APIRouter()
_viewers = 0


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
LOCAL_CDP = ("ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:", "http://127.0.0.1:", "http://localhost:")


def _profile_homes():
    from hermes_constants import get_default_hermes_root
    root = Path(get_default_hermes_root())
    yield "default", root
    for config in sorted((root / "profiles").glob("*/config.yaml")):
        yield config.parent.name, config.parent


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
        with urllib.request.urlopen(url.replace("ws://", "http://").rstrip("/") + "/json/version", timeout=0.4) as response:  # noqa: S310 - loopback only
            found = json.loads(response.read(65536)).get("webSocketDebuggerUrl")
        return found if isinstance(found, str) and cdp.LOOPBACK.match(found) else None
    except Exception:  # noqa: BLE001
        return None


def _publish_standing(force=False):
    """List each bot's standing browser (the Chrome its config connects it to) while its port answers, so a
    person can open it and sign in before the bot has used it. Its pause promise is the dashboard's to give for
    itself; any agent using that Chrome publishes its own record, and a claim waits for those too."""
    global _standing_at
    now = time.time()
    if not force and now - _standing_at < 3.0:
        return
    _standing_at = now
    for profile, home in _profile_homes():
        url = _standing_url(home)
        endpoint = _debugger_url(url) if url else None
        if not endpoint:
            continue
        ident = spool.standing_id(profile, url)
        claim = spool.control(ident, now=now)
        spool.write(spool.folder("browsers") / f"{ident}.json", {
            "schema": spool.SCHEMA, "id": ident, "generation": "standing", "pid": os.getpid(), "profile": profile,
            "source": ["cdp", url], "keys": [], "session_ids": [], "session_id": "", "endpoint": endpoint, "standing": True,
            "opened_at": now, "agent_at": 0, "status": "live", "url": None, "busy": 0,
            "paused": claim["epoch"] if claim else None, "seen_at": now})


def _live_records():
    """Published browsers whose owner still vouches for them; one per Chrome (a shared real-profile Chrome
    used from two processes is published twice), newest bot use first."""
    _publish_standing()
    now = time.time()
    found = {}
    for ident, record in spool.entries("browsers"):
        if not spool.live(record, now):
            if now - float(record.get("seen_at") or 0) > RETAIN_ENDED:
                for kind in ("browsers", "control", "asks"):
                    spool.remove(spool.folder(kind) / f"{ident}.json")
            continue
        if record.get("id") != ident or not cdp.LOOPBACK.match(record.get("endpoint") or ""):
            continue
        same = found.get(record["endpoint"])
        if same is None or float(record.get("agent_at") or 0) > float(same.get("agent_at") or 0):
            if same is not None:
                record = {**record, "busy": int(record.get("busy") or 0) + int(same.get("busy") or 0),
                          "session_ids": list(dict.fromkeys((same.get("session_ids") or []) + (record.get("session_ids") or [])))[-16:]}
            found[record["endpoint"]] = record
        else:
            found[record["endpoint"]] = {**same, "busy": int(same.get("busy") or 0) + int(record.get("busy") or 0)}
    return sorted(found.values(), key=lambda r: -float(r.get("agent_at") or 0))


def _siblings(record):
    """Every live published copy of this record's Chrome: a person's claim must pause all of them."""
    idents = [ident for ident, other in spool.entries("browsers")
              if other.get("endpoint") == record.get("endpoint") and spool.live(other)]
    return idents or [record["id"]]


def _describe(record):
    now = time.time()
    claim = spool.control(record["id"], now=now)
    ask = spool.read(spool.folder("asks") / f"{record['id']}.json")
    reason = ask.get("reason") if ask and float(ask.get("expires_at") or 0) > now else None
    control = "bot" if claim is None else "person" if claim["state"] == "controlled" else "waiting"
    return {**stream.public(record), "control": control, "ask": reason if isinstance(reason, str) else None}


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
    _publish_standing(force=True)
    record = spool.read(spool.folder("browsers") / f"{ident}.json")
    if record is None or record.get("id") != ident or not spool.live(record) or not cdp.LOOPBACK.match(record.get("endpoint") or ""):
        raise HTTPException(404, "This browser has closed")
    return record


@router.get("/health")
def health():
    missing, drift = _missing(), _drift()
    return {"ok": True, "schema": "dispatch-browser.health.v3", "version": VERSION, "protocol": stream.PROTOCOL,
            "installed": not missing and not drift, "missing": missing, "drift": drift,
            "features": {"watch": True, "control": True, "ask": True}}


@router.get("/sessions")
def sessions(owner: str = "", session: str = ""):
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
            listing = _listing(owner)
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
        record = _record(ident)
    except HTTPException:
        await ws.close(code=4404, reason="This browser has closed")
        return
    if _missing() or _viewers >= MAX_VIEWERS:
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
    viewer = stream.Viewer(spool, cdp, record, ws.send_json, _siblings, _publish_standing)
    try:
        await viewer.run(receive, viewer_id)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001
        log.debug("dispatch-browser: stream ended with an error", exc_info=True)
        try:
            await ws.send_json({"type": "ended", "reason": "error"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        _viewers -= 1
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
