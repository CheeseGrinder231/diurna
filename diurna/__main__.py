import argparse
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys
import time

from . import bar
from .install import InstallError, install, uninstall
from .runtime import HOOKS, notify, serve
from .today import today


def database():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "diurna/spans.sqlite3"


def main():
    parser = argparse.ArgumentParser(prog="diurna")
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("collect", help="collect local desktop spans")
    daemon.add_argument("--database", type=Path, default=database())
    hook = commands.add_parser("signal", help="desktop idle, lock or sleep event")
    hook.add_argument("event", choices=HOOKS)
    day = commands.add_parser("today", help="today's screen time by state, app and window")
    day.add_argument("--database", type=Path, default=database())
    view = day.add_mutually_exclusive_group()
    view.add_argument("--json", action="store_true", help="machine-readable totals in ns")
    view.add_argument("--watch", action="store_true", help="live tabbed view, redrawn on each change")
    module = commands.add_parser("bar", help="Waybar custom-module JSON for today")
    module.add_argument("--database", type=Path, default=database())
    for name, verb in (("install", "add"), ("uninstall", "remove")):
        command = commands.add_parser(name, help=f"{verb} the bar module")
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--waybar", dest="target", action="store_const", const="waybar")
        target.add_argument("--omarchy-shell", dest="target", action="store_const",
                            const="omarchy-shell", help="the Omarchy 4 bar (shell.json)")
        command.add_argument("--config", type=Path, help="config file to edit")
        if name == "install":
            command.add_argument("--program", help="diurna executable the bar runs")
    args = parser.parse_args()
    if args.command == "bar":
        try:
            output = bar.bar(today(args.database, time.time_ns()))
        except (OSError, ValueError, sqlite3.Error):
            output = bar.unavailable()
        print(bar.dumps(output))
        return 0
    if args.command == "today":
        load = lambda: today(args.database, time.time_ns())  # noqa: E731
        try:
            if args.watch:
                now_label = lambda: datetime.now().strftime("%H:%M")  # noqa: E731
                watcher = bar.Watcher(bar.changes_directory())
                if sys.stdin.isatty() and sys.stdout.isatty():
                    from . import tui  # curses only for the terminal view
                    return tui.run(load, now_label, watcher)
                return bar.watch(load, sys.stdout.write, now_label, watcher)
            summary = load()
        except (OSError, ValueError, sqlite3.Error):
            print("diurna: cannot read stored spans", file=sys.stderr)
            return 1
        print(bar.dumps(summary) if args.json else bar.render(summary))
        return 0
    if args.command in ("install", "uninstall"):
        try:
            if args.command == "install":
                message = install(args.target, args.config, args.program)
            else:
                message = uninstall(args.target, args.config)
        except (OSError, InstallError) as error:
            print(f"diurna: {error}", file=sys.stderr)
            return 1
        print(message)
        restart = "omarchy-restart-waybar" if args.target == "waybar" \
            else "omarchy-shell shell reloadConfig"
        print(f"Reload the bar to apply: {restart}")
        return 0
    try:
        if args.command == "collect":
            serve(args.database)
        else:
            notify(args.event)
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error):
        # Never log raw IPC, command payloads or titles in error diagnostics.
        print("diurna: collection/hook failed; check session environment and service", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
