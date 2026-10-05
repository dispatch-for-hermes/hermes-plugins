"""Dispatch push: Apple notifications for the Dispatch iPhone app.

Hooks run wherever an agent turn runs; they only enqueue. App sessions (platform desktop/tui) live in
``hermes serve``, which also mounts ``dashboard/plugin_api.py``, so an approval pushed from here can be
answered from the lock screen through that route. Messaging-platform sessions are ignored; a scheduled job's delivery into a bot's chat alerts like an app turn.
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
_replies: dict = {}  # session id -> the turn's reply (post_llm_call), until on_session_end sends it


def _profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _sender(profile: str) -> str:
    """The bot's name as the Bots roster shows it: its Bot Mode title, the profile's display name, or its id
    in words. Read-only profile.yaml metadata through Hermes' own helper."""
    try:
        from hermes_cli.profiles import get_profile_dir, read_profile_meta
        meta = read_profile_meta(get_profile_dir(profile))
        return meta.get("bot_title") or meta.get("display_name") or push.alert_copy.readable_profile(profile)
    except Exception:
        return push.alert_copy.readable_profile(profile)


def _dispatcher_for():
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = push.Dispatcher(push.Store(push.data_dir() / "devices.db"))
    return _dispatcher


def _serves_app_sessions() -> bool:
    """App approvals are queued in the process running tui_gateway (``hermes serve``), which marks
    itself interactive; the messaging gateway's approvals belong to its own platforms."""
    return "tui_gateway.server" in sys.modules and os.environ.get("HERMES_INTERACTIVE") == "1"


def post_llm_call(**kwargs):
    try:
        session = str(kwargs.get("session_id") or "")
        if session and (kwargs.get("platform") in push.APP_PLATFORMS or push.bot_chat_delivery()):
            _replies[session] = str(kwargs.get("assistant_response") or "")[:4000]
            if len(_replies) > 64:
                _replies.pop(next(iter(_replies)))
    except Exception:
        log.debug("dispatch-push reply hook failed", exc_info=True)


def on_session_end(**kwargs):
    try:
        profile = _profile()
        text = _replies.pop(str(kwargs.get("session_id") or ""), "")
        delivery = push.bot_chat_delivery()
        item = push.turn_push(kwargs, profile, _sender(profile), text, delivery=delivery)
        if delivery and item is not None:
            # A delivery child exits right after its one turn, before a daemon thread could send: send it now.
            _dispatcher_for().deliver(item)
        else:
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("dispatch-push turn hook failed", exc_info=True)


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
    # The reply preview's hook: a Hermes that doesn't know it must not keep the plugin from loading (alerts then say
    # who finished, without the reply's first words).
    try:
        ctx.register_hook("post_llm_call", post_llm_call)
    except Exception:
        log.warning("dispatch-push: post_llm_call hook unavailable; reply alerts carry no preview", exc_info=True)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
