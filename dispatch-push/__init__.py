"""Dispatch push: Apple notifications for the Dispatch iPhone app.

Hooks run wherever an agent turn runs; they only enqueue. App sessions (platform desktop/tui) live in
``hermes serve``, which also mounts ``dashboard/plugin_api.py``, so an approval pushed from here can be
answered from the lock screen through that route. Messaging-platform sessions are ignored. Reply alerts don't come from hooks: ``push.ReplyWatcher`` (started by
``dashboard/plugin_api.py`` in the server) reads every profile's replies, so one install covers every bot.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("dispatch_push_core", Path(__file__).with_name("push.py"))
push = importlib.util.module_from_spec(_spec)
sys.modules["dispatch_push_core"] = push  # before exec: dataclasses resolve annotations through it
_spec.loader.exec_module(push)

log = logging.getLogger("dispatch-push")
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
            _sent_approvals[push.approval_signature(kwargs)] = item
            if len(_sent_approvals) > 256:
                _sent_approvals.pop(next(iter(_sent_approvals)))
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("dispatch-push approval hook failed", exc_info=True)


def post_approval_response(**kwargs):
    try:
        _dispatcher_for().submit(push.settled_push(kwargs, _sent_approvals))
    except Exception:
        log.debug("dispatch-push settle hook failed", exc_info=True)


def register(ctx):
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
