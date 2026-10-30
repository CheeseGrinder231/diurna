import argparse
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys
import time

from . import bar
from .export import ERROR as EXPORT_ERROR, export
from .store import migrate
from .install import InstallError, install, uninstall
from .runtime import HOOKS, MEDIA, notify, serve
from .today import today


def database():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "diurna/spans.sqlite3"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Raw requests may carry private source text. Never echo bad arguments.
        if sys.argv[1:2] in (["export"], ["migrate"]):
            self.exit(2, "diurna: invalid request\n")
        super().error(message)


def main():
    parser = Parser(prog="diurna")
    commands = parser.add_subparsers(dest="command", required=True)
    raw = commands.add_parser("export", help="read-only versioned raw-span JSONL")
    raw.add_argument("--database", type=Path, required=True)
    raw.add_argument("--start-ns", type=int, required=True)
    raw.add_argument("--end-ns", type=int, required=True)
    raw.add_argument("--through-start-ns", type=int, help="cap the saved source high point")
    upgrade = commands.add_parser("migrate", help="migrate existing storage while collection is stopped")
    upgrade.add_argument("--database", type=Path, required=True)
    daemon = commands.add_parser("collect", help="collect local desktop spans")
    daemon.add_argument("--database", type=Path, default=database())
    hook = commands.add_parser("signal", help="desktop idle, lock, sleep, input or media event")
    hook.add_argument("event", choices=(*HOOKS, MEDIA))
    hook.add_argument("players", nargs="*", metavar="PLAYER:STATE",
                      help="media only: each MPRIS player as name:playing or name:paused")
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
    if args.command in ("export", "migrate"):
        try:
            if args.command == "export":
                export(args.database, sys.stdout.buffer, args.start_ns, args.end_ns,
                       args.through_start_ns)
                sys.stdout.buffer.flush()
            else:
                migrate(args.database)
        except (OSError, ValueError, sqlite3.Error, OverflowError, MemoryError) as error:
            if isinstance(error, BrokenPipeError):
                # Avoid a second, unsanitized flush error at interpreter shutdown.
                fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(fd, sys.stdout.fileno())
                finally:
                    os.close(fd)
            print(EXPORT_ERROR if args.command == "export" else "diurna: migration failed\n",
                  file=sys.stderr, end="")
            return 1
        return 0
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
            notify(args.event, args.players)
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error):
        # Never log raw IPC, command payloads or titles in error diagnostics.
        print("diurna: collection/hook failed; check session environment and service", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
