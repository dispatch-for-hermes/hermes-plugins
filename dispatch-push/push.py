"""Dispatch push core: device store, payloads, action tokens and APNs delivery.

No Hermes imports at module level, so the hooks (``__init__.py``), the dashboard routes
(``dashboard/plugin_api.py``, a separate module instance in ``hermes serve``) and the tests all
load it the same way. Alerts read like the app's own (alert_copy.py, docs/notifications.md): who (the bot)
and what (a masked, clipped preview of the reply or the command awaiting approval), or only who when the
device turned Show Previews off. Action tokens, keys and secrets never ride a payload.

A device that gives a seal key (every Dispatch build with the notification extension) gets its alerts sealed:
AES-256-GCM with that key, so Apple, and the relay in relay mode, see only "Dispatch: New notification" and the
phone's extension opens the real alert (docs/push-notifications.md, "Sealed alerts").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

log = logging.getLogger("dispatch-push")

_copy_spec = importlib.util.spec_from_file_location("dispatch_push_copy", Path(__file__).with_name("alert_copy.py"))
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
SEALED_ALERT = {"title": "Dispatch", "body": "New notification"}
SEAL_AAD = b"dispatch-push seal v1"
SEALS = importlib.util.find_spec("cryptography") is not None
# The public relay that holds the app's APNs key for every gateway that doesn't (a Cloudflare Worker, push-relay/).
DEFAULT_RELAY_URL = "https://push.getdispatchapp.com"


def data_dir() -> Path:
    """Root-level (not per-profile) so hooks in every profile and the serve routes share one store."""
    override = os.getenv("DISPATCH_PUSH_DATA_DIR")
    if override:
        root = Path(override)
    else:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root() / "plugin-data" / "dispatch-push"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


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
    # Relay mode carries only sealed alerts: a Dispatch without the notification extension keeps its own local
    # alerts instead (it reads anything but 200 as "not registered").
    if sealed_only and not seal:
        raise RouteError(409, "Update Dispatch to get notifications through the push relay.")
    # The tag is opaque; one that isn't the app's hash is dropped, not refused (alerts then carry none).
    tag = account.lower() if account and re.fullmatch(r"[0-9a-fA-F]{64}", account) else ""
    return Device(device_id, token.lower(), environment, topic, profile,
                  {k: bool(v) for k, v in (kinds or {}).items() if k in KINDS}, previews is not False, seal or "", tag)


def remove_device(root: Path, device_id: str) -> dict:
    check_device_id(device_id)
    return {"ok": True, "removed": Store(root / "devices.db").remove(device_id)}


def status_body(root: Path, sender) -> dict:
    """`seal`: registrations may carry a seal key (the app sends one only when this says so)."""
    return {"ok": True, "version": 2, "delivery": type(sender).__name__ if sender else None,
            "kinds": list(KINDS), "seal": SEALS, "devices": len(Store(root / "devices.db").devices())}


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
    AES.GCM.SealedBox(combined:) reads it. The visible alert says only "Dispatch: New notification" and asks for the
    extension (mutable-content); a settle stays a silent background push the app opens itself."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = nonce or secrets.token_bytes(12)
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    box = nonce + AESGCM(key).encrypt(nonce, plain, SEAL_AAD)
    inner = payload.get("aps", {})
    if "alert" in inner:
        aps = {"alert": dict(SEALED_ALERT), "mutable-content": 1, "sound": "default",
               "interruption-level": inner.get("interruption-level", "active"),
               "relevance-score": inner.get("relevance-score", 0.5)}
        # Kept outside so Approve/Deny show even if the extension doesn't run; it says only that one could be asked.
        if "category" in inner:
            aps["category"] = inner["category"]
    else:
        aps = {"content-available": 1}
    return {"aps": aps, "seal": {"v": 1, "kid": seal_kid(key), "box": base64.b64encode(box).decode("ascii")}}


def unseal(outer: dict, key: bytes) -> dict:
    """The phone's side, for tests and the shared vectors."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    box = base64.b64decode(outer["seal"]["box"])
    return json.loads(AESGCM(key).decrypt(box[:12], box[12:], SEAL_AAD))


# A scheduled job that delivers to a bot's chat (cron `deliver: bot-chat`) posts its result there as one turn of a
# `hermes chat -Q` child, a CLI turn this plugin would otherwise skip. Hermes names that child's query file
# hermes-cron-botchat-* and hands it the turn report path beside it in this variable.
BOT_CHAT_REPORT_ENV = "HERMES_QUIET_TURN_REPORT_FILE"


def bot_chat_delivery(environ=None) -> bool:
    """Is this process a scheduled job's delivery into a bot's chat? Its turns alert like the app's."""
    return Path((os.environ if environ is None else environ).get(BOT_CHAT_REPORT_ENV, "")).name.startswith("hermes-cron-botchat-")


def turn_push(kwargs: dict, profile: str, sender: str = "", text: str = "", delivery: bool = False) -> Push | None:
    """``on_session_end`` fires once per turn; a teardown repeat carries interrupted=True. ``text`` is the
    reply ``post_llm_call`` saw for the turn. `delivery`: a scheduled job's turn in a bot's chat, whatever its platform."""
    if kwargs.get("platform") not in APP_PLATFORMS and not delivery:
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


def settled_push(kwargs: dict, sent: dict[tuple, Push]) -> Push | None:
    """``post_approval_response`` (same payload as the request, plus the choice; no request id):
    withdraw the alert we sent for it — answered in the app, on another device, or timed out."""
    push = sent.pop(approval_signature(kwargs), None)
    if push is None:
        return None
    return Push("approval", session=push.session, request=push.request, session_key=push.session_key,
                profile=push.profile, settled=True)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class Gone(Exception):
    """APNs says the token is dead (uninstalled app or rotated token)."""


class DirectAPNs:
    """Token-based APNs with the app developer's own key. HTTP/2 through the system curl
    (the Hermes runtime has no h2 package). The JWT is reused for 40 minutes, as Apple requires."""

    def __init__(self, key_path: str, key_id: str, team_id: str, runner: Callable = subprocess.run):
        self.key_path, self.key_id, self.team_id, self.run = key_path, key_id, team_id, runner
        self._jwt: tuple[float, str] | None = None

    def _bearer(self) -> str:
        if self._jwt and time.time() - self._jwt[0] < 40 * 60:
            return self._jwt[1]
        import jwt
        key = Path(os.path.expanduser(self.key_path)).read_text()
        token = jwt.encode({"iss": self.team_id, "iat": int(time.time())}, key, algorithm="ES256",
                           headers={"kid": self.key_id})
        self._jwt = (time.time(), token)
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
        raise RuntimeError(f"APNs {status or 'unreachable'} {reason or (result.stderr or '').strip()[:120]}")


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
        headers = {"user-agent": "dispatch-push/0.2"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        response = httpx.post(f"{self.url}/v1/push", timeout=10, headers=headers, json=self.body(device, push))
        if response.status_code == 410:
            raise Gone("relay")
        response.raise_for_status()


def sender_from_env() -> DirectAPNs | Relay | None:
    """The app's developer sends straight to Apple with the app's key; everyone else goes through the public relay.
    DISPATCH_PUSH_RELAY_URL picks another relay, or `off` turns delivery off; one that isn't a valid HTTPS URL
    also turns it off (never a silent fallback to another sender)."""
    url = os.getenv("DISPATCH_PUSH_RELAY_URL")
    if url is not None and url.strip().lower() in ("off", "none", "0", "false"):
        return None
    if url is None:
        path, key_id, team = (os.getenv(n) for n in ("DISPATCH_APNS_KEY_PATH", "DISPATCH_APNS_KEY_ID", "DISPATCH_APNS_TEAM_ID"))
        if path and key_id and team:
            return DirectAPNs(path, key_id, team)
    if not SEALS:
        log.warning("dispatch-push relay mode needs the cryptography package to seal alerts")
        return None
    try:
        return Relay(DEFAULT_RELAY_URL if url is None else url, os.getenv("DISPATCH_PUSH_RELAY_KEY", ""))
    except ValueError:
        log.warning("dispatch-push relay URL must use HTTPS with a host and no user info")
        return None


class Dispatcher:
    """Hooks only enqueue (the approval hook blocks the agent thread); one daemon thread delivers."""

    def __init__(self, store: Store, sender_factory: Callable = sender_from_env, debounce_s: float = 3.0):
        self.store, self.sender_factory, self.debounce_s = store, sender_factory, debounce_s
        self.queue: queue.Queue[Push] = queue.Queue(maxsize=256)
        self.last_turn: dict[str, float] = {}
        self.thread: threading.Thread | None = None

    def submit(self, push: Push | None) -> None:
        if push is None:
            return
        if push.kind == "turnDone":
            # Goal and auto-continue loops end several turns back to back: one alert per burst.
            now = time.monotonic()
            if now - self.last_turn.get(push.session, -1e9) < self.debounce_s:
                return
            self.last_turn[push.session] = now
        try:
            self.queue.put_nowait(push)
        except queue.Full:
            log.warning("dispatch-push queue full; dropping %s", push.kind)
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="dispatch-push", daemon=True)
            self.thread.start()

    def deliver(self, push: Push) -> int:
        sender = self.sender_factory()
        if sender is None:
            return 0
        sent = 0
        for device in self.store.devices():
            if (device.topic != TOPIC or device.profile not in ("*", push.profile)
                    or not device.wants(push.kind)):
                continue
            # Registered before relay mode, without a key: the relay takes only sealed alerts.
            if isinstance(sender, Relay) and not device.seal:
                continue
            try:
                sender.send(device, push)
                sent += 1
            except Gone:
                self.store.remove_token(device.token)
            except Exception as error:  # delivery is best effort; never disturb the agent
                log.warning("dispatch-push delivery failed: %s", error)
        return sent

    def _run(self) -> None:
        while True:
            try:
                push = self.queue.get(timeout=60)
            except queue.Empty:
                return
            self.deliver(push)
