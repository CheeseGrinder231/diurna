"""The tabbed live view behind `diurna today --watch`: Overview, Timeline, Apps.

Rendering is pure: `View.lines()` returns exactly one line per terminal row, each
a list of (text, style) segments no wider than the terminal, so tests snapshot it
without a terminal. `run` paints those lines with curses on the alternate screen,
in the terminal's own palette, and redraws in place on a key, a collector change
(inotify), a resize (SIGWINCH) or the quiet-interval net.
"""
import curses
from datetime import date as Date
import os
import select
import signal
import sqlite3
import sys
import unicodedata

from . import bar
from .today import settle

TABS = ("Overview", "Timeline", "Apps")
TOP_APPS = 5
FOOTER = "Tab ←→ 1-3 tabs · ↑↓ jk PgUp PgDn scroll · q quit"
# Day strip and timeline glyph per category of state; the category names the colour style.
SHADES = {"active": "█", "idle": "▒", "away": "░", "none": "·"}
CATEGORY = {"active": "active", "idle": "idle", "locked": "away", "suspended": "away",
            "unknown": "none", "disconnected": "none", "not_recorded": "none",
            "pending": "none"}
TOTAL_CATEGORY = {"Active": "active", "· hands-on": "active", "· watching": "active",
                  "· on screen": "active", "Idle": "idle", "Locked": "away", "Asleep": "away"}
PART_LABELS = {"idle": "idle", "locked": "locked", "suspended": "asleep",
               "unknown": "unknown", "disconnected": "unknown",
               "not_recorded": "not recorded", "pending": "not saved"}
ESCAPE = 27
QUIT = (ord("q"), ord("Q"), ESCAPE)


def cells(text):
    """Terminal columns `text` takes: wide characters two, combining marks none."""
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def clip(line, width):
    """Segments cut to `width` columns, ending in … when cut; control characters
    in app names become ? so they cannot move the cursor."""
    out, used = [], 0
    for text, style in line:
        text = "".join(c if c.isprintable() else "?" for c in text)
        if used + cells(text) <= width:
            out.append((text, style))
            used += cells(text)
            continue
        kept = ""
        for c in text:
            if used + cells(kept + c) > width - 1:
                break
            kept += c
        out.append((kept + "…" if width > used else kept, style))
        break
    return out


def fit(text, width):
    """Plain text cut to `width` columns, then padded to it."""
    text = "".join(text for text, _ in clip([(text, "")], width))
    return text + " " * (width - cells(text))


def view_row_label(row):
    """A settled timeline row: an app on a workspace, a state, or a mix of both."""
    span = row["end_ns"] - row["start_ns"]
    if row["state"] is not None:
        return bar.row_label(row, span >= bar.STALE_NS)
    totals = {}
    for part in row["parts"]:
        name = bar.app_label(part["app_class"]) if part["state"] == "active" \
            else PART_LABELS[part["state"]]
        totals[name] = totals.get(name, 0) + part["ns"]
    top = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    listed = ", ".join(f"{name} {bar.row_duration(value)}" for name, value in top[:3])
    others = f", +{len(top) - 3} more" if len(top) > 3 else ""
    switches = row["switches"]
    more = bar.activity_note(row["activity"])
    more += f" (+{switches} switch{'es' if switches > 1 else ''})" if switches else ""
    return f"mixed · {listed}{others}{more}"


def row_category(row):
    if row["state"] is not None:
        return CATEGORY[row["state"]]
    return max(SHADES, key=lambda category: sum(
        part["ns"] for part in row["parts"] if CATEGORY[part["state"]] == category))


def day_strip(timeline, start_ns, end_ns, width):
    """One cell per equal slice of [start_ns, end_ns): the category holding most of it."""
    if width <= 0 or end_ns <= start_ns:
        return []
    strip, index = [], 0
    for cell in range(width):
        low = start_ns + (end_ns - start_ns) * cell // width
        high = start_ns + (end_ns - start_ns) * (cell + 1) // width
        while index < len(timeline) and timeline[index]["end_ns"] <= low:
            index += 1
        totals = dict.fromkeys(SHADES, 0)
        for row in timeline[index:]:
            if row["start_ns"] >= high:
                break
            totals[CATEGORY[row["state"]]] += min(row["end_ns"], high) - max(row["start_ns"], low)
        strip.append(max(SHADES, key=lambda category: totals[category]))
    return strip


def axis(start_ns, end_ns, width):
    """Clock labels under the strip: midnight on the left, now on the right, the
    middle between them when there is room."""
    line = [" "] * width
    labels = [(0, bar.clock_label(start_ns)), (width - 5, bar.clock_label(end_ns))]
    if width >= 20:
        labels.append((width // 2 - 2, bar.clock_label((start_ns + end_ns) // 2)))
    for position, label in labels:
        if position >= 0:
            line[position:position + 5] = label
    return "".join(line[:width])


class View:
    """What the tabbed view shows; keys, resizes and updates change it."""

    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.tab = 0
        # Lines scrolled past per tab; None follows the end, so the timeline
        # opens at now and stays there as rows arrive.
        self.offsets = [0, None, 0]
        self.summary, self.error, self.updated = None, None, ""

    def update(self, summary, updated):
        self.summary, self.error, self.updated = summary, None, updated

    def fail(self, error, updated):
        self.summary, self.error, self.updated = None, error, updated

    def resize(self, height, width):
        self.height, self.width = max(1, height), max(1, width)

    def page(self):
        return max(1, self.height - 4)

    def offset(self, total):
        """First body line shown, within range for `total` lines."""
        last = max(0, total - self.page())
        wanted = self.offsets[self.tab]
        return last if wanted is None else min(wanted, last)

    def key(self, key):
        """Apply one key; False when it closes the view."""
        if key in QUIT:
            return False
        tabs = {ord("\t"): self.tab + 1, curses.KEY_BTAB: self.tab - 1,
                curses.KEY_RIGHT: self.tab + 1, curses.KEY_LEFT: self.tab - 1,
                ord("1"): 0, ord("2"): 1, ord("3"): 2}
        if key in tabs:
            self.tab = tabs[key] % len(TABS)
            return True
        page = self.page()
        steps = {curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_UP: -1, ord("k"): -1,
                 curses.KEY_NPAGE: page, ord(" "): page, curses.KEY_PPAGE: -page,
                 curses.KEY_HOME: -sys.maxsize, ord("g"): -sys.maxsize,
                 curses.KEY_END: sys.maxsize, ord("G"): sys.maxsize}
        if key in steps:
            total = len(self.body())
            last = max(0, total - page)
            wanted = max(0, min(last, self.offset(total) + steps[key]))
            self.offsets[self.tab] = None if wanted == last and self.tab == 1 else wanted
        return True

    def lines(self):
        body = self.body()
        start = self.offset(len(body))
        shown = body[start:start + self.page()]
        lines = [self.header(), self.tabs(len(body), start, len(shown)), []]
        lines += shown + [[]] * (self.page() - len(shown))
        lines.append([(FOOTER, "dim")])
        lines = lines[:self.height - 1] + lines[-1:] if self.height > 1 else lines[-1:]
        return [clip(line, self.width) for line in lines]

    def header(self):
        title = "Diurna"
        if self.summary and self.summary["date"]:
            day = Date.fromisoformat(self.summary["date"])
            title += f" · {day.strftime('%A')} {day.isoformat()}"
        if self.updated:
            title += f" · updated {self.updated}"
        return [(title, "header")]

    def tabs(self, total, start, shown):
        line = []
        for index, name in enumerate(TABS):
            line += [(" ", ""), (f" {name} ", "tab_on" if index == self.tab else "tab")]
        if shown < total:
            position = f"{start + 1}–{start + shown} of {total}"
            room = self.width - sum(cells(text) for text, _ in line) - 1
            if room >= cells(position) + 1:
                line += [(" " * (room - cells(position)), ""), (position, "dim")]
        return line

    def body(self):
        if self.error is not None:
            return [[(f"  Screen time unavailable ({self.error})", "")]]
        if self.summary is None:
            return [[("  Loading…", "dim")]]
        return (self.overview, self.timeline, self.apps)[self.tab]()

    def overview(self):
        summary = self.summary
        totals = bar.state_totals(summary)
        # Cells of 21 columns ("█ Label  value"), three apart, after a margin of two.
        columns = max(1, (self.width + 1) // 24)
        lines = []
        for first in range(0, len(totals), columns):
            line = [("  ", "")]
            for label, value in totals[first:first + columns]:
                category = TOTAL_CATEGORY.get(label, "none")
                line += [(SHADES[category], category),
                         (f" {label:<12}{bar.duration(value):>7}   ", "")]
            lines.append(line)
        strip_width = max(0, self.width - 4)
        strip = day_strip(summary["timeline"], summary["start_ns"], summary["end_ns"],
                          strip_width)
        lines += [[], [("  ", "")] + [(SHADES[c], c) for c in strip],
                  [("  " + axis(summary["start_ns"], summary["end_ns"], strip_width), "dim")],
                  [], [("  Top apps", "header")]]
        return lines + self.app_rows(summary["apps"][:TOP_APPS], {})

    def app_rows(self, apps, workspaces):
        """App rows with bars, each followed by its workspaces when it spans more
        than one."""
        if not apps:
            return [[("  (no active time yet)", "dim")]]
        names = [bar.app_label(a["app_class"]) for a in apps]
        name_width = min(max(map(cells, names)), max(8, self.width // 3))
        meter_width = max(0, self.width - name_width - 15)
        largest = apps[0]["active_ns"]
        lines = []
        for name, app in zip(names, apps):
            lines.append([(f"  {fit(name, name_width)}  {bar.duration(app['active_ns']):>7}  ", ""),
                          (bar.meter(app["active_ns"], largest, meter_width), "active")])
            spread = workspaces.get(app["app_class"], [])
            if len(spread) > 1:
                lines += [[(f"    {fit('ws ' + (w['workspace'] or '?'), name_width - 2)}"
                            f"  {bar.duration(w['active_ns']):>7}", "dim")] for w in spread]
        return lines

    def timeline(self):
        lines = []
        for row in settle(self.summary["timeline"]):
            category = row_category(row)
            span = row["end_ns"] - row["start_ns"]
            lines.append([(" ", ""), (SHADES[category], category),
                          (f" {bar.clock_label(row['start_ns'])}–{bar.clock_label(row['end_ns'])}"
                           f"  {bar.row_duration(span):>7}  {view_row_label(row)}", "")])
        return lines

    def apps(self):
        workspaces = {}
        for window in self.summary["windows"]:
            workspaces.setdefault(window["app_class"], []).append(window)
        return self.app_rows(self.summary["apps"], workspaces)


def refresh(view, load, now_label):
    try:
        view.update(load(), now_label())
    except (OSError, ValueError, sqlite3.Error) as error:
        view.fail(type(error).__name__, now_label())


def loop(view, events, load, now_label, paint):
    """Draw, then redraw after each event until a key closes the view."""
    refresh(view, load, now_label)
    paint(view.lines())
    for event, value in events:
        if event == "key":
            if not view.key(value):
                return 0
        elif event == "resize":
            view.resize(*value)
        else:
            refresh(view, load, now_label)
        paint(view.lines())
    return 0


def events(window, watcher, wake, stdin, fallback_seconds, size, resize,
           wait=select.select):
    """("key", code), ("resize", (lines, columns)) or ("change", None), as they come.

    `wake` is the SIGWINCH pipe; the watcher's inotify fd marks a collector
    change, and a quiet `fallback_seconds` counts as one (the missed-event net).
    """
    while True:
        fds = [stdin, wake] + ([watcher.fd] if watcher.fd >= 0 else [])
        ready = wait(fds, [], [], fallback_seconds)[0]
        if wake in ready:
            try:
                while os.read(wake, 64):
                    pass
            except BlockingIOError:
                pass
            lines, columns = size()
            resize(lines, columns)
            yield "resize", (lines, columns)
        if stdin in ready:
            while (key := window.getch()) != -1:
                if key == curses.KEY_RESIZE:
                    yield "resize", window.getmaxyx()
                else:
                    yield "key", key
        if watcher.fd >= 0 and watcher.fd in ready:
            watcher.wait(0)  # drains the queued changes
            yield "change", None
        if not ready:
            yield "change", None


def on_resize(wake):
    """Write a byte to `wake` on SIGWINCH; returns the previous handler."""
    def handler(*_):
        try:
            os.write(wake, b"\0")
        except OSError:
            pass
    return signal.signal(signal.SIGWINCH, handler)


def palette():
    """Curses attributes per style, from the terminal's own palette: the eight
    ANSI colours over its default background, never fixed RGB."""
    styles = {"header": curses.A_BOLD, "tab": 0, "tab_on": curses.A_REVERSE | curses.A_BOLD,
              "dim": curses.A_DIM, "none": curses.A_DIM, "": 0}
    if not curses.has_colors():
        return styles
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    for pair, (style, colour) in enumerate((("active", curses.COLOR_GREEN),
                                            ("idle", curses.COLOR_YELLOW),
                                            ("away", curses.COLOR_BLUE)), 1):
        curses.init_pair(pair, colour, background)
        styles[style] = curses.color_pair(pair)
    return styles


def painter(window, styles):
    def paint(lines):
        window.erase()
        for y, line in enumerate(lines):
            x = 0
            for text, style in line:
                try:
                    window.addstr(y, x, text, styles.get(style, 0))
                except curses.error:
                    pass  # the bottom-right cell: curses cannot move past it
                x += cells(text)
        window.refresh()
    return paint


def _session(window, load, now_label, watcher, fallback_seconds):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    window.nodelay(True)
    window.keypad(True)
    view = View(*window.getmaxyx())
    wake, signal_end = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    previous = on_resize(signal_end)
    try:
        def size():
            columns, lines = os.get_terminal_size(sys.__stdout__.fileno())
            return lines, columns
        stream = events(window, watcher, wake, sys.stdin.fileno(), fallback_seconds,
                        size, curses.resizeterm)
        return loop(view, stream, load, now_label, painter(window, palette()))
    finally:
        signal.signal(signal.SIGWINCH, previous)
        os.close(wake)
        os.close(signal_end)


def run(load, now_label, watcher, fallback_seconds=60):
    """The tabbed view until q or Esc (0) or Ctrl-C (130)."""
    os.environ.setdefault("ESCDELAY", "25")  # Esc closes without a second's wait
    try:
        return curses.wrapper(_session, load, now_label, watcher, fallback_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        watcher.close()

