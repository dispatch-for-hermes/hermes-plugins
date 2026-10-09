"""Hermes HQ push core: device store, payloads, action tokens and APNs delivery.

No Hermes imports at module level, so the hooks (``__init__.py``), the dashboard routes
(``dashboard/plugin_api.py``, a separate module instance in ``hermes serve``) and the tests all
load it the same way. Alerts read like the app's own (alert_copy.py, docs/notifications.md): who (the bot)
and what (a masked, clipped preview of the reply or the command awaiting approval), or only who when the
device turned Show Previews off. Action tokens, keys and secrets never ride a payload.

A device that gives a seal key (every Hermes HQ build with the notification extension) gets its alerts sealed:
AES-256-GCM with that key, so Apple, and the relay in relay mode, see only "Hermes HQ: New notification" and the
phone's extension opens the real alert (docs/push-notifications.md, "Sealed alerts").

Until 0.5 this plugin was called dispatch-push. It adopts that plugin's state on first use (data_dir), reads its
DISPATCH_* settings when the HERMES_HQ_* ones aren't set (env), and silences a copy of it still loaded in the same
server so a phone isn't alerted twice (quiet_predecessor).
"""
from __future__ import annotations

import base64
import hashlib
import heapq
import hmac
import importlib.util
import itertools
import json
import logging
import os
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

NAME = "hermes-hq-push"
# The name this plugin had until 0.5, the module name it loads its core under, and its state directory's name.
PREDECESSOR = "dispatch-push"
PREDECESSOR_MODULE = "dispatch_push_core"

log = logging.getLogger(NAME)

_copy_spec = importlib.util.spec_from_file_location("hermes_hq_push_copy", Path(__file__).with_name("alert_copy.py"))
alert_copy = importlib.util.module_from_spec(_copy_spec)
_copy_spec.loader.exec_module(alert_copy)

KINDS = ("turnDone", "approval")
TOPIC = "com.charlesmcdowell.dispatch"
APPROVAL_CATEGORY = "DISPATCH_APPROVAL"
CHOICES = ("once", "deny")
# Notification Center order, as the app's local alerts: what needs an answer first.
RELEVANCE = {"approval": 1.0, "turnDone": 0.6}
APP_PLATFORMS = {"desktop", "tui"}
# Sealed alerts: what shows if the phone can't open one, and the associated data both sides bind the box to.
SEALED_ALERT = {"title": "Hermes HQ", "body": "New notification"}
# Unchanged by the rename: the phone opens boxes bound to it (DispatchSeal.aad).
SEAL_AAD = b"dispatch-push seal v1"
SEALS = importlib.util.find_spec("cryptography") is not None
# The public relay that holds the app's APNs key for every gateway that doesn't (a Cloudflare Worker, push-relay/).
DEFAULT_RELAY_URL = "https://push.getdispatchapp.com"


def env(name: str) -> str | None:
    """A setting: HERMES_HQ_<name>, or the DISPATCH_<name> it was called before 0.5, so an existing setup keeps working."""
    value = os.getenv("HERMES_HQ_" + name)
    return value if value is not None else os.getenv("DISPATCH_" + name)


def data_dir() -> Path:
    """Root-level (not per-profile) so hooks in every profile and the serve routes share one store."""
    override = env("PUSH_DATA_DIR")
    if override:
        root = Path(override)
    else:
        from hermes_constants import get_default_hermes_root
        plugin_data = get_default_hermes_root() / "plugin-data"
        root = plugin_data / NAME
        adopt(plugin_data / PREDECESSOR, root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def adopt(old: Path, new: Path) -> bool:
    """First use after the rename: dispatch-push's state (registered devices, the action key, delivery health) is
    copied over, so phones keep their alerts without signing in again and an Approve on an alert the old plugin sent
    still checks. Copied, never moved: the old plugin may still be installed. Only while ``new`` doesn't exist yet;
    the copy lands under a temporary name and is renamed into place, so a second process never sees half of it."""
    if new.exists() or not old.is_dir():
        return False
    staging = new.with_name(f"{new.name}.adopting-{secrets.token_hex(4)}")
    try:
        shutil.copytree(old, staging, ignore=shutil.ignore_patterns("reply-watcher.lock"))
        os.chmod(staging, 0o700)
        os.rename(staging, new)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return False
    log.info("%s adopted %s's devices from %s", NAME, PREDECESSOR, old)
    return True


def quiet_predecessor() -> bool:
    """dispatch-push (0.4 and earlier) still loaded beside this plugin in the same server would alert every phone a
    second time: after adopt() both stores hold the same devices. Its core is found under the module name it registers
    and its sender switched off, so only this plugin delivers (its routes still answer; the app asks this plugin's
    first). Returns whether it is loaded, for /status (`predecessor`), so the app can say so. Removing it (``hermes
    plugins remove dispatch-push``) is what the app's setup prompt asks for; this covers the time until then."""
    old = sys.modules.get(PREDECESSOR_MODULE)
    if old is None or old is sys.modules.get(__name__):
        return False
    if not getattr(old, "_quieted_by_successor", False):
        try:
            with old._senders_lock:
                old._new_sender = lambda: None
                old._senders.clear()
            old._quieted_by_successor = True
            log.warning("%s is still installed beside %s; only %s sends alerts. Remove it: hermes plugins remove %s",
                        PREDECESSOR, NAME, NAME, PREDECESSOR)
        except Exception:
            log.debug("%s couldn't quiet %s", NAME, PREDECESSOR, exc_info=True)
    return True


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

@dataclass
class Device:
    device_id: str
    token: str
    environment: str  # "sandbox" | "production"
    topic: str        # the app's bundle id
    profile: str
    kinds: dict
    previews: bool = True  # the app's Settings › Notifications › Show Previews
    seal: str = ""     # base64 AES-256 key from the app: alerts to this device are sealed with it
    account: str = ""  # the app's opaque account tag, echoed in alerts so it knows which gateway sent them

    def wants(self, kind: str) -> bool:
        return self.kinds.get(kind, True) is not False


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self._db() as db:
            db.execute("""create table if not exists devices (
                device_id text primary key, token text not null, environment text not null,
                topic text not null, profile text not null, kinds text not null, updated_at real not null,
                previews integer not null default 1)""")
            columns = {row[1] for row in db.execute("pragma table_info(devices)")}
            if "previews" not in columns:
                db.execute("alter table devices add column previews integer not null default 1")
            for column in ("seal", "account"):
                if column not in columns:
                    db.execute(f"alter table devices add column {column} text not null default ''")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("pragma journal_mode=wal")
        return db

    def upsert(self, device: Device) -> None:
        with self._db() as db:
            db.execute("insert or replace into devices (device_id, token, environment, topic, profile, kinds, updated_at, previews,"
                       " seal, account) values (?,?,?,?,?,?,?,?,?,?)",
                       (device.device_id, device.token, device.environment, device.topic, device.profile,
                        json.dumps(device.kinds), time.time(), int(device.previews), device.seal, device.account))
            # One row per APNs token: a reinstall mints a new device id for the same token.
            db.execute("delete from devices where token = ? and device_id != ?", (device.token, device.device_id))

    def remove(self, device_id: str) -> bool:
        with self._db() as db:
            return db.execute("delete from devices where device_id = ?", (device_id,)).rowcount > 0

    def remove_token(self, token: str) -> None:
        with self._db() as db:
            db.execute("delete from devices where token = ?", (token,))

    def devices(self) -> list[Device]:
        with self._db() as db:
            rows = db.execute("select device_id, token, environment, topic, profile, kinds, previews, seal, account"
                              " from devices").fetchall()
        return [Device(r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or "{}"), bool(r[6]), r[7] or "", r[8] or "") for r in rows]

    def get(self, device_id: str) -> Device | None:
        return next((d for d in self.devices() if d.device_id == device_id), None)


class RouteError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def check_device_id(device_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", device_id):
        raise RouteError(400, "Invalid device id.")


def registration(device_id: str, token: str, environment: str, topic: str,
                 profile: str = "*", kinds: dict | None = None, previews: bool = True,
                 seal: str | None = None, account: str | None = None, sealed_only: bool = False) -> Device:
    try:
        check_device_id(device_id)
        if (not re.fullmatch(r"[0-9a-f]{64,200}", token.lower()) or topic != TOPIC
                or environment not in ("sandbox", "production")
                or not re.fullmatch(r"\*|[A-Za-z0-9_.-]{1,64}", profile)):
            raise ValueError()
        if seal:
            seal_key(seal)
    except ValueError:
        raise RouteError(400, "Invalid device registration.") from None
    # Relay mode carries only sealed alerts: an app build without the notification extension keeps its own local
    # alerts instead (it reads anything but 200 as "not registered").
    if sealed_only and not seal:
        raise RouteError(409, "Update Hermes HQ to get notifications through the push relay.")
    # The tag is opaque; one that isn't the app's hash is dropped, not refused (alerts then carry none).
    tag = account.lower() if account and re.fullmatch(r"[0-9a-fA-F]{64}", account) else ""
    return Device(device_id, token.lower(), environment, topic, profile,
                  {k: bool(v) for k, v in (kinds or {}).items() if k in KINDS}, previews is not False, seal or "", tag)


def remove_device(root: Path, device_id: str) -> dict:
    check_device_id(device_id)
    return {"ok": True, "removed": Store(root / "devices.db").remove(device_id)}


def status_body(root: Path, sender) -> dict:
    """`seal`: registrations may carry a seal key (the app sends one only when this says so). `replies: "watcher"`
    (0.3): reply alerts cover every bot, scheduled jobs included (ReplyWatcher); the app offers an update without it.
    `health` (0.4): how deliveries went, across every process that sent (Health), for the app's Diagnostics.
    `plugin` (0.5): this plugin's name since the rename, and `predecessor`: dispatch-push is loaded too (and quieted).
    `desktopThemes` (0.6): it puts the Hermes HQ themes in the desktop app (desktop_theme.py); the app offers an update
    to a plugin without it. `restartGuard` (0.7): it blocks `launchctl submit` and removes a restart job that loops
    (restart_guard.py); the app offers an update to a plugin without it. `perfLogs` (0.8): it keeps the app's performance
    log on this computer (perf_logs.py), and `moments` (0.8): a marked moment's content-free bundle too (POST /logs/moment)."""
    store = Store(root / "devices.db")
    return {"ok": True, "version": 6, "plugin": NAME, "pluginVersion": PLUGIN_VERSION,
            "delivery": type(sender).__name__ if sender else None,
            "kinds": list(KINDS), "seal": SEALS, "replies": "watcher", "desktopThemes": True, "restartGuard": True, "perfLogs": True, "moments": True, "devices": len(store.devices()),
            "health": Health(store.path).summary(), "predecessor": quiet_predecessor()}


def answer_approval(root: Path, request_id: str, session_key: str, choice: str,
                    token: str, resolve: Callable) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise RouteError(400, "Invalid request id.")
    if not action_token_valid(root, request_id, session_key, token):
        raise RouteError(403, "This notification can't answer that request.")
    if choice not in CHOICES:
        raise RouteError(422, "Invalid approval choice.")
    return {"ok": True, "resolved": resolve(session_key, choice, request_id=request_id)}


# ---------------------------------------------------------------------------
# Action tokens: a notification's Approve/Deny can only answer the request it was sent for.
# ---------------------------------------------------------------------------

def _secret(root: Path) -> bytes:
    path = root / "action.key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    return path.read_bytes()


def action_token(root: Path, request_id: str, session_key: str) -> str:
    return hmac.new(_secret(root), f"{request_id}\n{session_key}".encode(), hashlib.sha256).hexdigest()


def action_token_valid(root: Path, request_id: str, session_key: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(action_token(root, request_id, session_key), token)


# ---------------------------------------------------------------------------
# Events → pushes
# ---------------------------------------------------------------------------

@dataclass
class Push:
    kind: str
    session: str = ""
    request: str = ""
    session_key: str = ""
    token: str = ""
    profile: str = ""
    settled: bool = False  # the approval was answered elsewhere: replace its alert quietly
    sender: str = ""  # the bot's display name
    text: str = ""    # the reply, or the command awaiting approval
    about: str = ""   # an approval's description
    queued: float = 0.0  # when the Dispatcher took it (its clock): a reply alert too late to matter isn't retried

    def payload(self, previews: bool = True, account: str = "") -> dict:
        dispatch = {k: v for k, v in {
            "kind": self.kind, "session": self.session, "request": self.request,
            "sessionKey": self.session_key, "token": self.token, "profile": self.profile, "account": account,
        }.items() if v}
        if self.settled:
            # Background update: the app removes the delivered approval alert.
            return {"aps": {"content-available": 1}, "dispatch": {**dispatch, "settled": True}}
        alert = alert_copy.alert(self.kind, self.sender or alert_copy.readable_profile(self.profile), self.text, previews, self.about)
        # Time Sensitive needs an entitlement the app doesn't have: every alert is a normal (active) one.
        aps: dict = {"alert": alert, "sound": "default", "interruption-level": "active",
                     "relevance-score": RELEVANCE.get(self.kind, 0.5)}
        if self.session:
            aps["thread-id"] = self.session
        # Approve/Deny only while the alert shows the whole command being approved: one line, not clipped (the app's
        # local rule). A command with more lines, or one cut short, only opens the chat.
        if self.kind == "approval" and previews and alert_copy.approvable(alert_copy.command_summary(self.text)):
            aps["category"] = APPROVAL_CATEGORY
        return {"aps": aps, "dispatch": dispatch}

    @property
    def collapse_id(self) -> str:
        return (self.request or f"{self.kind}:{self.session}")[:64]

    def for_device(self, device: Device) -> tuple[dict, str]:
        """What one device gets: its payload and collapse id. Sealed for a device that gave a key, with a collapse id
        that still groups an alert's updates without naming its chat or request."""
        payload = self.payload(device.previews, device.account)
        if not device.seal:
            return payload, self.collapse_id
        key = seal_key(device.seal)
        collapse = hmac.new(hashlib.sha256(b"dispatch-push collapse\n" + key).digest(), self.collapse_id.encode(),
                            hashlib.sha256).hexdigest()[:32]
        return sealed(payload, key), collapse


# ---------------------------------------------------------------------------
# Sealed alerts: only the phone that made the key can read them.
# ---------------------------------------------------------------------------

def seal_key(value: str) -> bytes:
    """The app's key: standard base64 of 32 random bytes. Anything else is refused."""
    key = base64.b64decode(value.encode("ascii"), validate=True)
    if len(key) != 32:
        raise ValueError("A seal key is 32 bytes.")
    return key


def seal_kid(key: bytes) -> str:
    """Which of the phone's keys opens a box (it keeps one per account): the first 8 bytes of SHA-256, in hex."""
    return hashlib.sha256(key).hexdigest()[:16]


def sealed(payload: dict, key: bytes, nonce: bytes | None = None) -> dict:
    """AES-256-GCM over the whole payload; the box is nonce (12) + ciphertext + tag (16), as CryptoKit's
    AES.GCM.SealedBox(combined:) reads it. The visible alert says only "Hermes HQ: New notification" and asks for the
    extension (mutable-content); a settle stays a silent background push the app opens itself."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = nonce or secrets.token_bytes(12)
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    box = nonce + AESGCM(key).encrypt(nonce, plain, SEAL_AAD)
    inner = payload.get("aps", {})
    if "alert" in inner:
        # No category outside: Approve/Deny come only from the opened alert (the extension sets them), never on a
        # "New notification" the phone couldn't open (before first unlock, no key, out of time), whose command nobody saw.
        aps = {"alert": dict(SEALED_ALERT), "mutable-content": 1, "sound": "default",
               "interruption-level": inner.get("interruption-level", "active"),
               "relevance-score": inner.get("relevance-score", 0.5)}
    else:
        aps = {"content-available": 1}
    return {"aps": aps, "seal": {"v": 1, "kid": seal_kid(key), "box": base64.b64encode(box).decode("ascii")}}


def unseal(outer: dict, key: bytes) -> dict:
    """The phone's side, for tests and the shared vectors."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    box = base64.b64decode(outer["seal"]["box"])
    return json.loads(AESGCM(key).decrypt(box[:12], box[12:], SEAL_AAD))


def turn_push(kwargs: dict, profile: str, sender: str = "", text: str = "") -> Push | None:
    """``on_session_end`` fires once per turn; a teardown repeat carries interrupted=True. ``text`` is the
    reply ``post_llm_call`` saw for the turn. (Reply alerts now come from ReplyWatcher; kept for its tests' rules.)"""
    if kwargs.get("platform") not in APP_PLATFORMS:
        return None
    if not kwargs.get("completed") or kwargs.get("interrupted") or not kwargs.get("session_id"):
        return None
    return Push("turnDone", session=str(kwargs["session_id"]), profile=profile, sender=sender, text=str(text or "")[:4000])


def approval_push(kwargs: dict, pending: Iterable[dict], root: Path, profile: str, sender: str = "") -> Push | None:
    """``pre_approval_request`` has no request id; the queued entry does (queued before the hook fires)."""
    if kwargs.get("surface") != "gateway" or kwargs.get("coalesced"):
        return None
    session_key = str(kwargs.get("session_key") or "")
    if not session_key:
        return None
    keys = list(kwargs.get("pattern_keys") or [])
    match = [e for e in pending if e.get("command") == kwargs.get("command")
             and list(e.get("pattern_keys") or []) == keys and e.get("request_id")]
    if not match:
        return None
    request_id = str(match[-1]["request_id"])
    return Push("approval", session=str(kwargs.get("session_id") or ""), request=request_id,
                session_key=session_key, token=action_token(root, request_id, session_key), profile=profile,
                sender=sender, text=str(kwargs.get("command") or ""), about=str(kwargs.get("description") or ""))


def approval_signature(kwargs: dict) -> tuple:
    return (str(kwargs.get("session_key") or ""), kwargs.get("command"), tuple(kwargs.get("pattern_keys") or ()))


def remember_approval(sent: dict, kwargs: dict, push: Push, limit: int = 256) -> None:
    """Keyed by request id, so two identical requests pending in one chat each keep their own alert."""
    sent[push.request] = (approval_signature(kwargs), push)
    while len(sent) > limit:
        sent.pop(next(iter(sent)))


def settled_push(kwargs: dict, sent: dict[str, tuple[tuple, Push]]) -> Push | None:
    """``post_approval_response`` (same payload as the request, plus the choice; no request id):
    withdraw the alert we sent for it — answered in the app, on another device, or timed out. Identical requests
    in one chat are answered in the order they were asked, so the oldest alert with this signature goes."""
    signature = approval_signature(kwargs)
    request = next((request for request, (sent_for, _) in sent.items() if sent_for == signature), None)
    if request is None:
        return None
    push = sent.pop(request)[1]
    return Push("approval", session=push.session, request=push.request, session_key=push.session_key,
                profile=push.profile, settled=True)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class Gone(Exception):
    """APNs says the token is dead (uninstalled app or rotated token)."""


class DeliveryError(Exception):
    """A push that didn't go. ``code`` names it in /status (an HTTP status, ``timeout`` or ``unreachable``). A
    ``transient`` one (Apple or the relay busy or briefly out of reach: 429, 5xx, a timeout) is tried again; anything
    else Apple or the relay refused (4xx) would only be refused again, so it never is."""

    def __init__(self, code: str, detail: str, transient: bool = False, retry_after: float | None = None):
        super().__init__(detail)
        self.code, self.transient, self.retry_after = code, transient, retry_after


def retry_after(value: str | None) -> float | None:
    """A Retry-After in seconds (the HTTP-date form isn't used by Apple or the relay)."""
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


# Apple asks for a provider token to be refreshed no more than once every 20 minutes and refuses one over an hour
# old. Each token is issued at the start of a 40-minute window, so every sender (this process's, another process's)
# signs with the same issue time and a new one is made at most once a window.
TOKEN_WINDOW_S = 40 * 60


def token_window(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(now) // TOKEN_WINDOW_S * TOKEN_WINDOW_S


class DirectAPNs:
    """Token-based APNs with the app developer's own key. HTTP/2 through the system curl
    (the Hermes runtime has no h2 package). The JWT is reused for its 40-minute window, as Apple requires."""

    def __init__(self, key_path: str, key_id: str, team_id: str, runner: Callable = subprocess.run):
        self.key_path, self.key_id, self.team_id, self.run = key_path, key_id, team_id, runner
        self._jwt: tuple[int, str] | None = None
        self._expired = False  # Apple called the window's token expired (a clock off by more than 20 minutes)
        self.signed = 0

    def _bearer(self) -> str:
        window = token_window()
        if self._jwt and self._jwt[0] == window:
            return self._jwt[1]
        import jwt
        key = Path(os.path.expanduser(self.key_path)).read_text()
        issued = int(time.time()) if self._expired else window
        token = jwt.encode({"iss": self.team_id, "iat": issued}, key, algorithm="ES256", headers={"kid": self.key_id})
        self._jwt, self._expired = (window, token), False
        self.signed += 1
        return token

    def send(self, device: Device, push: Push) -> None:
        host = "api.push.apple.com" if device.environment == "production" else "api.sandbox.push.apple.com"
        payload, collapse = push.for_device(device)
        headers = [f"authorization: bearer {self._bearer()}", f"apns-topic: {device.topic}",
                   f"apns-push-type: {'background' if push.settled else 'alert'}",
                   f"apns-priority: {5 if push.settled else 10}", f"apns-collapse-id: {collapse}"]
        # Headers go through a 0600 file so the bearer token never appears in the process list.
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("\n".join(headers))
            header_file = handle.name
        try:
            result = self.run(["curl", "-sS", "--http2", "--max-time", "10", "-X", "POST",
                               "-H", f"@{header_file}", "--data-binary", "@-", "-w", "\n%{http_code}",
                               f"https://{host}/3/device/{device.token}"],
                              input=json.dumps(payload), capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise DeliveryError("timeout", "APNs didn't answer in time", transient=True) from None
        finally:
            os.unlink(header_file)
        body, _, status = (result.stdout or "").rpartition("\n")
        if status == "200":
            return
        reason = ""
        try:
            reason = json.loads(body or "{}").get("reason", "")
        except ValueError:
            pass
        if status == "410" or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
            raise Gone(reason or status)
        if status == "403" and reason == "ExpiredProviderToken":
            self._jwt, self._expired = None, True  # the retry signs a new one, issued now
            raise DeliveryError("403", "APNs 403 ExpiredProviderToken", transient=True)
        code = status if status and status != "000" else "unreachable"
        transient = code in ("unreachable", "429") or code.startswith("5")
        raise DeliveryError(code, f"APNs {code} {reason or (result.stderr or '').strip()[:120]}".strip(), transient)


PLUGIN_VERSION = "0.8.0"


class Relay:
    """A relay holds the APNs key so a gateway never needs it: how every Hermes user but the app's developer gets
    push. It carries only sealed alerts, so it learns a device token, when, and whether an alert could be approved,
    never what an alert says. ``key`` is optional: the public relay needs none."""

    def __init__(self, url: str, key: str = ""):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError("Relay URL must use HTTPS with a host and no user info.")
        self.url, self.key = url.rstrip("/"), key

    def body(self, device: Device, push: Push) -> dict:
        payload, collapse = push.for_device(device)
        return {"token": device.token, "environment": device.environment, "topic": device.topic,
                "pushType": "background" if push.settled else "alert", "collapseId": collapse, "payload": payload}

    def send(self, device: Device, push: Push) -> None:
        import httpx
        headers = {"user-agent": f"{NAME}/{PLUGIN_VERSION}"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        try:
            response = httpx.post(f"{self.url}/v1/push", timeout=10, headers=headers, json=self.body(device, push))
        except httpx.TimeoutException:
            raise DeliveryError("timeout", "The push relay didn't answer in time", transient=True) from None
        except httpx.TransportError as error:
            raise DeliveryError("unreachable", f"The push relay is unreachable ({type(error).__name__})", transient=True) from None
        status = response.status_code
        if status == 410:
            raise Gone("relay")
        if 200 <= status < 300:
            return
        try:
            said = response.json()
            said = " ".join(str(said.get(k) or "") for k in ("error", "reason")).strip()
        except Exception:
            said = ""
        raise DeliveryError(str(status), f"Relay {status} {said}".strip(), status == 429 or status >= 500,
                            retry_after(response.headers.get("retry-after")))


# Read through env(): HERMES_HQ_<name>, else DISPATCH_<name>.
SENDER_ENV = ("PUSH_RELAY_URL", "PUSH_RELAY_KEY", "APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID")
_senders: dict = {}
_senders_lock = threading.Lock()


def sender_from_env() -> DirectAPNs | Relay | None:
    """The app's developer sends straight to Apple with the app's key; everyone else goes through the public relay.
    HERMES_HQ_PUSH_RELAY_URL (or DISPATCH_PUSH_RELAY_URL) picks another relay, or `off` turns delivery off; one that isn't a valid HTTPS URL
    also turns it off (never a silent fallback to another sender). One sender per configuration for the process,
    so direct mode's provider token is reused for its whole window instead of signed again for every push."""
    config = (SEALS, *(env(name) for name in SENDER_ENV))
    with _senders_lock:
        if config in _senders:
            return _senders[config]
        sender = _new_sender()
        if sender is not None:
            _senders.clear()
            _senders[config] = sender
        return sender


def _new_sender() -> DirectAPNs | Relay | None:
    url = env("PUSH_RELAY_URL")
    if url is not None and url.strip().lower() in ("off", "none", "0", "false"):
        return None
    if url is None:
        path, key_id, team = (env(n) for n in ("APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID"))
        if path and key_id and team:
            return DirectAPNs(path, key_id, team)
    if not SEALS:
        log.warning(f"{NAME} relay mode needs the cryptography package to seal alerts")
        return None
    try:
        return Relay(DEFAULT_RELAY_URL if url is None else url, env("PUSH_RELAY_KEY") or "")
    except ValueError:
        log.warning(f"{NAME} relay URL must use HTTPS with a host and no user info")
        return None


class Health:
    """How deliveries went, for /status and the app's Diagnostics: counts by outcome, when the last push went and the
    last thing that went wrong. Kept beside the devices (devices.db), so the server shows what every process that
    delivers saw (its reply watcher, approval hooks). Names: ``sent``, ``gone`` (a dead token removed),
    ``retried:<code>``, ``failed:<code>`` (given up), ``dropped:<why>``, and ``replies:<finish_reason>`` (the
    watcher's tally of finished assistant rows in app chats, to show a bot whose replies never end in `stop`)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("create table if not exists delivery (name text primary key, count integer not null default 0,"
                           " at real, detail text not null default '')")
        except sqlite3.Error:
            log.debug(f"{NAME} health table unavailable", exc_info=True)

    def record(self, name: str, detail: str = "", count: int = 1) -> None:
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("insert into delivery (name, count, at, detail) values (?, ?, ?, ?) on conflict(name) do update"
                           " set count = count + excluded.count, at = excluded.at, detail = excluded.detail",
                           (name, count, time.time(), detail[:300]))
        except sqlite3.Error:
            log.debug(f"{NAME} couldn't record %s", name, exc_info=True)

    def summary(self) -> dict:
        out = {"sent": 0, "retried": 0, "failed": 0, "dropped": 0, "gone": 0, "byStatus": {}, "replyRows": {},
               "lastSuccessAt": None, "lastFailureAt": None, "lastProblemAt": None, "lastProblem": ""}
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                rows = db.execute("select name, count, at, detail from delivery").fetchall()
        except sqlite3.Error:
            return out
        for name, count, at, detail in rows:
            kind, _, code = name.partition(":")
            if kind == "replies":
                out["replyRows"][code] = count
                continue
            if kind not in out:
                continue
            out[kind] += count
            if kind == "sent":
                out["lastSuccessAt"] = at
            if kind == "failed":
                out["byStatus"][code] = out["byStatus"].get(code, 0) + count
                out["lastFailureAt"] = max(out["lastFailureAt"] or 0, at)
            if kind in ("failed", "retried", "dropped") and at > (out["lastProblemAt"] or 0):
                out["lastProblemAt"], out["lastProblem"] = at, detail
        return out


def profile_sender(profile: str) -> str:
    """The bot's name as the Bots roster shows it: its Bot Mode title, the profile's display name, or its id
    in words. Read-only profile.yaml metadata through Hermes' own helper."""
    try:
        from hermes_cli.profiles import get_profile_dir, read_profile_meta
        meta = read_profile_meta(get_profile_dir(profile))
        return meta.get("bot_title") or meta.get("display_name") or alert_copy.readable_profile(profile)
    except Exception:
        return alert_copy.readable_profile(profile)


# A finished reply: the turn's last assistant message, in a chat people have with a bot in an app (Hermes HQ, the
# desktop app, the TUI), never a subagent's working session (Hermes marks those `_delegate_from`), a cron job's own
# run, a tool session or a messaging platform's chat.
_APP_REPLY = """m.role = 'assistant' AND COALESCE(m.content, '') != ''
    AND s.source IN ('desktop', 'tui') AND json_extract(s.model_config, '$._delegate_from') IS NULL"""
_REPLIES_SQL = f"""SELECT m.id, m.session_id, m.content FROM messages m JOIN sessions s ON s.id = m.session_id
  WHERE m.id > ? AND m.id <= ? AND m.finish_reason = 'stop' AND {_APP_REPLY}
  ORDER BY m.id"""
# The same rows by how they ended (Health's replyRows), and the ones not ended yet (rechecked: see ReplyWatcher).
_ENDINGS_SQL = f"""SELECT COALESCE(m.finish_reason, 'none'), COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id
  WHERE m.id > ? AND m.id <= ? AND {_APP_REPLY} GROUP BY 1"""
_OPEN_SQL = f"""SELECT m.id FROM messages m JOIN sessions s ON s.id = m.session_id
  WHERE m.id > ? AND m.id <= ? AND m.finish_reason IS NULL AND {_APP_REPLY}"""


class ReplyWatcher:
    """Reply alerts for every bot from one place. A hook runs only in processes whose profile loaded this plugin, and
    Hermes loads plugins per profile, so a bot's scheduled job (a `hermes chat -Q` child in its own profile posting
    to its Bot Chat) or any turn outside `hermes serve` would never alert. Every reply ends up in its profile's
    state.db, so the one process that has the plugin (the server Hermes HQ talks to) reads them all, read-only.
    Starts at each database's newest message: nothing from before it started alerts.

    A reply row written before its finish_reason (set to `stop` afterwards) would be passed over for good once the
    watcher moved past its id, so rows with no finish_reason yet are rechecked for ten minutes. How rows end, by
    finish_reason, is tallied in Health: a bot whose replies never end in `stop` shows there instead of just never
    alerting."""

    RECHECK_S = 600.0
    RECHECK_MAX = 256

    def __init__(self, root: Path, submit: Callable[[Push], None], sender: Callable[[str], str] = profile_sender,
                 health: Health | None = None, clock: Callable[[], float] = time.monotonic):
        self.root, self.submit, self.sender, self.health, self.clock = Path(root), submit, sender, health, clock
        self.seen: dict[Path, int] = {}
        self.open: dict[Path, dict[int, float]] = {}

    def databases(self) -> list[tuple[str, Path]]:
        found = [("default", self.root / "state.db")]
        profiles = self.root / "profiles"
        if profiles.is_dir():
            found += [(home.name, home / "state.db") for home in sorted(profiles.iterdir()) if home.is_dir()]
        return [(name, path) for name, path in found if path.is_file()]

    def poll(self) -> int:
        """One pass over every profile; returns how many replies it alerted."""
        sent = 0
        now = self.clock()
        for profile, path in self.databases():
            pending = self.open.setdefault(path, {})
            try:
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as db:
                    newest = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                    if path not in self.seen:
                        self.seen[path] = newest
                        continue
                    since = self.seen[path]
                    rows, endings, unended = [], [], []
                    if newest > since:
                        rows = db.execute(_REPLIES_SQL, (since, newest)).fetchall()
                        endings = db.execute(_ENDINGS_SQL, (since, newest)).fetchall()
                        unended = [row[0] for row in db.execute(_OPEN_SQL, (since, newest))]
                    if pending:
                        marks = ",".join("?" * len(pending))
                        rows += db.execute(f"SELECT id, session_id, content FROM messages WHERE id IN ({marks})"
                                           " AND finish_reason = 'stop' ORDER BY id", tuple(pending)).fetchall()
                    self.seen[path] = max(since, newest)
            except sqlite3.Error:
                continue  # busy, or not a Hermes database yet: next pass
            for row_id, _, _ in rows:
                pending.pop(row_id, None)
            for row_id in unended:
                pending[row_id] = now
            for row_id, at in list(pending.items()):
                if now - at > self.RECHECK_S:
                    del pending[row_id]
            while len(pending) > self.RECHECK_MAX:
                del pending[next(iter(pending))]
            if self.health:
                for ending, count in endings:
                    self.health.record(f"replies:{ending}", count=count)
            last: dict[str, str] = {}
            for _, session, content in sorted(rows):
                last[session] = content  # a burst of turns in one chat: its last reply
            for session, content in last.items():
                self.submit(Push("turnDone", session=session, profile=profile, sender=self.sender(profile), text=str(content)[:4000]))
                sent += 1
        return sent

    def run(self, interval: float = 4.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                quiet_predecessor()  # it may load after this plugin did
                self.poll()
            except Exception:
                log.debug(f"{NAME} reply watcher pass failed", exc_info=True)
            stop.wait(interval)


_watcher_started = False


def start_reply_watcher(root: Path | None = None) -> bool:
    """In the server Hermes HQ talks to (dashboard/plugin_api.py). One watcher per machine: a lock file, released
    when its process exits, keeps a second server from alerting twice."""
    global _watcher_started
    if _watcher_started:
        return False
    try:
        if root is None:
            from hermes_constants import get_default_hermes_root  # outside Hermes (the plugin's tests): no watcher
            root = get_default_hermes_root()
        import fcntl
        lock = open(data_dir() / "reply-watcher.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        return False
    _watcher_started = True
    dispatcher = Dispatcher(Store(data_dir() / "devices.db"))
    watcher = ReplyWatcher(root, dispatcher.submit, health=dispatcher.health)
    thread = threading.Thread(target=watcher.run, name=f"{NAME}-replies", daemon=True)
    thread._push_lock = lock  # held for the process's life
    thread.start()
    return True


# A push Apple or the relay couldn't take for a moment is tried again after these waits (or the Retry-After it came
# with, up to a minute), then given up. A reply alert that would land over two minutes late is dropped instead.
RETRY_DELAYS = (2.0, 10.0, 30.0)
RETRY_AFTER_MAX = 60.0
REPLY_MAX_AGE = 120.0


def priority(push: Push) -> int:
    """Approvals (and their withdrawals) before replies: an agent is waiting on them."""
    return 0 if push.kind == "approval" else 1


class Dispatcher:
    """Hooks only enqueue (the approval hook blocks the agent thread); one daemon thread delivers, approvals first,
    for the life of the process (a thread that quit when idle could miss a push queued as it quit). Transient failures
    are retried (RETRY_DELAYS) without holding up other pushes; every outcome is counted in Health."""

    def __init__(self, store: Store, sender_factory: Callable = sender_from_env, debounce_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        self.store, self.sender_factory, self.debounce_s, self.clock = store, sender_factory, debounce_s, clock
        self.health = Health(store.path)
        self.queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=256)
        self.retries: list = []  # heap of (due, priority, seq, attempt, device_id, push); the delivery thread's alone
        self.last_turn: dict[str, float] = {}
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seq = itertools.count()

    def submit(self, push: Push | None) -> None:
        if push is None:
            return
        now = self.clock()
        if push.kind == "turnDone":
            # Goal and auto-continue loops end several turns back to back: one alert per burst.
            with self._lock:
                if now - self.last_turn.get(push.session, -1e9) < self.debounce_s:
                    return
                self.last_turn[push.session] = now
                if len(self.last_turn) > 64:  # only sessions still inside their burst window matter
                    self.last_turn = {s: at for s, at in self.last_turn.items() if now - at < self.debounce_s}
        push.queued = push.queued or now
        try:
            self.queue.put_nowait((priority(push), next(self._seq), push))
        except queue.Full:
            log.warning(f"{NAME} queue full; dropping %s", push.kind)
            self.health.record("dropped:queue", f"Queue full; a {push.kind} alert was dropped")
            return
        with self._lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name=NAME, daemon=True)
                self.thread.start()

    def deliver(self, push: Push, attempt: int = 0, only: str = "") -> int:
        quiet_predecessor()  # one alert per event, even with dispatch-push still loaded beside this plugin
        sender = self.sender_factory()
        if sender is None:
            return 0
        push.queued = push.queued or self.clock()
        if push.settled and self.retries:
            # Answered: a retry of its alert would only put back what this withdraws.
            kept = [item for item in self.retries if item[5].request != push.request or item[5].settled]
            if len(kept) != len(self.retries):
                self.health.record("dropped:settled", "An approval was answered before its alert could be sent", len(self.retries) - len(kept))
                self.retries = kept
                heapq.heapify(self.retries)
        sent = 0
        for device in self.store.devices():
            if (device.topic != TOPIC or device.profile not in ("*", push.profile)
                    or not device.wants(push.kind) or (only and device.device_id != only)):
                continue
            # Registered before relay mode, without a key: the relay takes only sealed alerts.
            if isinstance(sender, Relay) and not device.seal:
                continue
            sent += self._send(sender, device, push, attempt)
        return sent

    def _send(self, sender, device: Device, push: Push, attempt: int) -> int:
        try:
            sender.send(device, push)
        except Gone:
            self.store.remove_token(device.token)
            self.health.record("gone", "A device's push token was retired")
            return 0
        except DeliveryError as error:
            if error.transient and attempt < len(RETRY_DELAYS):
                wait = RETRY_DELAYS[attempt]
                if error.retry_after is not None:
                    wait = min(max(wait, error.retry_after), RETRY_AFTER_MAX)
                heapq.heappush(self.retries, (self.clock() + wait, priority(push), next(self._seq), attempt + 1, device.device_id, push))
                self.health.record(f"retried:{error.code}", str(error))
                log.info(f"{NAME} delivery failed (%s); retrying in %.0f s", error, wait)
            else:
                self.health.record(f"failed:{error.code}", str(error))
                log.warning(f"{NAME} delivery failed: %s", error)
            return 0
        except Exception as error:  # delivery is best effort; never disturb the agent
            self.health.record("failed:error", f"{type(error).__name__}: {error}")
            log.warning(f"{NAME} delivery failed: %s", error)
            return 0
        self.health.record("sent")
        return 1

    def retry_due(self) -> int:
        """Sends the retries whose time has come; returns how many went."""
        sent, now = 0, self.clock()
        while self.retries and self.retries[0][0] <= now:
            _, _, _, attempt, device_id, push = heapq.heappop(self.retries)
            if push.kind == "turnDone" and now - push.queued > REPLY_MAX_AGE:
                self.health.record("dropped:stale", "A reply alert was dropped: too late to be useful")
                continue
            sent += self.deliver(push, attempt, device_id)
        return sent

    def _run(self) -> None:
        while True:
            try:
                wait = max(0.0, self.retries[0][0] - self.clock()) if self.retries else None
                try:
                    push = self.queue.get(timeout=wait)[2]
                except queue.Empty:
                    push = None
                if push is not None:
                    self.deliver(push)
                self.retry_due()
            except Exception:
                log.warning(f"{NAME} delivery pass failed", exc_info=True)
