"""The Hermes HQ theme for the Hermes desktop app, put in place by this plugin.

The desktop app loads ``<Hermes folder>/desktop-plugins/<name>/plugin.js`` on its own and lists a theme it adds under
Settings > Appearance > Theme. ``hermes-hq-theme.js`` here is that plugin (hermes-ios desktop-plugin/hermes-hq-theme);
on load this copies it there, or brings an older copy up to date. It only adds a choice: the theme in use stays the
person's. The same file is what Hermes HQ setup and the app's Add button write. (Shipped as this plugin's own
``desktop/plugin.js`` instead, the desktop app would list it switched off.)
"""
import os
from pathlib import Path

THEME = Path(__file__).with_name("hermes-hq-theme.js")
FOLDER = "hermes-hq-theme"


def theme_path(root: Path) -> Path:
    return Path(root) / "desktop-plugins" / FOLDER / "plugin.js"


def install(root: Path, source: Path = THEME) -> str:
    """'added', 'updated' or 'same'; 'missing' without the theme file here. Raises only on a write that failed."""
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    target = theme_path(root)
    try:
        if target.read_text(encoding="utf-8") == text:
            return "same"
        outcome = "updated"
    except OSError:
        outcome = "added"
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".plugin.js.{os.getpid()}.tmp")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, target)  # the desktop app reloads it on change: never a half-written file
    return outcome
