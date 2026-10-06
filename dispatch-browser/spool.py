"""The files the agent processes and the dashboard share under ``<hermes root>/dispatch-browser``.

A browser lives in the process that runs its agent (the messaging gateway, the dashboard's own chat
gateway, a profile's chat child, cron). The dashboard that serves Dispatch may be another process, so
the two sides talk through small owner-only JSON files:

- ``browsers/<id>.json``  written by the owning process: where the browser is, whose it is, and whether
                          a browser tool call is running in it right now (``busy``).
- ``control/<id>.json``   written by the dashboard: a person asked for (``requested``) or holds
                          (``controlled``) this browser. The owning process refuses the bot's browser
                          tools while it exists and has not expired.
- ``asks/<id>.json``      written by the owning process while its bot waits in ``browser_ask_user``.
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from pathlib import Path

SCHEMA = 2
ID = re.compile(r"^[0-9a-f]{24}$")
HEARTBEAT_STALE = 12.0  # an owner that stopped refreshing its record for this long is gone
MAX_FILE = 64 * 1024


def root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root()) / "dispatch-browser"


def folder(kind: str, base: Path | None = None) -> Path:
    path = (base or root()) / kind
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write(path: Path, value: dict) -> None:
    """Atomic owner-only write."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(value, file, separators=(",", ":"))
    os.replace(temp, path)


def read(path: Path) -> dict | None:
    """A spool file this user wrote, or None (missing, foreign, a link, too big or torn)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_FILE:
            return None
        with os.fdopen(descriptor, "r") as file:
            descriptor = None
            value = json.load(file)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def entries(kind: str, base: Path | None = None):
    """(id, value) for every well-formed file of one kind."""
    try:
        names = os.listdir(folder(kind, base))
    except OSError:
        return
    for name in names:
        ident = name[:-5] if name.endswith(".json") else ""
        if ID.match(ident):
            value = read(folder(kind, base) / name)
            if value is not None:
                yield ident, value


def alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def live(record: dict, now: float | None = None) -> bool:
    """The owner still runs and still vouches for this browser."""
    now = time.time() if now is None else now
    return (record.get("status") == "live" and alive(record.get("pid"))
            and now - float(record.get("seen_at") or 0) < HEARTBEAT_STALE)


def standing_id(profile: str, url: str) -> str:
    """The id of a bot's standing browser: the Chrome its config connects it to (``browser.cdp_url``), listed by the
    dashboard even before the bot uses it. The agent side computes the same id to honor a person's claim on it."""
    import hashlib
    return hashlib.sha256(f"standing\0{profile}\0{url}".encode()).hexdigest()[:24]


def control(ident: str, base: Path | None = None, now: float | None = None) -> dict | None:
    """The person's current claim on a browser, or None when nobody has asked or the claim lapsed."""
    now = time.time() if now is None else now
    value = read(folder("control", base) / f"{ident}.json")
    if (value is None or value.get("state") not in ("requested", "controlled")
            or not isinstance(value.get("epoch"), int) or float(value.get("expires_at") or 0) <= now):
        return None
    return value
