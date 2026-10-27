"""Bar JSON, the expanded today view and its change-driven watch loop."""
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import select
import sqlite3

from .today import SECOND

TOOLTIP_APPS = 5
VIEW_ROWS = 10
DESKTOP = "(desktop)"
# The collector commits at least every quiet interval (default 60 s); a longer
# uncommitted tail suggests it is not running.
STALE_NS = 120 * SECOND
# inotify(7) constants; stable kernel ABI.
IN_CLOSE_WRITE, IN_MOVED_TO, IN_CREATE = 0x08, 0x80, 0x100


def duration(ns):
    minutes = max(0, ns) // (60 * SECOND)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def app_label(app):
    return app or DESKTOP


def clock_label(ns):
    return datetime.fromtimestamp(ns // SECOND).strftime("%H:%M")


def row_duration(ns):
    return f"{ns // SECOND}s" if ns < 60 * SECOND else duration(ns)


STATE_LABELS = {
    "idle": "idle", "locked": "locked", "suspended": "asleep",
    "unknown": "unknown (collector stopped)",
    "disconnected": "unknown (Hyprland disconnected)",
    "not_recorded": "not recorded (Diurna not running)",
}


def row_label(row, stale):
    """What a timeline row was: an app on a workspace, a mix of apps, or a state."""
    if row["state"] == "pending":
        return "not saved (is the collector running?)" if stale else "now, not saved yet"
    if row["state"] != "active":
        return STATE_LABELS[row["state"]]
    switches = row["switches"]
    more = f" (+{switches} switch{'es' if switches > 1 else ''})" if switches else ""
    if row["app_class"] is not None:
        return f"{app_label(row['app_class'])} · ws {row['workspace'] or '?'}{more}"
    apps = {}
    for entry in row["apps"]:
        apps[entry["app_class"]] = apps.get(entry["app_class"], 0) + entry["active_ns"]
    top = sorted(apps.items(), key=lambda item: (-item[1], item[0]))
    listed = ", ".join(f"{app_label(app)} {row_duration(value)}" for app, value in top[:3])
    others = f", +{len(top) - 3} more" if len(top) > 3 else ""
    return f"mixed · {listed}{others}{more}"


def bar(summary):
    """Waybar custom-module JSON; omarchy-shell command modules read the same shape.

    Text is plain: the module config sets `escape` so Pango never sees app names.
    """
    total = duration(summary["active_ns"])
    lines = [f"Today {total} active"]
    apps = summary["apps"][:TOOLTIP_APPS]
    width = max((len(app_label(a["app_class"])) for a in apps), default=0)
    lines += [f"{app_label(a['app_class']).ljust(width)}  {duration(a['active_ns'])}"
              for a in apps]
    away = [(label, summary["states"][key]) for label, key in (
        ("idle", "idle_ns"), ("locked", "locked_ns"), ("asleep", "suspended_ns"))]
    away = [f"{label} {duration(value)}" for label, value in away if value]
    if away:
        lines.append(" · ".join(away))
    classes = ["diurna"] + ([] if summary["active_ns"] else ["empty"])
    return {"text": total, "tooltip": "\n".join(lines), "class": classes,
            "alt": "empty" if "empty" in classes else "tracking"}


def unavailable():
    return {"text": "–", "tooltip": "Diurna: screen time unavailable",
            "class": ["diurna", "unavailable"], "alt": "unavailable"}


def _meter(value, largest, width=20):
    if largest <= 0:
        return ""
    return "█" * max(1 if value else 0, round(width * value / largest))


def render(summary, now_label=""):
    states = summary["states"]
    lines = [f"Diurna · today {summary['date'] or ''}"
             + (f" · updated {now_label}" if now_label else ""), ""]
    rows = [("Active", states["active_ns"]), ("Idle", states["idle_ns"]),
            ("Locked", states["locked_ns"]), ("Asleep", states["suspended_ns"])]
    rows += [(label, value) for label, value in (
        ("Unknown", states["unknown_ns"] + states["disconnected_ns"]),
        ("Not recorded", summary["not_recorded_ns"]),
        ("Not saved", summary["pending_ns"] if summary["pending_ns"] >= STALE_NS else 0))
        if value]
    lines += [f"  {label:<12}{duration(value):>8}" for label, value in rows]
    apps = summary["apps"][:VIEW_ROWS]
    largest = apps[0]["active_ns"] if apps else 0
    width = max((len(app_label(a["app_class"])) for a in apps), default=0)
    lines += ["", "Apps"]
    lines += [f"  {app_label(a['app_class']).ljust(width)}  {duration(a['active_ns']):>8}  "
              f"{_meter(a['active_ns'], largest)}" for a in apps] or ["  (no active time yet)"]
    windows = summary["windows"][:VIEW_ROWS]
    labels = [app_label(w["app_class"]) + f" · ws {w['workspace'] or '?'}"
              + (f" · {w['title']}" if w["title"] else "") for w in windows]
    width = max(map(len, labels), default=0)
    lines += ["", "Windows (app · workspace; titles are not recorded)"]
    lines += [f"  {label.ljust(width)}  {duration(w['active_ns']):>8}"
              for label, w in zip(labels, windows)] or ["  (no active time yet)"]
    lines += ["", "Timeline"]
    for row in summary["timeline"]:
        span = row["end_ns"] - row["start_ns"]
        lines.append(f"  {clock_label(row['start_ns'])}–{clock_label(row['end_ns'])}"
                     f"  {row_duration(span):>7}  {row_label(row, span >= STALE_NS)}")
    return "\n".join(lines)


def changes_directory():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "diurna" if runtime and Path(runtime).is_absolute() else None


class Watcher:
    """inotify on the collector's change stamp; the timeout is only a missed-event net."""

    def __init__(self, directory):
        self.fd = -1
        if directory is None:
            return
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            if libc.inotify_add_watch(fd, os.fsencode(directory),
                                      IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE) < 0:
                os.close(fd)
                return
            self.fd = fd
        except (OSError, AttributeError):
            self.fd = -1

    def wait(self, timeout):
        if self.fd < 0:
            select.select([], [], [], timeout)
            return
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                while os.read(self.fd, 65536):
                    pass
            except BlockingIOError:
                pass

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def watch(load, write, now_label, watcher, fallback_seconds=60):
    """Redraw on each collector change; return only when interrupted."""
    try:
        while True:
            try:
                screen = render(load(), now_label())
            except (OSError, ValueError, sqlite3.Error) as error:
                screen = f"Diurna: screen time unavailable ({type(error).__name__})"
            write("\x1b[H\x1b[2J" + screen + "\n\nCtrl-C to close\n")
            watcher.wait(fallback_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        watcher.close()


def dumps(value):
    return json.dumps(value, ensure_ascii=False)
