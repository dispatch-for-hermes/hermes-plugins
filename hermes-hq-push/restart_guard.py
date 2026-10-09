"""Keeps a bot's "restart it once" from becoming a restart loop that locks the phone out (macOS).

``launchctl submit`` jobs are KeepAlive: launchd re-runs them forever. Bots reach for it to restart the
Hermes web server "once, in 15 seconds", and the server then restarts every ~20 s, so the Hermes HQ app
can't sign in (seen twice on 2026-10-08). Two parts:

- ``blocked_message``: the ``pre_tool_call`` veto. Any tool call whose arguments run ``launchctl submit``
  is refused with the safe way to do it, so the bot retries correctly.
- ``Sweeper``: for loops started some other way (a script the bot wrote, an older plugin, a person). It
  removes a job only when launchd says it was submitted (no plist), its command names the Hermes folder
  or a ``launchctl kickstart``, and it has already run ``MIN_RUNS`` times. Each Hermes process that loads
  the plugin sweeps on load and then every ``INTERVAL`` seconds, at most once a minute across processes.
"""
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("hermes-hq-push")

SUBMIT = re.compile(r"\blaunchctl\b[^\n;|&]{0,80}?\bsubmit\b")
MIN_RUNS = 3
INTERVAL = 60
SKIP_PREFIXES = ("application.", "com.apple.")

BLOCKED = (
    "Blocked by Hermes HQ: `launchctl submit` jobs are KeepAlive, so macOS re-runs them forever. A one-time "
    "restart made this way restarts the service every few seconds and locks the Hermes HQ app out.\n"
    "- Restart now: `launchctl kickstart -k gui/$(id -u)/<label>` (the Hermes web server is ai.hermes.fleet-serve).\n"
    "- Restart after a delay: `nohup /bin/sh -c 'sleep 15; launchctl kickstart -k gui/$(id -u)/<label>' >/dev/null 2>&1 &`\n"
    "Never create a launchd job for a one-off task."
)


def _strings(value, depth=0):
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)


def blocked_message(args) -> Optional[str]:
    """The veto message when any string in a tool call's arguments runs ``launchctl submit``."""
    return BLOCKED if any(SUBMIT.search(text) for text in _strings(args)) else None


def _run(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Sweeper:
    def __init__(self, hermes_root: Path, stamp: Path, runner: Callable[..., str] = _run, uid: Optional[int] = None):
        self.root = str(Path(hermes_root).expanduser())
        self.stamp = stamp
        self.run = runner
        self.domain = f"gui/{os.getuid() if uid is None else uid}"
        self.harmless: set = set()  # labels already seen to be something else: looked at once only

    def _suspect(self, info: str) -> Optional[int]:
        """The run count of a submitted job whose command is a Hermes restart, else None."""
        if "type = Submitted" not in info:
            return None
        command = re.search(r"\n\s*arguments = \{(.*?)\n\s*\}", info, re.S)
        if not command or (self.root not in command.group(1) and "kickstart" not in command.group(1)):
            return None
        runs = re.search(r"^\s*runs = (\d+)", info, re.M)
        return int(runs.group(1)) if runs else 0

    def sweep(self) -> list:
        removed = []
        for row in self.run("/bin/launchctl", "list").splitlines()[1:]:
            parts = row.split("\t")
            if len(parts) < 3:
                continue
            label = parts[2].strip()
            if not label or label in self.harmless or label.startswith(SKIP_PREFIXES):
                continue
            info = self.run("/bin/launchctl", "print", f"{self.domain}/{label}")
            if not info:
                continue
            runs = self._suspect(info)
            if runs is None:
                self.harmless.add(label)
            elif runs >= MIN_RUNS:
                self.run("/bin/launchctl", "remove", label)
                log.warning("hermes-hq-push: removed launchd job %s: launchctl submit made it re-run (%d runs)", label, runs)
                removed.append(label)
        return removed

    def due(self, now: float) -> bool:
        """One sweep a minute however many Hermes processes load the plugin."""
        try:
            if now - self.stamp.stat().st_mtime < INTERVAL - 5:
                return False
        except OSError:
            pass
        try:
            self.stamp.parent.mkdir(parents=True, exist_ok=True)
            self.stamp.touch()
        except OSError:
            pass
        return True

    def loop(self):
        while True:
            try:
                if self.due(time.time()):
                    self.sweep()
            except Exception:
                log.debug("hermes-hq-push: restart guard sweep failed", exc_info=True)
            time.sleep(INTERVAL)


_started = False


def start(hermes_root: Path, data_dir: Path):
    global _started
    if _started or sys.platform != "darwin":
        return
    _started = True
    sweeper = Sweeper(hermes_root, Path(data_dir) / "restart-guard.stamp")
    threading.Thread(target=sweeper.loop, name="hermes-hq-restart-guard", daemon=True).start()
