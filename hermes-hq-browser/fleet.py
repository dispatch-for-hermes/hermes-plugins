"""Every bot gets this plugin and a Chrome of its own, which a person can watch and take over from Hermes HQ.

The dashboard (``hermes serve``, one per machine) runs ``Supervisor``; a bot's agent process calls ``ensure`` before
a browser call when its Chrome is down, so a bot never waits on the dashboard.

- **The plugin in every bot.** Hermes loads plugins per profile, so the dashboard's copy (installed once, for the
  main profile) is copied into each profile that lacks it or has an older one, and enabled there, unless that
  profile disabled it.
- **A Chrome per bot.** A bot whose browser isn't set up some other way gets a headless Chrome of its own on a
  loopback port, with its profile in ``<bot home>/chrome-debug`` (the folder Hermes' own ``/browser connect``
  uses, so sign-ins stay between turns and restarts), and ``browser.cdp_url`` pointing at it. A bot already
  connected to a local Chrome (``/browser connect``) keeps it. The supervisor starts each Chrome, restarts one
  that exits, and ends and restarts one whose DevTools port stops answering (frozen).
- **Bots that share a Chrome get one each.** A cloned bot copies its source's ``browser.cdp_url``; the bot whose
  Chrome it is keeps the port and the copy gets its own.
- **Left alone:** ``use_real_profile``, a remote or fixed-id ``cdp_url``, Camofox, a profile that disabled the plugin,
  and a bot whose ``cdp_url`` the person removed after the plugin set it (removing it is how to opt a bot out).

State: ``<hermes root>/hermes-hq-browser/chromes/<profile>.json`` (owner-only), written by whoever last acted.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: one supervisor is still kept by the dashboard being one process
    fcntl = None

log = logging.getLogger("hermes-hq-browser")

PLUGIN = "hermes-hq-browser"
OLD_PLUGIN = "dispatch-browser"  # this plugin's name before Hermes HQ: retired in every profile, never run beside it
FIRST_PORT, LAST_PORT = 9222, 9421
PASS_EVERY = 10.0       # seconds between supervisor passes
PROBE_TIMEOUT = 5.0     # a Chrome that takes longer than this to answer /json/version missed a probe
MISSES = 4              # missed probes in a row before a Chrome is called frozen and restarted (about a minute)
START_GRACE = 30.0      # a Chrome this young is still starting
READY_WAIT = 15.0       # ensure() waits this long for a Chrome it started
MAX_BACKOFF = 300.0
WINDOW = "1280,800"
LOCAL = re.compile(r"^(?:http|ws)://(?:127\.0\.0\.1|localhost|\[::1\]):(\d{2,5})/?$")
LOOPBACK_FIXED = re.compile(r"^ws://(?:127\.0\.0\.1|localhost|\[::1\]):\d+/devtools/browser/")  # one browser, by id
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
COPY_SKIP = {".git", "tests", "__pycache__", ".DS_Store"}

# Chrome's own password manager pops a native "Save password?" bubble a headless Chrome can't show, and has stalled
# bot Chromes: a bot's Chrome starts with it off. Sign-ins still stay (cookies).
SEEDED_PREFERENCES = {
    "credentials_enable_service": False, "credentials_enable_autosignin": False,
    "profile": {"password_manager_enabled": False}, "browser": {"has_seen_welcome_page": True},
}


# ---------------------------------------------------------------------------------------------- profiles & config

def setting(name: str, default: str = "") -> str:
    """``HERMES_HQ_BROWSER_<name>`` from the environment, else the older ``DISPATCH_BROWSER_<name>``."""
    for prefix in ("HERMES_HQ_BROWSER_", "DISPATCH_BROWSER_"):
        value = os.environ.get(prefix + name, "").strip()
        if value:
            return value
    return default


def state_home(root: Path) -> Path:
    """``<hermes root>/hermes-hq-browser``, adopting the old plugin's folder the first time (spool.home)."""
    key = "hermes_hq_browser_spool"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name("spool.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key].home(root)


def hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def profiles(root: Path) -> list[tuple[str, Path]]:
    """Every bot on this machine: the main profile, then each named profile Hermes itself would list."""
    homes = [("default", root)]
    try:
        from hermes_cli.profiles import _iter_named_profile_dirs
        named = list(_iter_named_profile_dirs())
    except Exception:  # noqa: BLE001 - an older or newer Hermes: profiles with a config.yaml
        named = [config.parent for config in sorted((root / "profiles").glob("*/config.yaml"))]
    return homes + [(home.name, home) for home in named if SAFE_NAME.match(home.name)]


def read_config(home: Path) -> dict:
    path = home / "config.yaml"
    try:
        from hermes_cli.config import read_user_config_raw
        return read_user_config_raw(path) or {}
    except ImportError:
        pass
    try:
        import yaml
        value = yaml.safe_load(path.read_text()) if path.exists() else {}
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_config(home: Path, change) -> None:
    """Apply ``change(config)`` to a profile's config.yaml through Hermes' own comment-keeping writer."""
    path = home / "config.yaml"
    try:
        from hermes_cli import config as hermes_config
    except ImportError:
        hermes_config = None
    if hermes_config is not None and hasattr(hermes_config, "atomic_config_write"):
        with getattr(hermes_config, "_CONFIG_LOCK", threading.RLock()):
            config = read_config(home)
            change(config)
            hermes_config.atomic_config_write(path, config)
        return
    import yaml
    config = read_config(home)
    change(config)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(yaml.safe_dump(config, sort_keys=False))
    os.replace(temp, path)


def browser_settings(config: dict) -> dict:
    value = config.get("browser")
    return value if isinstance(value, dict) else {}


def plugin_lists(config: dict) -> tuple[list | None, list]:
    section = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    enabled = section.get("enabled")
    disabled = section.get("disabled")
    return (enabled if isinstance(enabled, list) else None), (disabled if isinstance(disabled, list) else [])


def port_of(url) -> int | None:
    found = LOCAL.match(str(url or "").strip())
    port = int(found.group(1)) if found else 0
    return port if 0 < port < 65536 else None


def loopback_port(url) -> int | None:
    """The port of any loopback address (a fixed-id one too): a port some bot already points at."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(url or "").strip())
        return parts.port if parts.hostname in ("127.0.0.1", "localhost", "::1") else None
    except ValueError:
        return None


def local_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------------------------- state files

def state_folder(root: Path) -> Path:
    path = state_home(root) / "chromes"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def read_state(root: Path, profile: str) -> dict:
    try:
        value = json.loads((state_folder(root) / f"{profile}.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(root: Path, profile: str, value: dict) -> None:
    path = state_folder(root) / f"{profile}.json"
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(value, file, separators=(",", ":"))
    os.replace(temp, path)


class Locked:
    """An exclusive lock on a file, across processes. ``acquired`` is False when ``wait`` is off and it is taken."""

    def __init__(self, path: Path, wait: bool = True):
        self.path, self.wait, self.file, self.acquired = path, wait, None, False

    def __enter__(self):
        self.file = open(self.path, "a")  # noqa: SIM115
        if fcntl is None:
            self.acquired = True
            return self
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
            self.acquired = True
        except OSError:
            self.acquired = False
        return self

    def __exit__(self, *_):
        if self.acquired and fcntl is not None:
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


# ---------------------------------------------------------------------------------------------- the plugin in every bot

def version_of(folder: Path) -> tuple:
    try:
        text = (folder / "plugin.yaml").read_text()
    except OSError:
        return ()
    found = re.search(r'^version:\s*"?([0-9][0-9.]*)"?', text, re.M)
    return tuple(int(part) for part in found.group(1).split(".") if part) if found else ()


def copy_plugin(source: Path, target: Path) -> None:
    """Copy this plugin into a profile, leaving a git checkout's history and anything else there alone."""
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in COPY_SKIP or item.name.startswith("."):
            continue
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*COPY_SKIP))
        else:
            temp = destination.with_name(f".{item.name}.{os.getpid()}.tmp")
            shutil.copy2(item, temp)
            os.replace(temp, destination)


def adopt(config: dict, home: Path) -> None:
    """Enable this plugin in a profile's config and retire the old ``dispatch-browser`` there, so the two never both
    run: where the old plugin is present, its name leaves ``plugins.enabled`` and goes into ``plugins.disabled``
    (which also stops an old dashboard copying it back). A profile that had switched the old plugin off gets this one
    switched off too, and one that disabled this plugin stays off. The old plugin's ``plugins.entries`` settings are
    carried over (bar the obsolete extension design's ``granted_capabilities``)."""
    section = config.get("plugins")
    if not isinstance(section, dict):
        section = config["plugins"] = {}
    disabled = section.get("disabled")
    disabled = list(disabled) if isinstance(disabled, list) else []
    listed = section.get("enabled")
    names = listed
    if not isinstance(names, list):
        # Hermes' first run of a profile without the list grandfathers every installed plugin into it; writing the
        # list here must not switch those off.
        installed = home / "plugins"
        names = sorted(p.name for p in installed.iterdir() if (p / "plugin.yaml").exists()) if installed.is_dir() else []
        names = [n for n in names if n not in disabled]
    names, before = list(names), list(names)
    entries = section.get("entries")
    has_old = OLD_PLUGIN in names or (home / "plugins" / OLD_PLUGIN / "plugin.yaml").exists() or (
        isinstance(entries, dict) and OLD_PLUGIN in entries)
    if PLUGIN not in disabled and PLUGIN not in names:
        if OLD_PLUGIN in disabled and OLD_PLUGIN not in names:
            disabled.append(PLUGIN)  # the person had switched the old plugin off: keep it off under its new name
        else:
            names.append(PLUGIN)
    names = [n for n in names if n != OLD_PLUGIN]
    if has_old and OLD_PLUGIN not in disabled:
        disabled.append(OLD_PLUGIN)
    if isinstance(entries, dict) and isinstance(entries.get(OLD_PLUGIN), dict) and PLUGIN not in entries:
        carried = {k: copy.deepcopy(v) for k, v in entries[OLD_PLUGIN].items() if k != "granted_capabilities"}
        if carried:
            entries[PLUGIN] = carried
    if isinstance(listed, list) or names != before:
        section["enabled"] = names
    if disabled or "disabled" in section:
        section["disabled"] = disabled


def spread(root: Path, source: Path, activate=None) -> list[str]:
    """Install and enable this plugin in every profile that lacks it or has an older copy, retiring the old
    ``dispatch-browser`` there (``adopt``). Returns the profiles changed. A profile that lists the plugin in
    ``plugins.disabled`` keeps it off."""
    changed = []
    mine = version_of(source)
    for profile, home in profiles(root):
        try:
            if home.resolve() == source.parent.parent.resolve():
                target = source  # the dashboard's own profile: its copy is the source
            else:
                target = home / "plugins" / PLUGIN
            config = read_config(home)
            enabled, disabled = plugin_lists(config)
            before = enabled is not None and PLUGIN in enabled
            wanted = copy.deepcopy(config)
            adopt(wanted, home)
            if wanted != config:
                write_config(home, lambda config, home=home: adopt(config, home))
            enabled_now = not before and PLUGIN in (plugin_lists(wanted)[0] or [])
            copied = False
            if PLUGIN not in plugin_lists(wanted)[1] and target != source and mine and version_of(target) < mine:
                copy_plugin(source, target)
                copied = True
            if copied or enabled_now or wanted != config:
                changed.append(profile)
                log.info("hermes-hq-browser: %s %s", "added to" if enabled_now else "updated in", profile)
                if activate and enabled_now:  # an update loads when the bot's process next starts
                    activate(profile)
        except Exception:  # noqa: BLE001 - one bot's trouble never stops the rest
            log.warning("hermes-hq-browser: couldn't add the plugin to %s", profile, exc_info=True)
    return changed


def activate_in_hermes(profile: str) -> None:
    """Load the plugin into a profile this Hermes is already running, as ``hermes plugins enable`` does; a profile
    that isn't running yet loads it when it starts."""
    try:
        from hermes_cli.plugins_activation import activate_plugin_now
        from hermes_cli.web_server_profiles import _config_profile_scope
        with _config_profile_scope(None if profile == "default" else profile):
            activate_plugin_now(PLUGIN)
    except Exception:  # noqa: BLE001
        log.info("hermes-hq-browser: %s picks the plugin up when it next starts", profile, exc_info=True)


# ---------------------------------------------------------------------------------------------- which Chrome each bot gets

def plan(bots: list[dict], states: dict, owners: dict, in_use) -> dict:
    """Decide each bot's browser. ``bots``: ``{profile, config}``; ``states``: profile -> state file; ``owners``:
    port -> the ``--user-data-dir`` of the Chrome on it; ``in_use(port)``: something else listens there.

    Returns profile -> ``{"manage": port, "url", "write": bool}`` or ``{"skip": reason}``."""
    decisions, claimed, wanted = {}, {}, []
    referenced = {loopback_port(browser_settings(b["config"]).get("cdp_url")) for b in bots} - {None}
    referenced |= {s.get("port") for s in states.values() if isinstance(s.get("port"), int)}

    def priority(bot):
        port = port_of(browser_settings(bot["config"]).get("cdp_url"))
        state = states.get(bot["profile"]) or {}
        mine = bool(port) and (state.get("port") == port or owners.get(port) == str(Path(bot["home"]) / "chrome-debug"))
        return (0 if mine else 1, 0 if bot["profile"] == "default" else 1, bot["profile"])

    for bot in sorted(bots, key=priority):
        profile, settings = bot["profile"], browser_settings(bot["config"])
        state = states.get(profile) or {}
        enabled, disabled = plugin_lists(bot["config"])
        url = str(settings.get("cdp_url") or "").strip()
        if PLUGIN in disabled or (enabled is not None and PLUGIN not in enabled):
            decisions[profile] = {"skip": "off"}
        elif settings.get("use_real_profile") is True:
            decisions[profile] = {"skip": "real_profile"}
        elif url and port_of(url) is None:
            decisions[profile] = {"skip": "fixed" if LOOPBACK_FIXED.match(url) else "remote"}
        elif not url and state.get("url"):
            decisions[profile] = {"skip": "released"}  # the plugin set cdp_url and the person removed it
        elif not url and str(settings.get("cloud_provider") or "").lower() == "camofox":
            decisions[profile] = {"skip": "camofox"}
        elif url and port_of(url) not in claimed:
            claimed[port_of(url)] = profile
            decisions[profile] = {"manage": port_of(url), "url": url, "write": False}
        else:
            wanted.append(profile)  # no browser of its own yet, or a copy of another bot's
    port = FIRST_PORT
    for profile in wanted:
        while port <= LAST_PORT and (port in claimed or port in referenced or in_use(port)):
            port += 1
        if port > LAST_PORT:
            decisions[profile] = {"skip": "no_port"}
            continue
        claimed[port] = profile
        decisions[profile] = {"manage": port, "url": local_url(port), "write": True}
    return decisions


def point_at(home: Path, url: str) -> None:
    def change(config):
        settings = config.get("browser")
        if not isinstance(settings, dict):
            settings = config["browser"] = {}
        settings["cdp_url"] = url
    write_config(home, change)


# ---------------------------------------------------------------------------------------------- Chrome itself

def chrome_binary() -> str | None:
    """Google Chrome (or Chromium, Brave, Edge) where Hermes' own ``/browser connect`` looks, else Playwright's."""
    override = setting("CHROME")
    if override and os.access(override, os.X_OK):
        return override
    system = platform.system()
    try:
        from hermes_cli.browser_connect import get_chrome_debug_candidates
        found = get_chrome_debug_candidates(system)
    except Exception:  # noqa: BLE001
        found = [p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                             "/Applications/Chromium.app/Contents/MacOS/Chromium",
                             shutil.which("google-chrome") or "", shutil.which("chromium") or "",
                             shutil.which("chromium-browser") or "") if p and os.path.isfile(p)]
    if found:
        return found[0]
    caches = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright",
              Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/nonexistent")]
    patterns = ("chromium-*/chrome-mac*/*.app/Contents/MacOS/*", "chromium-*/chrome-linux*/chrome")
    for cache in caches:
        for pattern in patterns:
            for path in sorted(cache.glob(pattern), reverse=True):
                if os.access(path, os.X_OK):
                    return str(path)
    return None


_versions: dict = {}


def user_agent(binary: str) -> str:
    """A desktop Chrome's user agent: headless Chrome names itself ``HeadlessChrome``, which many sites refuse."""
    major = _versions.get(binary)
    if major is None:
        try:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10).stdout
            major = re.search(r"(\d+)\.\d+", out).group(1)
        except Exception:  # noqa: BLE001
            major = "140"
        _versions[binary] = major
    system = platform.system()
    device = {"Darwin": "Macintosh; Intel Mac OS X 10_15_7", "Windows": "Windows NT 10.0; Win64; x64"}.get(system, "X11; Linux x86_64")
    return f"Mozilla/5.0 ({device}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def answers(port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"{local_url(port)}/json/version", timeout=timeout) as response:  # noqa: S310 - loopback only
            return 200 <= response.status < 300 and b"webSocketDebuggerUrl" in response.read(65536)
    except Exception:  # noqa: BLE001
        return False


def listening(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


def chrome_processes() -> list[tuple[int, str, int | None]]:
    """``(pid, --user-data-dir, --remote-debugging-port)`` of every running Chrome browser process."""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    try:
        processes = list(psutil.process_iter())
    except Exception:  # noqa: BLE001
        return []
    for process in processes:
        try:  # macOS can refuse one process's arguments with a bare PermissionError: skip that one, not the pass
            arguments = process.cmdline() or []
        except Exception:  # noqa: BLE001
            continue
        if not arguments or any(a.startswith("--type=") for a in arguments):
            continue
        folder = next((a.split("=", 1)[1] for a in arguments if a.startswith("--user-data-dir=")), None)
        port = next((a.split("=", 1)[1] for a in arguments if a.startswith("--remote-debugging-port=")), None)
        if folder:
            found.append((process.pid, str(Path(folder).expanduser()), int(port) if port and port.isdigit() else None))
    return found


def seed(folder: Path) -> None:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    preferences = folder / "Default" / "Preferences"
    if not preferences.exists():
        preferences.parent.mkdir(mode=0o700, exist_ok=True)
        preferences.write_text(json.dumps(SEEDED_PREFERENCES))
    # A copied profile folder (a clone) can carry Chrome's lock from the Chrome it came from; this folder has no
    # Chrome now (checked by the caller), so the lock is stale and would make the new Chrome hand off and quit.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (folder / name).unlink()
        except OSError:
            pass


_children: list = []


def reap() -> None:
    _children[:] = [child for child in _children if child.poll() is None]


def launch(home: Path, port: int, binary: str) -> int:
    folder = home / "chrome-debug"
    seed(folder)
    stderr = folder / "launch-stderr.log"
    try:
        if stderr.stat().st_size > 1_000_000:
            stderr.unlink()
    except OSError:
        pass
    arguments = [binary, "--headless=new", f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                 f"--user-data-dir={folder}", "--no-first-run", "--no-default-browser-check",
                 f"--window-size={WINDOW}", f"--user-agent={user_agent(binary)}"]
    if platform.system() == "Linux":
        arguments.append("--disable-dev-shm-usage")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            arguments.append("--no-sandbox")
    arguments.append("about:blank")
    with open(stderr, "ab") as errors:
        child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,  # noqa: S603
                                 start_new_session=True, close_fds=True)
    _children.append(child)
    return child.pid


def end(pid: int, folder: str) -> None:
    """End a Chrome this plugin runs for a bot (checked: its ``--user-data-dir`` is the bot's folder)."""
    try:
        import psutil
        process = psutil.Process(pid)
        if f"--user-data-dir={folder}" not in process.cmdline():
            return
        process.terminate()
        try:
            process.wait(5)
        except psutil.TimeoutExpired:
            process.kill()
    except Exception:  # noqa: BLE001
        pass
    reap()


def sample(pid: int, port: int, root: Path) -> None:
    """What a frozen Chrome was stuck on, for a later look (macOS; newest five kept)."""
    if platform.system() != "Darwin" or not os.path.exists("/usr/bin/sample"):
        return
    folder = state_home(root) / "freezes"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        subprocess.run(["/usr/bin/sample", str(pid), "3", "-mayDie", "-file",
                        str(folder / f"port{port}-{time.strftime('%Y%m%d-%H%M%S')}.txt")],
                       capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return
    for old in sorted(folder.glob("port*.txt"), key=lambda p: p.stat().st_mtime)[:-5]:
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------- one bot's Chrome

def start(root: Path, profile: str, home: Path, port: int, url: str, now: float | None = None) -> dict:
    """Start a bot's Chrome unless it already runs (another process may have just started it). Under the bot's lock;
    returns its state."""
    now = time.time() if now is None else now
    folder = str(home / "chrome-debug")
    with Locked(state_folder(root) / f"{profile}.lock"):
        state = {**read_state(root, profile), "profile": profile, "port": port, "url": url, "folder": folder}
        if answers(port, 2.0):
            return state
        running = [pid for pid, data, _ in chrome_processes() if data == folder]
        if running:  # still starting, or frozen: the supervisor's probes decide
            state.setdefault("pid", running[0])
            return state
        if listening(port):
            state.update(status="port_taken", error=f"Something else is using port {port}.")
            write_state(root, profile, state)
            return state
        binary = chrome_binary()
        if not binary:
            state.update(status="no_chrome", error="No Chrome or Chromium found on this computer.")
            write_state(root, profile, state)
            return state
        state.update(pid=launch(home, port, binary), binary=binary, launched_at=now, status="starting", error=None)
        write_state(root, profile, state)
        log.info("hermes-hq-browser: started %s's Chrome on port %d", profile, port)
        return state


def ensure(profile: str, home: Path, url: str) -> bool:
    """Agent side, before a browser call: make sure this bot's own Chrome runs. Only for a Chrome this plugin runs for
    the bot (its state file names this URL). Returns True once it answers."""
    port = port_of(url)
    if port is None or answers(port, 1.0):
        return bool(port)
    root = hermes_root()
    state = read_state(root, profile)
    if state.get("url") != url or state.get("status") in ("external", "released"):
        return False
    start(root, profile, home, port, url)
    deadline = time.time() + READY_WAIT
    while time.time() < deadline:
        if answers(port, 1.0):
            return True
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------------------------- the supervisor

class Supervisor:
    """One per machine (a lock file decides which dashboard process). Each pass: spread the plugin, decide each bot's
    Chrome, write any ``cdp_url`` that changed, start Chromes that aren't running, restart frozen ones."""

    def __init__(self, root: Path, source: Path, activate=activate_in_hermes):
        self.root, self.source, self.activate = root, source, activate
        self.misses: dict[str, int] = {}
        self.failures: dict[str, int] = {}
        self.next_try: dict[str, float] = {}
        self.launched: dict[str, bool] = {}
        self.answered: dict[str, bool] = {}
        self.statuses: dict[str, dict] = {}
        self.thread = None
        self.lock = None

    def pass_once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        reap()
        try:
            spread(self.root, self.source, self.activate)
        except Exception:  # noqa: BLE001
            log.warning("hermes-hq-browser: spreading the plugin failed", exc_info=True)
        bots, statuses = [], {}
        for profile, home in profiles(self.root):
            try:
                bots.append({"profile": profile, "home": home, "config": read_config(home)})
            except Exception:  # noqa: BLE001 - a config.yaml with a typo: that bot waits until it is fixed
                statuses[profile] = {"profile": profile, "status": "config_error"}
        # Every bot's state, a config_error one's too: its port stays its own.
        states = {name: read_state(self.root, name) for name in [b["profile"] for b in bots] + list(statuses)}
        processes = chrome_processes()
        owners = {port: data for _, data, port in processes if port}
        decisions = plan(bots, states, owners, listening)
        for bot in bots:
            profile, home = bot["profile"], Path(bot["home"])
            try:
                statuses[profile] = self.keep(profile, home, decisions.get(profile) or {"skip": "off"}, states[profile],
                                              processes, now)
            except Exception:  # noqa: BLE001
                log.warning("hermes-hq-browser: %s's Chrome check failed", profile, exc_info=True)
                statuses[profile] = {"profile": profile, "status": "error"}
        # A bot that is gone (deleted profile): end the Chrome this plugin ran for it.
        for path in state_folder(self.root).glob("*.json"):
            profile = path.stem
            if profile not in statuses:
                state = read_state(self.root, profile)
                if state.get("pid") and state.get("folder") and state.get("status") != "external":
                    end(int(state["pid"]), state["folder"])
                path.unlink(missing_ok=True)
                path.with_suffix(".lock").unlink(missing_ok=True)
                log.info("hermes-hq-browser: %s is gone; ended its Chrome", profile)
        self.statuses = statuses
        return statuses

    def keep(self, profile: str, home: Path, decision: dict, state: dict, processes, now: float) -> dict:
        if "skip" in decision:
            reason = decision["skip"]
            if reason == "released" and state.get("status") != "released":
                if state.get("pid") and state.get("folder"):
                    end(int(state["pid"]), state["folder"])
                write_state(self.root, profile, {**state, "status": "released", "pid": None})
                log.info("hermes-hq-browser: %s's cdp_url was removed; its own Chrome is off", profile)
            return {"profile": profile, "status": reason}
        port, url = decision["manage"], decision["url"]
        folder = str(home / "chrome-debug")
        if decision["write"]:
            point_at(home, url)
            log.info("hermes-hq-browser: %s now browses in its own Chrome (%s)", profile, url)
        if state.get("url") != url or state.get("port") != port or state.get("folder") != folder:
            state = {**state, "profile": profile, "port": port, "url": url, "folder": folder, "status": "starting"}
            write_state(self.root, profile, state)
        mine = [pid for pid, data, p in processes if data == folder and p == port]
        for pid, data, other in processes:
            if data == folder and other != port and state.get("status") != "external":
                end(pid, folder)  # the bot moved to another port: its Chrome follows
        if answers(port):
            self.misses[profile], self.failures[profile], self.answered[profile] = 0, 0, True
            owner = next((data for _, data, p in processes if p == port), None)
            status = "ready" if owner in (None, folder) else "external"
            if state.get("status") != status or (mine and state.get("pid") != mine[0]):
                state = {**state, "status": status, "pid": mine[0] if mine else state.get("pid"), "error": None}
                write_state(self.root, profile, state)
            return {"profile": profile, "status": status, "port": port}
        if mine:
            if now - float(state.get("launched_at") or 0) < START_GRACE:
                return {"profile": profile, "status": "starting", "port": port}
            self.misses[profile] = self.misses.get(profile, 0) + 1
            if self.misses[profile] < MISSES:
                return {"profile": profile, "status": "not_responding", "port": port}
            log.warning("hermes-hq-browser: %s's Chrome (port %d) stopped answering; restarting it", profile, port)
            sample(mine[0], port, self.root)
            end(mine[0], folder)
            self.misses[profile] = 0
        elif listening(port) and not answers(port, 1.0):
            write_state(self.root, profile, {**state, "status": "port_taken", "error": f"Something else is using port {port}."})
            return {"profile": profile, "status": "port_taken", "port": port}
        if now < self.next_try.get(profile, 0):
            return {"profile": profile, "status": state.get("status") or "starting", "port": port, "error": state.get("error")}
        # Back off only from a Chrome that keeps quitting before it ever answers (a bad flag, a broken install); one
        # that ran and then died or froze comes straight back.
        failed = self.launched.get(profile) and not self.answered.get(profile)
        self.failures[profile] = self.failures.get(profile, 0) + 1 if failed else 0
        self.next_try[profile] = now + min(MAX_BACKOFF, 15.0 * 2 ** self.failures[profile]) if failed else 0
        self.launched[profile], self.answered[profile] = True, False
        result = start(self.root, profile, home, port, url, now)
        return {"profile": profile, "status": result.get("status") or "starting", "port": port, "error": result.get("error")}

    def run(self) -> None:
        while True:
            try:
                self.pass_once()
            except Exception:  # noqa: BLE001 - the supervisor outlives any one bad pass
                log.warning("hermes-hq-browser: supervisor pass failed", exc_info=True)
            time.sleep(PASS_EVERY)

    def begin(self) -> bool:
        """Start supervising unless another process on this machine already does."""
        self.lock = Locked(state_folder(self.root) / "supervisor.lock", wait=False).__enter__()
        if not self.lock.acquired:
            self.lock.__exit__()
            self.lock = None
            return False
        self.thread = threading.Thread(target=self.run, name="hermes-hq-browser-chromes", daemon=True)
        self.thread.start()
        return True


def statuses(root: Path) -> list[dict]:
    """Each bot's browser as the state files tell it (for a dashboard process that isn't the supervisor)."""
    found = []
    for profile, _ in profiles(root):
        state = read_state(root, profile)
        found.append({"profile": profile, "status": state.get("status") or "unknown", "port": state.get("port"),
                      "error": state.get("error")})
    return found
