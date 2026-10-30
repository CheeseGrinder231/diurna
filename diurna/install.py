"""Add or remove the Diurna bar module by text insertion, keeping every other byte.

A small JSONC scanner records offsets, so install inserts one array element and
(for Waybar) one module definition, and uninstall removes exactly those spans.
An untouched install-then-uninstall round trip is byte-identical.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from .changes import WAYBAR_SIGNAL
from .config import policy
from . import shell_plugin

WAYBAR_MODULE = "custom/diurna"
WAYBAR_ARRAYS = ("modules-left", "modules-center", "modules-right")
SHELL_ID = "diurna"
BACKUP_SUFFIX = ".diurna-backup"
FALLBACK_INTERVAL = 60
ICON = "\U000f0150"  # Nerd Font md-clock-outline; Omarchy ships Nerd Fonts.


class InstallError(ValueError):
    pass


@dataclass
class Node:
    kind: str
    start: int
    end: int = 0
    members: list = field(default_factory=list)  # object: (key, key_start, Node)
    items: list = field(default_factory=list)  # array: Node

    def get(self, key):
        return next((value for name, _, value in self.members if name == key), None)


class Scanner:
    def __init__(self, text):
        self.text, self.at = text, 0

    def fail(self):
        raise InstallError(f"cannot parse config near offset {self.at}")

    def skip(self):
        text = self.text
        while self.at < len(text):
            if text[self.at] in " \t\r\n":
                self.at += 1
            elif text.startswith("//", self.at):
                end = text.find("\n", self.at)
                self.at = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.at):
                end = text.find("*/", self.at + 2)
                if end < 0:
                    self.fail()
                self.at = end + 2
            else:
                break

    def value(self):
        self.skip()
        if self.at >= len(self.text):
            self.fail()
        char = self.text[self.at]
        if char == "{":
            return self.container("object", "}")
        if char == "[":
            return self.container("array", "]")
        if char == '"':
            return self.string()
        start = self.at
        while self.at < len(self.text) and self.text[self.at] not in ",:]} \t\r\n/":
            self.at += 1
        if start == self.at:
            self.fail()
        try:
            json.loads(self.text[start:self.at])
        except ValueError:
            self.at = start
            self.fail()
        return Node("literal", start, self.at)

    def string(self):
        start = self.at
        self.at += 1
        while self.at < len(self.text):
            char = self.text[self.at]
            if char == "\\":
                self.at += 2
            elif char == '"':
                self.at += 1
                return Node("string", start, self.at)
            elif char == "\n":
                break
            else:
                self.at += 1
        self.fail()

    def container(self, kind, close):
        node = Node(kind, self.at)
        self.at += 1
        while True:
            self.skip()
            if self.at < len(self.text) and self.text[self.at] == close:
                self.at += 1
                node.end = self.at
                return node
            if kind == "object":
                key_node = self.value()
                if key_node.kind != "string":
                    self.fail()
                self.skip()
                if not self.text.startswith(":", self.at):
                    self.fail()
                self.at += 1
                node.members.append((json.loads(self.text[key_node.start:key_node.end]),
                                     key_node.start, self.value()))
            else:
                node.items.append(self.value())
            self.skip()
            if self.text.startswith(",", self.at):
                self.at += 1
            elif not self.text.startswith(close, self.at):
                self.fail()


def parse(text):
    scanner = Scanner(text)
    root = scanner.value()
    scanner.skip()
    if scanner.at != len(text):
        scanner.fail()
    return root


def _string(text, node):
    return json.loads(text[node.start:node.end]) if node.kind == "string" else None


def _insert_first(text, container, snippet):
    """Insert as the first element/member; `, ` keeps the original layout intact."""
    at = container.start + 1
    empty = not (container.items or container.members)
    return text[:at] + snippet + ("" if empty else ", ") + text[at:]


def _remove(text, container, index):
    """Remove one element/member; the exact inverse of `_insert_first` when untouched."""
    parts = container.items or [value for _, _, value in container.members]
    starts = ([n.start for n in container.items] if container.items
              else [key_start for _, key_start, _ in container.members])
    start, end = starts[index], parts[index].end
    if index == 0 and start == container.start + 1:
        if len(parts) == 1:
            return text[:start] + text[end:]
        if text.startswith(", ", end):
            return text[:start] + text[end + 2:]
    if index + 1 < len(parts):
        return text[:start] + text[starts[index + 1]:]
    if index:
        return text[:parts[index - 1].end] + text[end:]
    return text[:start] + text[end:]


def _write(path, data):
    target = path.resolve()
    mode = target.stat().st_mode & 0o7777
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read(path):
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except FileNotFoundError:
        raise InstallError(f"config not found: {path}") from None


def program():
    """The installed executable when run as one, else `diurna` from PATH."""
    launcher = Path(sys.argv[0])
    if launcher.suffix != ".py" and launcher.is_file() and os.access(launcher, os.X_OK):
        return str(launcher.absolute())
    return "diurna"


def commands(executable):
    command = shlex.quote(executable)
    # TUI.float is Omarchy's floating-terminal app id; launch-or-focus reuses an open one.
    return (f"{command} bar",
            f"omarchy-launch-or-focus-tui --app-id=TUI.float {command} today --watch")


# Waybar ---------------------------------------------------------------------

def waybar_config():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        base = Path.home() / ".config"
    for name in ("config.jsonc", "config"):
        if (base / "waybar" / name).exists():
            return base / "waybar" / name
    return base / "waybar/config.jsonc"


def _waybar_bar(root):
    bar = root.items[0] if root.kind == "array" and root.items else root
    if bar.kind != "object":
        raise InstallError("Waybar config must be a bar object or a list of them")
    return bar


def waybar_add(text, executable):
    bar = _waybar_bar(parse(text))
    listed = any(_string(text, item) == WAYBAR_MODULE
                 for name in WAYBAR_ARRAYS if (array := bar.get(name)) and array.kind == "array"
                 for item in array.items)
    defined = bar.get(WAYBAR_MODULE) is not None
    if listed and defined:
        return text
    if listed or defined:
        raise InstallError("Waybar config has a partial custom/diurna; remove it first")
    right = bar.get("modules-right")
    if right is None or right.kind != "array":
        raise InstallError("Waybar config has no modules-right list")
    execute, click = commands(executable)
    module = {"exec": execute, "return-type": "json", "interval": FALLBACK_INTERVAL,
              "signal": WAYBAR_SIGNAL, "escape": True, "format": ICON + " {}",
              "tooltip": True, "on-click": click}
    # The list sits inside the bar object: insert there first so bar.start stays valid.
    text = _insert_first(text, right, json.dumps(WAYBAR_MODULE))
    text = _insert_first(text, bar, "\n  " + json.dumps(WAYBAR_MODULE) + ": "
                         + json.dumps(module, ensure_ascii=False))
    parse(text)
    return text


def waybar_remove(text):
    bar = _waybar_bar(parse(text))
    for name in WAYBAR_ARRAYS:
        array = bar.get(name)
        if array is None or array.kind != "array":
            continue
        for index, item in enumerate(array.items):
            if _string(text, item) == WAYBAR_MODULE:
                text = _remove(text, array, index)
                bar = _waybar_bar(parse(text))
                break
    for index, (key, _, _) in enumerate(bar.members):
        if key == WAYBAR_MODULE:
            text = _remove_member(text, bar, index)
            break
    parse(text)
    return text


def _remove_member(text, bar, index):
    _, key_start, value = bar.members[index]
    # Exact inverse of the "\n  key: value, " insertion at the top of the object.
    if index == 0 and text[bar.start + 1:key_start] == "\n  " and len(bar.members) > 1 \
            and text.startswith(", ", value.end):
        return text[:bar.start + 1] + text[value.end + 2:]
    return _remove(text, bar, index)


# omarchy-shell (Omarchy 4 bar) ----------------------------------------------

def shell_config():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "omarchy/shell.json"


def _shell_right(text):
    node = parse(text)
    for key in ("bar", "layout", "right"):
        node = node.get(key) if node.kind == "object" else None
        if node is None:
            raise InstallError("shell.json has no bar.layout.right list")
    if node.kind != "array":
        raise InstallError("shell.json bar.layout.right is not a list")
    return node


def _shell_index(text, right):
    for index, item in enumerate(right.items):
        if item.kind == "object" and (ident := item.get("id")) and _string(text, ident) == SHELL_ID:
            return index
    return None


def shell_bar_add(text, executable):
    right = _shell_right(text)
    if _shell_index(text, right) is not None:
        return text
    execute, click = commands(executable)
    # omarchy-shell command modules re-run on their interval only; the expanded
    # view is still event-driven through the collector's change stamp.
    entry = {"id": SHELL_ID, "type": "command", "exec": execute,
             "interval": FALLBACK_INTERVAL, "onClick": click, "tooltip": "Diurna"}
    text = _insert_first(text, right, json.dumps(entry, ensure_ascii=False))
    parse(text)
    return text


def shell_bar_remove(text):
    right = _shell_right(text)
    index = _shell_index(text, right)
    return text if index is None else _remove(text, right, index)


def _plugin_index(text, plugins):
    return next((i for i, item in enumerate(plugins.items)
                 if item.kind == "object" and (ident := item.get("id"))
                 and _string(text, ident) == shell_plugin.PLUGIN_ID), None)


def shell_add(text, executable):
    text = shell_bar_add(text, executable)
    root = parse(text)
    plugins = root.get("plugins")
    entry = json.dumps({"id": shell_plugin.PLUGIN_ID})
    if plugins is None:
        return _insert_first(text, root, '"plugins": [' + entry + ']')
    if plugins.kind != "array":
        raise InstallError("shell.json plugins is not a list")
    return text if _plugin_index(text, plugins) is not None else _insert_first(text, plugins, entry)


def shell_remove(text):
    root = parse(text)
    plugins = root.get("plugins")
    if plugins and plugins.kind == "array":
        index = _plugin_index(text, plugins)
        if index is not None:
            text = _remove(text, plugins, index)
    return shell_bar_remove(text)


def shell_idle_seconds(text):
    # Match omarchy.idle's first enabled timeout at installation. When both
    # actions are disabled, Diurna still measures idle after 150 seconds.
    root = parse(text)
    idle = root.get("idle")
    times = []
    for key, default in (("screensaver", 150), ("lock", 300)):
        node = idle.get(key) if idle and idle.kind == "object" else None
        try:
            seconds = int(float(text[node.start:node.end])) if node else default
            if seconds < 0:
                seconds = default
        except (ValueError, OverflowError):
            seconds = default
        if seconds:
            times.append(seconds)
    return min(times) if times else 150


def hands_on_seconds():
    # The plugin's input-only monitor takes whole seconds, fixed at install.
    return max(1, round(policy()["hands_on_seconds"]))


# Files ----------------------------------------------------------------------

TARGETS = {"waybar": (waybar_config, waybar_add, waybar_remove),
           "omarchy-shell": (shell_config, shell_add, shell_remove)}


def install(target, path=None, executable=None):
    locate, add, remove = TARGETS[target]
    path = Path(path) if path else locate()
    original = _read(path)
    executable = executable or program()
    updated = add(original, executable)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists() and updated != original:
        saved = _read(backup)
        # Also allow upgrading a bar-only install made before the plugin.
        if saved != original and saved != remove(original) and not (
                target == "omarchy-shell" and saved == shell_bar_remove(original)):
            raise InstallError(f"{backup} exists from an earlier install; move it away first")
    if target == "omarchy-shell":
        try:
            shell_plugin.prepare(path, executable, shell_idle_seconds(original),
                                 hands_on_seconds())
        except ValueError as error:
            raise InstallError(str(error)) from error
    if updated == original:
        return f"{target}: already installed in {path}"
    if not backup.exists():
        backup.write_text(original, encoding="utf-8", newline="")
    _write(path, updated)
    return f"{target}: installed in {path} (backup {backup})"


def uninstall(target, path=None):
    locate, _, remove = TARGETS[target]
    path = Path(path) if path else locate()
    current = _read(path)
    updated = remove(current)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if target == "omarchy-shell" and backup.exists():
        # If we introduced plugins[], remove that member when empty. Preserve
        # an originally empty plugins[] and any plugins subsequently added.
        root, saved = parse(updated), parse(_read(backup))
        plugins = root.get("plugins")
        if saved.get("plugins") is None and plugins and plugins.kind == "array" and not plugins.items:
            index = next(i for i, (name, _, _) in enumerate(root.members) if name == "plugins")
            updated = _remove(updated, root, index)
    if updated != current:
        _write(path, updated)
    extra = shell_plugin.remove(path) if target == "omarchy-shell" else ""
    if updated == current:
        return f"{target}: not installed in {path}{extra}"
    if backup.exists() and _read(backup) == updated:
        backup.unlink()
        return f"{target}: removed from {path}; config matches the backup{extra}"
    return f"{target}: removed from {path}; config was edited since install, backup kept at {backup}{extra}"
