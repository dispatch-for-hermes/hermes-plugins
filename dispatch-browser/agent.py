"""Agent-process side: publish the browsers this process's bots drive, and pause them while a person drives.

Hermes has no browser lifecycle hook, so ``pre_tool_call``/``post_tool_call`` watch the browser tools and
read Hermes' own browser session table (read-only) to learn which browser served a call. The Chrome
debugging endpoint comes from Chromium itself (a Hermes-managed browser's daemon pid → its Chrome child →
``DevToolsActivePort``) or from the session's own loopback ``cdp_url`` (the real-profile browser). Bots keep
every browser tool Hermes gives them; nothing is wrapped, overridden or patched. If the internals read here
change shape, ``problems()`` says so and the plugin stays off.

While a person holds a browser (``control/<id>.json``), ``pre_tool_call`` refuses that bot's browser tools,
so the bot cannot click under the person's finger. ``browser_ask_user`` lets a bot hand its browser to the
person (a login, a code, a CAPTCHA) and wait for it back.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("dispatch-browser")

BROWSER_TOOLS = frozenset({
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_console", "browser_get_images", "browser_vision", "browser_cdp", "browser_dialog",
    "browser_exec",
})
ASK_TOOL = "browser_ask_user"
BROWSER_EXEC = "browser_exec"
TICK = 1.0
HEARTBEAT = 3.0
BUSY_CAP = 450.0             # a call Hermes forgot to finish stops counting as running (its own limit is 420 s)
ASK_LIMIT = 390.0            # under Hermes' 420 s tool deadline
ASK_KEEPALIVE = 30.0         # renews Hermes' and the browser daemon's idle timers while the person works
GENERATION = uuid.uuid4().hex  # one per process: ids never collide across restarts

BLOCKED = ("The user has taken over your browser and is using it right now. Don't use browser tools until "
           "they hand it back. To wait for them, call browser_ask_user; otherwise carry on with other work.")

_spool = None
_lock = threading.Lock()
_browsers: dict[str, dict] = {}   # id -> record (this process's browsers only)
_keys: dict[str, str] = {}        # Hermes session key -> browser id
_pending: dict[str, dict] = {}    # session key -> call info, for a browser the running call has not opened yet
_busy: dict[str, dict] = {}       # browser id or "key:<session key>" -> {tool call id: start time} of running calls
_thread = None


def _modules():
    from tools import browser_tool, browser_tool_lifecycle, browser_tool_session
    return browser_tool, browser_tool_lifecycle, browser_tool_session


def problems() -> list[str]:
    """Hermes internals this side relies on, in the shapes it was written for (Hermes 0.21.5)."""
    found = []
    try:
        bt, _, session = _modules()
    except Exception as error:  # noqa: BLE001
        return [f"browser tools unavailable ({type(error).__name__})"]
    for name, kind in (("_active_sessions", dict), ("_last_active_session_key", dict), ("_session_owner_homes", dict)):
        if not isinstance(getattr(bt, name, None), kind):
            found.append(f"browser_tool.{name} changed")
    if not hasattr(getattr(bt, "_cleanup_lock", None), "acquire"):
        found.append("browser_tool._cleanup_lock changed")
    if not callable(getattr(bt, "_socket_safe_tmpdir", None)):
        found.append("browser_tool._socket_safe_tmpdir changed")
    try:
        parameters = list(inspect.signature(session._run_browser_command).parameters)
        if parameters[:3] != ["task_id", "command", "args"]:
            found.append("_run_browser_command signature changed")
    except (TypeError, ValueError, AttributeError):
        found.append("_run_browser_command unavailable")
    try:
        from tools import browser_use_cli
        if list(inspect.signature(browser_use_cli._backend_cache_key).parameters)[:2] != ["task_id", "session_name"]:
            found.append("browser_use_cli._backend_cache_key signature changed")
    except Exception:  # noqa: BLE001 - Browser Use mode is optional; built-in tools still work
        pass
    return found


def profile_name() -> str:
    from hermes_constants import get_default_hermes_root, get_hermes_home
    home, default = Path(get_hermes_home()).resolve(), Path(get_default_hermes_root()).resolve()
    return "default" if home == default else home.name


def _session_key(tool_name: str, args: dict, task_id: str) -> str:
    if tool_name == BROWSER_EXEC:
        from tools import browser_use_cli
        return browser_use_cli._backend_cache_key(task_id, str((args or {}).get("session") or ""))
    bt, _, _ = _modules()
    return bt._last_active_session_key.get(task_id) or task_id


def _session_info(key: str) -> dict | None:
    bt, _, _ = _modules()
    with bt._cleanup_lock:
        info = bt._active_sessions.get(key)
        return dict(info) if isinstance(info, dict) else None


def browser_of(info: dict | None) -> tuple[str, str] | None:
    """``("daemon", session name)`` for a Hermes-managed Chromium, ``("cdp", ws url)`` for a local browser
    Hermes attaches to (the real-profile Chrome, a local override), or None (cloud, Lightpanda, remote)."""
    if not info or info.get("bb_session_id"):
        return None
    name = info.get("session_name")
    if not isinstance(name, str) or not name or name.startswith("lp"):
        return None
    url = info.get("cdp_url")
    if url:
        url = str(url)
        local = url.startswith(("ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:", "http://127.0.0.1:", "http://localhost:"))
        return ("cdp", url) if local else None
    return ("daemon", name)


LOCAL_CDP = ("ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:", "http://127.0.0.1:", "http://localhost:")


def exec_source() -> tuple[str, str] | None:
    """Browser Use's own routes that leave no entry in the session table: a local ``/browser connect``
    Chrome (``browser.cdp_url`` / ``BROWSER_CDP_URL``, read without network I/O) or the real-profile Chrome
    Hermes launched on its profile copy (``<home>/browser-profile/<browser>/DevToolsActivePort``)."""
    try:
        from tools.browser_tool_cdp import _get_cdp_override_raw
        raw = str(_get_cdp_override_raw() or "")
    except Exception:  # noqa: BLE001
        raw = ""
    if raw:
        return ("cdp", raw) if raw.startswith(LOCAL_CDP) else None
    try:
        from tools.browser_use_cli import _real_profile_consented
        if not _real_profile_consented():
            return None
        from hermes_constants import get_hermes_home
        for active in sorted(Path(get_hermes_home(), "browser-profile").glob("*/DevToolsActivePort")):
            port, path = active.read_text().split("\n")[:2]
            if 0 < int(port) < 65536 and path.startswith("/devtools/browser/"):
                return ("cdp", f"ws://127.0.0.1:{int(port)}{path.strip()}")
    except (OSError, ValueError):
        pass
    return None


def _reachable(url: str) -> bool:
    """A loopback debugging port still accepts connections (cheap; no CDP)."""
    import socket
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def _ident(profile: str, kind: str, value: str) -> str:
    return hashlib.sha256(f"{GENERATION}\0{profile}\0{kind}\0{value}".encode()).hexdigest()[:24]


def endpoint(source: tuple[str, str], tmpdir: str | None = None) -> str | None:
    """``ws://127.0.0.1:<port>/devtools/browser/<id>`` of the browser, or None."""
    kind, value = source
    if kind == "cdp":
        if "/devtools/browser/" in value:
            return value
        try:  # an http discovery root
            with urllib.request.urlopen(value.rstrip("/") + "/json/version", timeout=2) as response:  # noqa: S310 - loopback only
                found = json.loads(response.read(65536)).get("webSocketDebuggerUrl")
            return found if isinstance(found, str) else None
        except Exception:  # noqa: BLE001
            return None
    if tmpdir is None:
        bt, _, _ = _modules()
        tmpdir = bt._socket_safe_tmpdir()
    try:
        daemon = int((Path(tmpdir) / f"agent-browser-{value}" / f"{value}.pid").read_text().strip())
        import psutil
        processes = psutil.Process(daemon).children(recursive=True)
    except Exception:  # noqa: BLE001 - not started yet, gone, or psutil missing
        return None
    for process in processes:
        try:
            arguments = process.cmdline()
        except Exception:  # noqa: BLE001
            continue
        if not any(a.startswith("--remote-debugging-port") for a in arguments) or any(a.startswith("--type=") for a in arguments):
            continue
        folder = next((a.split("=", 1)[1] for a in arguments if a.startswith("--user-data-dir=")), None)
        if not folder:
            continue
        try:
            port, path = (Path(folder) / "DevToolsActivePort").read_text().split("\n")[:2]
            port = int(port)
        except (OSError, ValueError):
            continue
        if 0 < port < 65536 and path.startswith("/devtools/browser/"):
            return f"ws://127.0.0.1:{port}{path.strip()}"
    return None


def _track(key: str, session_id: str, task_id: str, tool_name: str, url=None) -> str | None:
    """Record (or refresh) the browser behind one Hermes session key. Returns its id."""
    source = browser_of(_session_info(key))
    fallback = False
    if source is None and tool_name in (BROWSER_EXEC, ASK_TOOL):
        source, fallback = exec_source(), True
    if source is None:
        return None
    now = time.time()
    with _lock:
        known = _keys.get(key)
        if known in _browsers and tuple(_browsers[known]["source"]) == source:
            ident, profile = known, _browsers[known]["profile"]
        else:
            profile = profile_name()
            ident = _ident(profile, *source)
        record = _browsers.get(ident)
        if record is None:
            record = _browsers[ident] = dict(
                schema=_spool.SCHEMA, id=ident, generation=GENERATION, pid=os.getpid(), profile=profile,
                source=list(source), keys=[], session_ids=[], session_id=session_id or task_id, endpoint=None,
                opened_at=now, status="live", url=None, busy=0, paused=None, dirty=True,
                shared=source[0] == "cdp", fallback=fallback)
        if key not in record["keys"]:
            record["keys"].append(key)
        sid = session_id or task_id
        if sid and sid not in record["session_ids"]:
            record["session_ids"] = (record["session_ids"] + [sid])[-16:]
        record.update(session_id=sid or record["session_id"], agent_at=now, activity=tool_name, dirty=True)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            record["url"] = url[:2048]
        _keys[key] = ident
        moved = _busy.pop(f"key:{key}", None)
        if moved:
            _busy.setdefault(ident, {}).update(moved)
    return ident


def _busy_count(ident: str, now: float) -> int:
    """Running browser calls in a browser (caller holds ``_lock``); one Hermes forgot stops counting at BUSY_CAP."""
    calls = {call: started for call, started in _busy.get(ident, {}).items() if now - started < BUSY_CAP}
    _busy[ident] = calls
    return len(calls)


def _call_id(tool_call_id, tool_name: str) -> str:
    return str(tool_call_id) if tool_call_id else f"{threading.get_ident()}:{tool_name}"


def _standing() -> str | None:
    """This profile's standing browser id, when its config connects it to a local Chrome (``browser.cdp_url``)."""
    try:
        from tools.browser_tool_cdp import _get_cdp_override_raw
        raw = str(_get_cdp_override_raw() or "")
    except Exception:  # noqa: BLE001
        return None
    return _spool.standing_id(profile_name(), raw) if raw.startswith(LOCAL_CDP) else None


def controlled(ident: str | None) -> bool:
    """A person holds, or has asked for, this browser, or this profile's standing browser (the same Chrome, which
    the dashboard lists under its own id)."""
    return any(i and _spool.control(i) is not None for i in (ident, _standing()))


def before_tool(tool_name="", args=None, task_id="", session_id="", tool_call_id=None, **_):
    """``pre_tool_call``: refuse a browser tool while a person holds or has asked for that browser; count
    running calls. The claim check and the count happen under ``_lock``, the same lock ``tick`` holds while it
    tells the dashboard the bot has paused, so no call slips in after that promise."""
    if tool_name not in BROWSER_TOOLS or not task_id:
        return None
    try:
        key = _session_key(tool_name, args or {}, task_id)
        with _lock:
            ident = _keys.get(key)
        if ident is None:
            ident = _track(key, session_id, task_id, tool_name)
        now = time.time()
        with _lock:
            if controlled(ident):
                return {"action": "block", "message": BLOCKED}
            _busy.setdefault(ident or f"key:{key}", {})[_call_id(tool_call_id, tool_name)] = now
            if ident in _browsers:
                _browsers[ident].update(busy=_busy_count(ident, now), dirty=True)
            else:
                _pending[key] = {"session_id": session_id, "task_id": task_id, "tool": tool_name, "at": now}
        _start()
    except Exception:  # noqa: BLE001 - observation must never disturb a browser tool
        log.debug("dispatch-browser: pre_tool_call failed", exc_info=True)
    return None


def after_tool(tool_name="", args=None, result=None, task_id="", session_id="", tool_call_id=None, status="", **_):
    """``post_tool_call``: the call finished (or was refused); remember which chat the browser belongs to."""
    if tool_name not in BROWSER_TOOLS or not task_id:
        return None
    try:
        key = _session_key(tool_name, args or {}, task_id)
        call = _call_id(tool_call_id, tool_name)
        url = (args or {}).get("url") if tool_name == "browser_navigate" and status != "blocked" else None
        ident = _track(key, session_id, task_id, tool_name, url) if status != "blocked" else None
        now = time.time()
        with _lock:
            _pending.pop(key, None)
            for slot in list(_busy):
                if _busy[slot].pop(call, None) is not None:
                    ident = ident or (slot if slot in _browsers else None)
                    break
            if ident in _browsers:
                _browsers[ident].update(busy=_busy_count(ident, now), dirty=True)
        _start()
    except Exception:  # noqa: BLE001
        log.debug("dispatch-browser: post_tool_call failed", exc_info=True)
    return None


def _publish(record: dict) -> None:
    folder = _spool.folder("browsers")
    _spool.write(folder / f"{record['id']}.json", {k: v for k, v in record.items() if k != "dirty"})


def _open_keys(record: dict) -> list[str]:
    """This record's Hermes session keys whose browser is still the one recorded. A browser found through
    Browser Use's own routes stays open while it is still that route's browser and its port answers."""
    source = tuple(record["source"])
    if record.get("fallback"):
        endpoint_now = record.get("endpoint") or source[1]
        return list(record["keys"]) if exec_source() == source and _reachable(endpoint_now) else []
    return [key for key in record["keys"] if browser_of(_session_info(key)) == source]


def tick(now: float | None = None) -> None:
    """One pass: pick up browsers a running call opened, refresh endpoints and heartbeats, retire closed ones."""
    now = time.time() if now is None else now
    with _lock:
        pending = dict(_pending)
    for key, call in pending.items():
        if now - call["at"] > BUSY_CAP:
            with _lock:
                _pending.pop(key, None)
        elif _track(key, call["session_id"], call["task_id"], call["tool"]):
            with _lock:
                _pending.pop(key, None)
    with _lock:
        records = list(_browsers.values())
    for record in records:
        ident = record["id"]
        original = list(record["keys"])
        keys = _open_keys(record)
        if not keys:
            record.update(status="ended", ended_at=now, seen_at=now)
            _publish(record)
            with _lock:
                _browsers.pop(ident, None)
                for key in record["keys"]:
                    if _keys.get(key) == ident:
                        _keys.pop(key, None)
                _busy.pop(ident, None)
            continue
        with _lock:  # _track may be adding a key meanwhile: drop only the closed ones
            record["keys"] = [key for key in record["keys"] if key in keys or key not in original]
        if not record.get("endpoint") or not _reachable(record["endpoint"]):  # a restarted Chrome moves ports
            found = endpoint(tuple(record["source"]))
            if found != record.get("endpoint"):
                record.update(endpoint=found, dirty=True)
        with _lock:
            # The pause promise: a claim is on file and no browser call is running. before_tool checks the
            # claim under this same lock, so nothing starts after this is published.
            busy = _busy_count(ident, now)
            claim = _spool.control(ident, now=now)
            paused = claim["epoch"] if claim is not None and busy == 0 else None
            if record.get("fallback") or record.get("shared"):  # a claim made on the standing listing
                standing = _standing()
                held = _spool.control(standing, now=now) if standing else None
                if held is not None and claim is None:
                    paused = held["epoch"] if busy == 0 else None
        if busy != record.get("busy") or paused != record.get("paused"):
            record.update(busy=busy, paused=paused, dirty=True)
        if record.get("dirty") or now - float(record.get("seen_at") or 0) >= HEARTBEAT:
            record["seen_at"] = now
            record["dirty"] = False
            if record.get("endpoint"):
                _publish(record)


def _loop():
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 - the loop must outlive a torn read
            log.debug("dispatch-browser: tick failed", exc_info=True)
        time.sleep(TICK)


def _start():
    global _thread
    with _lock:
        if _thread is None:
            _thread = threading.Thread(target=_loop, name="dispatch-browser", daemon=True)
            _thread.start()


def _keep_alive(key: str) -> str | None:
    """Renew Hermes' and the daemon's idle timers through Hermes' own command path; the page's address.
    Only while the session still exists: on a missing session this path would launch a new browser."""
    if browser_of(_session_info(key)) is None:
        return None
    _, _, session = _modules()
    result = session._run_browser_command(key, "get", ["url"], timeout=10)
    data = result.get("data") if isinstance(result, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) else None


def _key_for_chat(sid: str) -> str | None:
    """The open session key of the browser this chat used most recently."""
    with _lock:
        records = sorted((r for r in _browsers.values() if sid and sid in r["session_ids"]),
                         key=lambda r: -float(r.get("agent_at") or 0))
    for record in records:
        keys = _open_keys(record)
        if keys:
            return keys[-1]
    return None


def ask_user(args=None, task_id="", session_id="", **_):
    """``browser_ask_user``: hand this bot's browser to the person and wait until they hand it back."""
    args = args if isinstance(args, dict) else {}
    reason = " ".join(str(args.get("reason") or "").split())[:200]
    if not reason:
        return json.dumps({"error": "Say what you need the user to do in the browser (reason)."})
    if not task_id:
        return json.dumps({"error": "No browser is open for this task."})
    key = _key_for_chat(session_id or task_id) or _session_key("browser_navigate", {}, task_id)
    if browser_of(_session_info(key)) is None:
        exec_key = _session_key(BROWSER_EXEC, args, task_id)
        key = exec_key if browser_of(_session_info(exec_key)) else key
    ident = _track(key, session_id, task_id, ASK_TOOL)
    if ident is None:
        return json.dumps({"error": "Open the page in your browser first, then ask the user to take over."})
    _start()
    path = _spool.folder("asks") / f"{ident}.json"
    started = time.time()
    _spool.write(path, {"id": ident, "reason": reason, "session_id": session_id or task_id, "pid": os.getpid(),
                        "generation": GENERATION, "asked_at": started, "expires_at": started + ASK_LIMIT})
    held = False
    kept = started
    url = None
    try:
        while True:
            time.sleep(1.0)
            now = time.time()
            claim = _spool.control(ident, now=now)
            marker = _spool.read(path)
            given = bool(marker and float(marker.get("handed_back_at") or 0) >= started)
            if claim is not None and claim.get("state") == "controlled":
                held = True
            if given or (held and claim is None):
                try:
                    url = _keep_alive(key)
                except Exception:  # noqa: BLE001
                    url = None
                return json.dumps({"status": "handed_back", "url": url,
                                   "message": "The user handed your browser back. Take a fresh snapshot before acting: the page may have changed."})
            with _lock:
                record = _browsers.get(ident)
            if record is None or not _open_keys(record):
                return json.dumps({"status": "closed", "message": "Your browser closed while you waited."})
            if now - kept >= ASK_KEEPALIVE:
                kept = now
                try:
                    _keep_alive(key)
                except Exception:  # noqa: BLE001
                    log.debug("dispatch-browser: keep-alive failed", exc_info=True)
            if now - started >= ASK_LIMIT:
                if held or claim is not None:
                    return json.dumps({"status": "still_controlled", "message": "The user is still using your browser. Call browser_ask_user again to keep waiting."})
                return json.dumps({"status": "not_taken", "message": "The user hasn't taken over your browser yet. Tell them what you need in your reply, or call browser_ask_user again to keep waiting."})
    finally:
        current = _spool.read(path)
        if current is not None and current.get("generation") == GENERATION and current.get("asked_at") == started:
            _spool.remove(path)


ASK_SCHEMA = {
    "name": ASK_TOOL,
    "description": ("Hand your browser to the user and wait until they give it back. Use it when a page needs the user "
                    "themself: signing in, a one-time code, a CAPTCHA, or approving a payment or purchase. They watch and "
                    "control your browser from the Dispatch app; your browser tools are paused while they do. Returns when "
                    "they hand it back (take a fresh snapshot then), or after about six minutes if they haven't finished."),
    "parameters": {"type": "object", "properties": {
        "reason": {"type": "string", "description": "What you need them to do, in a short sentence (shown to the user)."},
    }, "required": ["reason"]},
}


def ask_available() -> bool:
    try:
        return not problems()
    except Exception:  # noqa: BLE001
        return False


def install(spool_module) -> list[str]:
    global _spool
    _spool = spool_module
    found = problems()
    if found:
        log.warning("dispatch-browser: off, Hermes changed: %s", "; ".join(found))
    return found


def retire_all() -> None:
    """Process exit: mark this process's browsers ended so viewers close at once."""
    now = time.time()
    with _lock:
        records = list(_browsers.values())
        _browsers.clear()
    for record in records:
        try:
            record.update(status="ended", ended_at=now, seen_at=now)
            _publish(record)
        except Exception:  # noqa: BLE001
            pass


import atexit  # noqa: E402
atexit.register(retire_all)
