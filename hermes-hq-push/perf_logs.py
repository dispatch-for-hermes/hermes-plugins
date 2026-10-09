"""The Hermes HQ phone's performance log, kept on this computer (0.7).

The app records timings and events (socket opens and closes with their codes, app background/foreground, network
changes, request sizes and durations, page stalls, scroll jumps, dock height changes, memory, crash/hang reports) and
sends them in batches to POST /api/plugins/hermes-hq-push/logs, behind the dashboard's normal sign-in like every other
route here. This module writes them as JSON lines to

    <hermes root>/hq-logs/<device>/<YYYY-MM-DD>.jsonl      (HERMES_HQ_LOG_DIR overrides the folder)

for `scripts/hq-logs.py` (in the hermes-hq repo) to summarize. Nothing is sent anywhere else. The app never sends
message text, prompts, tool output, query strings, tokens or passwords; every line is cleaned again here anyway: only
numbers, flags and short strings under plain field names, never a field named like content or a credential.

Bounded: a day's file stops at DAY_BYTES (a `log.full` line says so), a device keeps DEVICE_BYTES and KEEP_DAYS at
most (oldest days removed first), and at most MAX_DEVICES device folders exist.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

DAY_BYTES = 16 * 1024 * 1024
DEVICE_BYTES = 128 * 1024 * 1024
KEEP_DAYS = 14
MAX_DEVICES = 8
MAX_LINES = 4000
MAX_LINE_BYTES = 2048
MAX_KEYS = 16
MAX_STRING = 120
DEVICE = re.compile(r"[A-Za-z0-9_]{1,24}-[0-9a-f]{8}")
KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,31}")
KIND = re.compile(r"[a-z][a-z0-9_.-]{0,39}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl")
# The same names the app drops (HermesPerfLog.swift PerfLogPolicy.blocked, src/perf-log.ts BLOCKED).
BLOCKED = frozenset({"text", "content", "body", "prompt", "message", "messages", "delta", "output", "input", "arguments",
                     "args", "url", "href", "query", "search", "token", "password", "passwd", "secret", "ticket", "cookie",
                     "authorization", "auth", "bearer", "key", "apikey", "title", "name", "username", "email", "transcript",
                     "answer", "value", "data"})


class LogError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def log_dir() -> Path:
    override = os.environ.get("HERMES_HQ_LOG_DIR")
    if override:
        return Path(override)
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "hq-logs"


def short(text: str) -> str:
    if "/" in text:
        text = re.split(r"[?#]", text, maxsplit=1)[0]
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:MAX_STRING]


def clean(event: object) -> dict | None:
    """One event as it is written: {t, k, s, ...fields}, or None when it isn't one."""
    if not isinstance(event, dict):
        return None
    kind, at = event.get("k"), event.get("t")
    if not isinstance(kind, str) or not KIND.fullmatch(kind):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at <= 0:
        return None
    out: dict = {"t": int(at), "k": kind, "s": "j" if event.get("s") == "j" else "n"}
    for name in sorted(event):
        if len(out) - 3 >= MAX_KEYS:
            break
        if name in ("t", "k", "s") or not isinstance(name, str) or not KEY.fullmatch(name) or name.lower() in BLOCKED:
            continue
        value = event[name]
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, (int, float)):
            if math.isfinite(value):
                out[name] = round(float(value), 1) if isinstance(value, float) else value
        elif isinstance(value, str):
            out[name] = short(value)
    return out


def write(root: Path, device: str, lines: list, app: str = "", reason: str = "", lost: bool = False, now: float | None = None) -> dict:
    """Writes one upload's lines under root/device/, by each event's own day (UTC). Returns counts."""
    if not DEVICE.fullmatch(device or ""):
        raise LogError(400, "Invalid device.")
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise LogError(413, "Too many lines in one upload.")
    now = time.time() if now is None else now
    folder = root / device
    if not folder.exists():
        root.mkdir(parents=True, exist_ok=True)
        devices = [entry for entry in root.iterdir() if entry.is_dir() and DEVICE.fullmatch(entry.name)]
        if len(devices) >= MAX_DEVICES:
            raise LogError(409, "Too many devices are logging to this computer.")
    folder.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    dropped = 0
    # Source "g": written by this plugin (an upload's header, a full day). A phone's lines are always "n" or "j".
    events = [{"t": int(now * 1000), "k": "upload", "s": "g", "n": len(lines), "lost": bool(lost), "app": short(str(app)), "reason": short(str(reason))}]
    for raw in lines:
        try:
            event = clean(json.loads(raw)) if isinstance(raw, str) and len(raw.encode()) <= MAX_LINE_BYTES else None
        except ValueError:
            event = None
        if event is None:
            dropped += 1
        else:
            events.append(event)
    for event in events:
        day = time.strftime("%Y-%m-%d", time.gmtime(event["t"] / 1000))
        by_day.setdefault(day, []).append(json.dumps(event, separators=(",", ":"), sort_keys=True))
    for day, items in sorted(by_day.items()):
        path = folder / f"{day}.jsonl"
        size = path.stat().st_size if path.exists() else 0
        if size >= DAY_BYTES:
            dropped += len(items)
            continue
        data = "".join(item + "\n" for item in items)
        if size + len(data.encode()) > DAY_BYTES:
            data = json.dumps({"t": int(now * 1000), "k": "log.full", "s": "g", "dropped": len(items)}) + "\n"
            dropped += len(items)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(data)
    prune(folder, now)
    return {"ok": True, "lines": len(lines), "dropped": dropped}


# Marked moments (0.7, `moments` in status): when the user marks a janky moment (the Settings button or a shake), the
# app sends a content-free bundle beside the log: frame times per second over the last minute and the page's layout
# as boxes (data-slot / role / tag names and geometry only). Written to <device>/moments/<marker>.json, rebuilt here
# from the fields below only, size-capped and pruned.
MOMENT_BYTES = 256 * 1024
MOMENT_FRAMES = 120
MOMENT_NODES = 600
MAX_MOMENTS = 100
MOMENTS_BYTES = 16 * 1024 * 1024
MARKER = re.compile(r"[0-9a-f]{8,16}")
SLOT = re.compile(r"[A-Za-z0-9_-]{1,48}")
ROLE = re.compile(r"[a-z]{1,24}")
TAG = re.compile(r"[a-z][a-z0-9-]{0,15}")
FRAME_KEYS = ("at", "n", "max", "over")
NODE_NUMBERS = ("d", "x", "y", "w", "h", "o", "z", "tx", "ty", "sx")


def _number(value) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if isinstance(value, int) else round(float(value), 3)


def clean_moment(bundle: object) -> dict:
    """A moment's bundle as written: only numbers in frames and nodes, only plain names for tag, slot and role."""
    if not isinstance(bundle, dict):
        raise LogError(400, "Invalid moment.")
    frames = []
    for item in bundle.get("frames") or []:
        if not isinstance(item, dict) or len(frames) >= MOMENT_FRAMES:
            continue
        frame = {key: _number(item.get(key)) for key in FRAME_KEYS}
        frames.append({key: value for key, value in frame.items() if value is not None})
    outline = []
    for item in bundle.get("outline") or []:
        if not isinstance(item, dict) or len(outline) >= MOMENT_NODES:
            continue
        tag = item.get("tag")
        if not isinstance(tag, str) or not TAG.fullmatch(tag):
            continue
        node: dict = {"tag": tag}
        for key, pattern in (("slot", SLOT), ("role", ROLE)):
            value = item.get(key)
            if isinstance(value, str) and pattern.fullmatch(value):
                node[key] = value
        for key in NODE_NUMBERS:
            value = _number(item.get(key))
            if value is not None:
                node[key] = value
        outline.append(node)
    ctx = bundle.get("ctx")
    out = {"v": 1, "frames": frames, "outline": outline,
           "ctx": clean({**ctx, "t": 1, "k": "ctx"}) if isinstance(ctx, dict) else None}
    if out["ctx"]:
        out["ctx"] = {key: value for key, value in out["ctx"].items() if key not in ("t", "k", "s")}
    spent = _number(bundle.get("outlineMs"))
    if spent is not None:
        out["outlineMs"] = spent
    return out


def write_moment(root: Path, device: str, marker: str, bundle: object, app: str = "", now: float | None = None) -> dict:
    """Writes one marked moment's bundle to root/device/moments/<marker>.json. Returns counts."""
    if not DEVICE.fullmatch(device or ""):
        raise LogError(400, "Invalid device.")
    if not MARKER.fullmatch(marker or ""):
        raise LogError(400, "Invalid marker.")
    now = time.time() if now is None else now
    moment = clean_moment(bundle)
    moment.update({"marker": marker, "device": device, "app": short(str(app)), "t": int(now * 1000)})
    data = json.dumps(moment, separators=(",", ":"), sort_keys=True)
    while len(data.encode()) > MOMENT_BYTES and moment["outline"]:
        moment["outline"] = moment["outline"][: len(moment["outline"]) // 2]
        moment["truncated"] = True
        data = json.dumps(moment, separators=(",", ":"), sort_keys=True)
    folder = root / device
    if not folder.exists():
        root.mkdir(parents=True, exist_ok=True)
        devices = [entry for entry in root.iterdir() if entry.is_dir() and DEVICE.fullmatch(entry.name)]
        if len(devices) >= MAX_DEVICES:
            raise LogError(409, "Too many devices are logging to this computer.")
    moments = folder / "moments"
    moments.mkdir(parents=True, exist_ok=True)
    (moments / f"{marker}.json").write_text(data + "\n", encoding="utf-8")
    prune_moments(moments, now)
    return {"ok": True, "frames": len(moment["frames"]), "nodes": len(moment["outline"])}


def prune_moments(folder: Path, now: float) -> None:
    """Oldest first: anything past KEEP_DAYS, then beyond MAX_MOMENTS files or MOMENTS_BYTES."""
    files = sorted((entry for entry in folder.iterdir() if entry.is_file() and entry.suffix == ".json"), key=lambda entry: entry.stat().st_mtime)
    for entry in list(files):
        if entry.stat().st_mtime < now - KEEP_DAYS * 86400 and len(files) > 1:
            entry.unlink(missing_ok=True)
            files.remove(entry)
    total = sum(entry.stat().st_size for entry in files)
    while len(files) > 1 and (len(files) > MAX_MOMENTS or total > MOMENTS_BYTES):
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)


def prune(folder: Path, now: float) -> None:
    """Oldest days first: anything past KEEP_DAYS, then while the device holds more than DEVICE_BYTES."""
    days = sorted(entry for entry in folder.iterdir() if entry.is_file() and DAY.fullmatch(entry.name))
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_DAYS * 86400))
    for entry in list(days):
        if entry.name[:10] < cutoff and len(days) > 1:
            entry.unlink(missing_ok=True)
            days.remove(entry)
    total = sum(entry.stat().st_size for entry in days)
    while total > DEVICE_BYTES and len(days) > 1:
        oldest = days.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)

