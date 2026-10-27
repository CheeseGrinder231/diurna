# Diurna: agent guide

Product shape and operating instructions: [README.md](README.md).

## Repo facts

- Target: Omarchy on Arch Linux with Hyprland. The collector runs as a systemd user service.
- Events, not polling. Read Hyprland's `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock`. Use a timer only as a net for a missed event.
- Local only. No network calls in the collector. Never record screenshots, keystrokes, clipboard or audio. Window titles are off unless a per-app rule keeps them.
- Tests use recorded event streams and a fake clock. No wall-clock bounds in unit tests.
- Language: Python 3.10+ standard library (Linux). No pip dependencies.
- Test: `python3 -W error::ResourceWarning -m unittest discover -s tests -v`.
- Shell plugin proof: `python3 scripts/test_shell_plugin.py --check-mutations` (Qt 6 test dependencies in README).
- Build: `python3 scripts/build.py --output "$TMPDIR/diurna.pyz"`.
- Smoke test: `python3 "$TMPDIR/diurna.pyz" --help`.
- Source tests use synthetic streams and Unix sockets, never the live desktop or database.
