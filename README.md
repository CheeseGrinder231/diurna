# Diurna: screen time for Omarchy

Named after *Acta Diurna*, Rome's daily record. Screen time for Omarchy (Hyprland): what you did on the computer, recorded locally from desktop events.

## Why

Time on the computer is a large part of a day. Diurna records it locally,
without a server or an account. Desktop events drive collection, and privacy
rules apply before data reaches storage. Existing Linux trackers either need a
server and web UI (ActivityWatch with awatcher), are early single-author binaries
(HPR, hyprwatch), or send data to a service (RescueTime clients).

## Shape

- **Event-driven collector.**
  - A small user daemon listens to Hyprland's event socket (`.socket2.sock`: active window, workspace, monitor, window open and close).
  - It also listens to idle, lock and suspend signals.
  - There is no polling loop. A span ends on the next event that changes the app, workspace or state, and a timer only stands in for a missed event.
- **Local only.** Spans go to a SQLite file on this machine: app class, workspace, start, end, idle or active, and an optional window title. Nothing leaves the computer. No screenshots, no keystrokes, no audio.
- **Privacy rules at collection.** Window titles are off by default. Per-app rules can keep a title, hash it, or map it to a project (for example a terminal's working directory or an editor's project name). A title never stored can never leak.
- **Rollups.** Per day and hour: by app, by project, active versus idle, and focus sessions (contiguous use of one app or project).
- **Surfaces.**
  - `diurna today` and `diurna week` on the command line.
  - A bar module showing today's active total, for Waybar or the Omarchy 4 bar, that expands to a live today view.
  - An Omarchy menu entry.
- **Standalone export (planned).** A versioned JSON Lines export and SQLite schema will let other local apps read spans, with stable IDs for idempotent reads.

## Collector

Python 3.10+ on Linux, standard library only. The collector reads Hyprland's
event socket and accepts idle, lock and sleep transitions from desktop signals.
On Omarchy 4, a shell plugin reports idle and Linux clock differences detect
sleep. Omarchy 3 can use hypridle hooks for all three states.
It uses Unix sockets only and makes no network calls. Titles are discarded
before reducing events and SQLite enforces `title IS NULL`. Per-app title rules,
`diurna today` and the bar module are available; per-app title rules, `diurna week`
and the versioned export are planned.

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 scripts/build.py --output "$TMPDIR/diurna.pyz"
python3 "$TMPDIR/diurna.pyz" --help
```

The shell plugin also has a headless QML behavior test, required in CI:

```bash
python3 scripts/test_shell_plugin.py --check-mutations
```

This needs Qt 6's `qmltestrunner`, QtQuick and QtTest (Arch: `qt6-declarative`;
Ubuntu: `qt6-declarative-dev-tools`, `qml6-module-qtquick`,
`qml6-module-qtquick-window`, `qml6-module-qttest`, `qml6-module-qtqml` and
`qml6-module-qtqml-workerscript`). Quickshell and a desktop session are not
needed: fake native types drive the shipped QML's initial state and idle/active
transitions, recording process commands without launching them. Tests use
synchronous stimuli, without wall-clock deadlines. Mutation checks remove each
notification in scratch and require the behavior assertions to fail. The
collector and built artifact still require only Python's standard library.

### Manual installation

Build the collector using the commands above, then install the artifact and unit:

```bash
install -Dm755 "$TMPDIR/diurna.pyz" "$HOME/.local/bin/diurna"
install -Dm644 packaging/diurna.service "$HOME/.config/systemd/user/diurna.service"
```

#### Omarchy 3: hypridle

Merge [packaging/hypridle.conf.example](packaging/hypridle.conf.example) into the
existing hypridle configuration. Retain every existing hook command by
prepending the diurna command followed by `;`. The example's five-minute
idle threshold is a user setting. `on_lock_cmd` / `on_unlock_cmd` observe actual
screen-lock transitions; `before_sleep_cmd` / `after_sleep_cmd` observe sleep.
Idle and lock hooks are required for those classifications; sleep hooks remain
supported alongside automatic sleep-gap detection. Restart hypridle after
merging its config, while the desktop is awake and unlocked.

#### Omarchy 4: shell plugin

After installing the executable and unit above, run:

```bash
diurna install --omarchy-shell
omarchy-shell shell reloadConfig
```

This installs the bar module and the `diurna.activity` service plugin under
`$XDG_CONFIG_HOME/omarchy/plugins/diurna.activity` (default `~/.config/omarchy`),
and enables it in `shell.json`'s `plugins` array. It uses Quickshell's native
`IdleMonitor`, the same ext-idle-notify mechanism as `omarchy.idle`, and sends
`diurna signal idle|active` on changes. It sends its initial state on load,
serializes signals, and retries a failed acknowledgement after 60 seconds.
No hypridle, hyprlock or hand-written hooks are needed for idle and sleep.

At install time, the idle threshold matches the first enabled `idle.screensaver`
or `idle.lock` timeout in `shell.json` (defaults 150 and 300 seconds). If both
are disabled, Diurna uses 150 seconds. Reinstall after changing those settings;
remove the unchanged plugin with uninstall first. The monitor respects compositor
idle inhibitors. Omarchy's Stay Awake switch disables its own idle service; it
does not disable Diurna's separate monitor.

**Lock limitation (Omarchy 4.0.6):** the shell's third-party plugin API exposes
neither the idle service object nor authentication lock state. Diurna therefore
uses its own native idle monitor and falls back to **idle only** for the shell
plugin. Locked time is counted as idle after the idle threshold, and can still
count as active before it. It is not reported as locked without an external
`diurna signal lock|unlock` source. Diurna does not probe private authentication
objects or claim that idle means locked. See the shell's
[plugin API](https://github.com/omacom/omarchy/blob/quattro/shell/services/PluginShellApi.qml)
and [scoped service lookup](https://github.com/omacom/omarchy/blob/quattro/shell/shell.qml).

`diurna uninstall --omarchy-shell` removes the plugin entry, unchanged shipped
plugin files and bar module. Other plugins and edits remain. A config untouched
since install is restored byte for byte; edited plugin files are kept with a
message. Upgrading an existing bar-only install is supported.

#### Start the collector

From a Hyprland terminal, import the session environment and start the service:

```bash
systemctl --user import-environment HYPRLAND_INSTANCE_SIGNATURE XDG_RUNTIME_DIR
systemctl --user daemon-reload
systemctl --user enable --now diurna.service
journalctl --user -u diurna.service
```

The unit follows `graphical-session.target`. If Hyprland is not ready at login,
the collector stays running and retries session discovery with exponential
backoff from 250 ms to at most 5 seconds between attempts. It prefers the imported
Hyprland signature, but if that is missing or stale it discovers a single live
instance under `$XDG_RUNTIME_DIR/hypr/` (default `/run/user/<uid>/hypr/`). A later
systemd environment import does not need to reach the already running process.
Dead sockets are skipped; multiple live instances without a live explicit
signature leave the collector waiting to avoid recording the wrong session.
The waiting interval from collector startup is saved as **unknown**, with no
app attributed to it. The unit also delays failure restarts by five seconds and
disables the start-rate cutoff so a slow login cannot exhaust retries.
A second collector using the same database is refused by an exclusive lock.
The service's address-family sandbox
allows only Unix sockets.

Storage defaults to `$XDG_STATE_HOME/diurna/spans.sqlite3`, falling back to
`~/.local/state/diurna/spans.sqlite3`. Override it with `collect --database`.
New directories and database files are private to the user. Stop the service
before copying or moving the database; keep the original for rollback.
Uninstalling the unit or executable does not remove stored spans.

### Today and the bar

`diurna today` reads the stored spans for the local day (midnight to now,
read-only) and prints the active total, idle, locked and asleep time, unknown
time from collector restarts, time not recorded because no collector ran, time
per app (window class), the top windows and a timeline. Titles are not stored
yet, so a window is an app on a workspace. `diurna today --json` prints the same
totals as integer nanoseconds. The current span is counted once the collector
commits it: on a change, or at the checkpoint every `safety_seconds` (default 60).
`not_recorded_ns` is time before the first span or between spans. `pending_ns` is
the time after the last committed span; the live view shows it as "Not saved"
only past two minutes, when the collector may be stopped.

The timeline (`timeline` in the JSON) says from when to when, what. Its rows tile
the day exactly from midnight to now, each `{start_ns, end_ns, state, app_class,
workspace, switches, apps}`:

```text
Timeline
  00:00–08:41   8h 41m  not recorded (Diurna not running)
  08:41–08:47       5m  foot · ws 2 (+36 switches)
  08:47–08:53       6m  mixed · slack 2m, firefox 2m, mpv 54s, +4 more (+15 switches)
  08:53–09:12      19m  code · ws 1 (+14 switches)
  09:12–09:22      10m  idle
  09:22–09:23      30s  now, not saved yet
```

- Idle, locked, asleep (`suspended`), unknown (`unknown`, `disconnected`: the
  collector stopped or lost Hyprland), not recorded (`not_recorded`) and the
  unsaved tail (`pending`) are each their own row.
- Within active time, focus shorter than three minutes is a switch, not an
  activity. The shortest such row joins a neighbour until none is left: all three
  when both neighbours are the same app and workspace, otherwise the shorter
  neighbour. Shortest first, so sub-second blips (a file picker, a popup) rejoin
  the stay they interrupted; the shorter neighbour, so a burst of switching
  gathers into its own row instead of vanishing into the stay before it.
- A row is named by the app and workspace holding more than half its time,
  otherwise it is mixed (`app_class` null) and lists its top apps. `switches`
  counts focus changes inside the row; `apps` has the active time of each.

Three minutes because real switching is fast: in the first 38 minutes recorded
on a real desktop, 137 of 176 stays were under 10 seconds and only 10 lasted a
minute. A two-minute threshold gave about 11 rows an hour; five minutes made most
of the hour one mixed row. Three gives about 7, each nameable. The per-app totals
above the timeline still count every second.

`diurna bar` prints custom-module JSON: the text is today's active total, the
tooltip lists the top five apps plus idle, locked and asleep time. Classes are
`diurna`, plus `empty` with no active time yet or `unavailable` when the store
cannot be read, so the theme's CSS styles it (`#custom-diurna` in Waybar).
The text also says "not recording" if storage cannot be read, the last saved
span is unknown/disconnected, or the unsaved tail reaches two minutes (twice the
checkpoint interval if longer). This uses saved spans rather than probing the
service. It clears when normal spans are saved again.
Clicking the module opens `diurna today --watch` in Omarchy's floating terminal
(`omarchy-launch-or-focus-tui --app-id=TUI.float`): a full-screen view with three
tabs that fits its window, so the terminal itself never scrolls.

- **Overview:** the state totals, a day strip from midnight to now (one cell per
  slice, shaded by its main state: █ active, ▒ idle, ░ away (locked or asleep),
  · not recorded) and the top five apps with bars.
- **Timeline:** the rows above, opened scrolled to now. Here any row shorter than
  the three-minute fold, idle and other states included, joins a neighbour by the
  same rule, so a sub-second idle blip is not a `0s idle` row. A row whose
  largest part holds less than half its time is mixed.
- **Apps:** every app ranked with a bar; an app used on more than one workspace
  lists them beneath it.

Tab / Shift-Tab, ← / → or 1–3 switch tabs; ↑ / ↓, j / k, PgUp / PgDn, g / G
scroll inside one; q or Esc closes. It uses curses on the alternate screen with
the terminal's own palette colours, and redraws in place when the collector
commits spans (inotify on `$XDG_RUNTIME_DIR/diurna/changed`), on resize, and on a
60-second net. When stdin or stdout is not a terminal, `--watch` reprints the
plain `diurna today` text instead; plain `diurna today` and `--json` are unchanged.

After each batch of events that commits spans, the collector rewrites that change
stamp and sends `SIGRTMIN+11` to the user's own `waybar` processes, so the Waybar
module refreshes on change. The module's 60-second `interval` is only the net.

Add the module, after installing the executable as above:

```bash
diurna install --waybar          # Waybar: $XDG_CONFIG_HOME/waybar/config.jsonc (or config)
omarchy-restart-waybar
diurna install --omarchy-shell   # Omarchy 4 bar: ~/.config/omarchy/shell.json
omarchy-shell shell reloadConfig
```

Install inserts the module at the start of `modules-right` (the first bar of a
multi-bar Waybar config) or of `bar.layout.right`, and keeps a copy of the file at
`<config>.diurna-backup`. Comments and formatting elsewhere are untouched. The
module runs the executable that ran `install` (override with `--program`). Use
`--config PATH` for another file. Running install twice changes nothing.

`diurna uninstall --waybar` or `--omarchy-shell` removes exactly what install added.
When nothing else changed, the file is byte-identical to the backup and the backup
is deleted; when the file was edited since, those edits stay and the backup is kept.
Install refuses to overwrite a kept backup that differs from the current file.

The Omarchy 4 bar (omarchy-shell) re-runs command modules on their interval only,
so there the bar text refreshes every 60 seconds; the expanded view still updates
on each change.

### Span behavior and recovery

A recognized focus, workspace, monitor, window-close or desktop signal event ends
the preceding span only if it changes the app class, workspace or reason. Events
that change nothing extend the open span: a terminal that retitles itself re-sends
`activewindow` for the same window many times a second, which used to write about
3,300 rows an hour. Paired events at the same fake timestamp produce no
zero-length rows. An empty desktop is active time with an empty app class.

Because one span can now stay open for hours, the collector checkpoints it: once
the open span is `safety_seconds` old, it commits it and continues it as a new
row with the same app, workspace and reason. The live view therefore never shows
a long stay in one app as "Not saved", and a crash loses at most one interval.
A checkpoint is chosen over a view-side rule because the view cannot tell a
running collector's open span from a stopped one without it, and could not say
which app the unsaved time belongs to. The timeline merges checkpoint rows and
older no-op splits back together when it reads them.
Idle, locked and suspended states are independent flags, in that priority order
from lowest to highest: an unlock cannot clear idle, and resume cannot clear a
lock. Sleep is included in elapsed time using Linux `CLOCK_BOOTTIME`; wall-clock
adjustments during a run cannot make spans overlap or go backwards.

After each event-loop wake (including the quiet-interval net), the collector
compares `CLOCK_BOOTTIME - CLOCK_MONOTONIC` with its previous sample. An increase
measures time spent suspended. It records that duration as asleep (`suspended`
in storage) within the uncommitted tail and preserves idle and lock flags after
resume. Explicit suspend hooks take precedence, so their sleep is counted once.
Separate clock reads introduce skew; changes below one millisecond or the
measured read uncertainty are ignored. No network or D-Bus dependency is needed.

Clock differences determine sleep duration, not its exact wall-clock placement.
Without hooks the asleep interval is placed immediately before discovery; awake
time between resume and discovery can shift its endpoints. If no event arrives,
discovery waits for the quiet-interval net. No committed rows are rewritten.
Collector downtime remains unknown, rather than inferred sleep.

Only after a quiet interval does a safety net reconcile a Hyprland snapshot,
recover hook state and checkpoint the current span. It never generates focus
work while events are arriving; the age checkpoint above only commits. A lost event can be corrected only at discovery,
not retroactively assigned an invented timestamp. Hook flags persist in the
session runtime directory even when the collector is restarting. A successful
hook waits for an acknowledgement after the preceding span is committed.

On a crash, the unfinished tail is unknown. On restart, the store inserts an
explicit `unknown` span from the last committed endpoint to the new run; it
never attributes process downtime to an app. A compositor disconnect exits and
systemd restarts the daemon. A backwards wall-clock jump across restarts is
refused if it precedes the stored endpoint. Review the journal and correct the
clock before restarting; do not rewrite stored history.

Schema version 1 (`PRAGMA user_version`) stores integer epoch nanoseconds,
app class, workspace name, active flag, reason and a nullable title column.
Reasons are `active`, `idle`, `locked`, `suspended`, `disconnected`, `unknown`.
Each insert commits its boundary. `active` is false for every other reason;
consumers should show unknown time separately. The schema is internal until
the versioned export is available; do not treat it as the published export contract.

Operational settings are reloaded at call time from
`$XDG_CONFIG_HOME/diurna/config.json`, falling back to
`~/.config/diurna/config.json` when `XDG_CONFIG_HOME` is unset, empty or relative.
Values use file > named environment > bounded defaults. The optional JSON object
accepts `safety_seconds` (default 60, range 1–3600) and `ipc_timeout_seconds`
(default 3, range 0.1–30). Corresponding variables are `DIURNA_SAFETY_SECONDS`
and `DIURNA_IPC_TIMEOUT_SECONDS`.

Malformed, unsafe, oversized, unknown-key or duplicate-key files fall through to
environment values and defaults, with sanitized diagnostics. Individual invalid
values also fall through. Config files must be regular files owned by the user,
without group/other write permissions, symlinks or multiple hard links. Files
are limited to 64 KiB.

### One-hour capture check

After installation, use the computer for at least an hour, including
an idle/resume, lock/unlock and suspend/resume. Stop the collector to commit its
final boundary. Choose an exact one-hour interval within that uninterrupted run
(end minus start is `3600000000000` nanoseconds), then check it without printing
any app names or titles:

```bash
python3 scripts/verify_capture.py "$HOME/.local/state/diurna/spans.sqlite3" \
  --start-ns START_EPOCH_NS --end-ns END_EPOCH_NS
```

Use the configured XDG state path if different. The script opens SQLite read-only
and fails for gaps, overlaps, unknown/disconnected time, missing coverage or any
stored title. A pass prints `covered_ns=3600000000000 gap_ns=0 overlap_ns=0`, plus
totals by state. Check that the idle/lock/sleep boundaries match the actions you
took, then restart the service. Deterministic synthetic replay and this real-use
check are distinct: source CI uses synthetic data only.

Protocol references: [Hyprland IPC](https://wiki.hypr.land/IPC/) and
[hypridle hooks](https://wiki.hypr.land/Hypr-Ecosystem/hypridle/).

License: MIT
