"""Dispatch Browser: watch a bot's browser live from Dispatch, and take it over.

Every process that runs agents loads this and publishes its bots' browsers (agent.py) through owner-only
files under ``<hermes root>/dispatch-browser`` (spool.py). ``hermes serve`` also mounts
``dashboard/plugin_api.py``, which lists those browsers, relays Chrome's own screencast to the phone and
carries the person's taps and typing while they hold a browser. Bots keep every browser tool Hermes gives
them: nothing in Hermes is overridden or patched. If the browser internals agent.py reads change shape,
the plugin stays off. See README.md.
"""
import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger("dispatch-browser")


def load(name: str):
    """This plugin's sibling modules, one instance per process (shared with dashboard/plugin_api.py)."""
    key = f"dispatch_browser_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def register(ctx):
    try:
        agent = load("agent")
        if agent.install(load("spool")):
            return
        ctx.register_hook("pre_tool_call", agent.before_tool)
        ctx.register_hook("post_tool_call", agent.after_tool)
        ctx.register_tool(name=agent.ASK_TOOL, toolset="browser", schema=agent.ASK_SCHEMA, handler=agent.ask_user,
                          check_fn=agent.ask_available, description=agent.ASK_SCHEMA["description"], emoji="🙋")
    except Exception:  # noqa: BLE001 - a broken plugin must never break the agent
        log.warning("dispatch-browser: could not start", exc_info=True)
