"""Hermes HQ push: Apple notifications for the Hermes HQ iPhone app (called dispatch-push before 0.5).

Hooks run wherever an agent turn runs; they only enqueue. App sessions (platform desktop/tui) live in
``hermes serve``, which also mounts ``dashboard/plugin_api.py``, so an approval pushed from here can be
answered from the lock screen through that route. Messaging-platform sessions are ignored. Reply alerts don't come from hooks: ``push.ReplyWatcher`` (started by
``dashboard/plugin_api.py`` in the server) reads every profile's replies, so one install covers every bot.
On load it also puts the Hermes HQ theme in the desktop app's plugin folder (desktop_theme.py), and
restart_guard.py keeps a bot's one-time restart from looping and locking the app out.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).with_name("push.py"))
push = importlib.util.module_from_spec(_spec)
sys.modules["hermes_hq_push_core"] = push  # before exec: dataclasses resolve annotations through it
_spec.loader.exec_module(push)
_guard_spec = importlib.util.spec_from_file_location("hermes_hq_push_restart_guard", Path(__file__).with_name("restart_guard.py"))
restart_guard = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(restart_guard)

log = logging.getLogger(push.NAME)
_dispatcher = None
_sent_approvals: dict = {}


def _profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


_sender = push.profile_sender


def _dispatcher_for():
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = push.Dispatcher(push.Store(push.data_dir() / "devices.db"))
    return _dispatcher


def _serves_app_sessions() -> bool:
    """App approvals are queued in the process running tui_gateway (``hermes serve``), which marks
    itself interactive; the messaging gateway's approvals belong to its own platforms."""
    return "tui_gateway.server" in sys.modules and os.environ.get("HERMES_INTERACTIVE") == "1"


def pre_approval_request(**kwargs):
    try:
        if not _serves_app_sessions():
            return
        from tools.approval import list_gateway_approvals
        profile = _profile()
        item = push.approval_push(kwargs, list_gateway_approvals(str(kwargs.get("session_key") or "")),
                                  push.data_dir(), profile, _sender(profile))
        if item is not None:
            push.remember_approval(_sent_approvals, kwargs, item)
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("hermes-hq-push approval hook failed", exc_info=True)


def post_approval_response(**kwargs):
    try:
        _dispatcher_for().submit(push.settled_push(kwargs, _sent_approvals))
    except Exception:
        log.debug("hermes-hq-push settle hook failed", exc_info=True)


def _desktop_theme():
    """The Hermes HQ theme for the desktop app (desktop_theme.py): a nicety, so nothing here ever stops the plugin."""
    try:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_theme", Path(__file__).with_name("desktop_theme.py"))
        theme = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(theme)
        from hermes_constants import get_default_hermes_root
        outcome = theme.install(get_default_hermes_root())
        if outcome in ("added", "updated"):
            log.info("hermes-hq-push: %s the Hermes HQ theme for the desktop app", outcome)
    except Exception:
        log.debug("hermes-hq-push: desktop theme not written", exc_info=True)


def pre_tool_call(tool_name="", args=None, **kwargs):
    try:
        message = restart_guard.blocked_message(args)
    except Exception:
        return None
    if message:
        log.warning("hermes-hq-push: blocked a %s call that ran launchctl submit", tool_name)
        return {"action": "block", "message": message}
    return None


def _restart_guard():
    try:
        from hermes_constants import get_default_hermes_root
        restart_guard.start(get_default_hermes_root(), push.data_dir())
    except Exception:
        log.debug("hermes-hq-push: restart guard not started", exc_info=True)


def register(ctx):
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    _desktop_theme()
    _restart_guard()
