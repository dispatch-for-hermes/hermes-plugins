"""Routes under /api/plugins/hermes-hq-push, behind the dashboard's normal login (the Hermes HQ app's
session cookie). Mounted in ``hermes serve``, the process that owns app sessions' approval queues."""
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

if "hermes_hq_push_core" in sys.modules:
    push = sys.modules["hermes_hq_push_core"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).parents[1] / "push.py")
    push = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_push_core"] = push
    _spec.loader.exec_module(push)
if "hermes_hq_perf_logs" in sys.modules:
    perf_logs = sys.modules["hermes_hq_perf_logs"]
else:
    _logs_spec = importlib.util.spec_from_file_location("hermes_hq_perf_logs", Path(__file__).parents[1] / "perf_logs.py")
    perf_logs = importlib.util.module_from_spec(_logs_spec)
    sys.modules["hermes_hq_perf_logs"] = perf_logs
    _logs_spec.loader.exec_module(perf_logs)

router = APIRouter()
# Reply alerts for every bot, from this server (push.ReplyWatcher).
push.start_reply_watcher()


class DeviceIn(BaseModel):
    token: str = Field(max_length=200)
    environment: str = Field(pattern="^(sandbox|production)$")
    topic: str = Field(max_length=155)
    profile: str = Field(default="*", max_length=64)
    kinds: dict[str, bool] = Field(default_factory=dict)
    previews: bool = True  # Settings › Notifications › Show Previews in the app
    seal: str = Field(default="", max_length=64)  # base64 AES-256 key: alerts to this device are sealed with it
    account: str = Field(default="", max_length=64)  # the app's opaque account tag, echoed in alerts


class LogsIn(BaseModel):
    device: str = Field(max_length=40)
    lines: list[str] = Field(default_factory=list, max_length=perf_logs.MAX_LINES)
    app: str = Field(default="", max_length=40)
    reason: str = Field(default="", max_length=40)
    lost: bool = False


class ApprovalIn(BaseModel):
    session_key: str = Field(max_length=512)
    choice: str = Field(pattern="^(once|deny)$")
    token: str = Field(default="", max_length=128)


@router.get("/status")
def status():
    """The app probes this to offer push; a 404 means the plugin isn't installed or enabled."""
    return push.status_body(push.data_dir(), push.sender_from_env())


@router.put("/devices/{device_id}")
def register_device(device_id: str, body: DeviceIn):
    try:
        device = push.registration(device_id, body.token, body.environment, body.topic, body.profile, body.kinds,
                                   body.previews, body.seal, body.account,
                                   sealed_only=isinstance(push.sender_from_env(), push.Relay))
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None
    push.Store(push.data_dir() / "devices.db").upsert(device)
    return {"ok": True}


@router.delete("/devices/{device_id}")
def remove_device(device_id: str):
    try:
        return push.remove_device(push.data_dir(), device_id)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/approvals/{request_id}")
def answer_approval(request_id: str, body: ApprovalIn):
    """Approve once or deny from a notification. The action token binds the answer to the request the
    notification was sent for, and is checked before Hermes' resolver is imported; 0 resolved means it
    was already answered or timed out."""
    def resolve(session_key, choice, request_id):
        from tools.approval import resolve_gateway_approval
        return resolve_gateway_approval(session_key, choice, request_id=request_id)

    try:
        return push.answer_approval(push.data_dir(), request_id, body.session_key, body.choice, body.token, resolve)
    except push.RouteError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("/logs")
def upload_logs(body: LogsIn):
    """0.7: the app's performance log (perf_logs.py), written to this computer's hq-logs folder. Behind the same
    dashboard sign-in as every route here: Hermes refuses an unauthenticated /api/plugins/... request before it
    reaches this router, and the Hermes HQ edge only forwards it with an owner's bearer."""
    try:
        return perf_logs.write(perf_logs.log_dir(), body.device, body.lines, app=body.app, reason=body.reason, lost=body.lost)
    except perf_logs.LogError as error:
        raise HTTPException(error.status_code, error.detail) from None


class MomentIn(BaseModel):
    device: str = Field(max_length=40)
    marker: str = Field(pattern="^[0-9a-f]{8,16}$")
    app: str = Field(default="", max_length=40)
    bundle: dict = Field(default_factory=dict)


@router.post("/logs/moment")
def upload_moment(body: MomentIn):
    """0.7 (`moments` in status): a marked moment's content-free bundle (frame times, the page's layout as boxes),
    written beside the log as <device>/moments/<marker>.json. Same sign-in as /logs; rebuilt field by field."""
    try:
        return perf_logs.write_moment(perf_logs.log_dir(), body.device, body.marker, body.bundle, app=body.app)
    except perf_logs.LogError as error:
        raise HTTPException(error.status_code, error.detail) from None
