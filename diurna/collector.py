"""Deterministic event reducer. No I/O, titles, or wall-clock reads here."""
from dataclasses import dataclass, replace
import re
from typing import Optional

# MPRIS bus names are org.mpris.MediaPlayer2.<player>[.instance<id>].
_BUS_PREFIX = "org.mpris.mediaplayer2."
_INSTANCE = re.compile(r"\.instance[0-9_]*$")
# Chromium-family app windows (`--app`, Omarchy web apps) are classed chrome-<site>,
# while their media plays through the browser's one player.
_CHROMIUM_PLAYERS = ("chromium", "chrome", "google-chrome", "brave")


@dataclass(frozen=True)
class Context:
    app_class: str = ""
    workspace: str = ""
    idle: bool = False
    locked: bool = False
    suspended: bool = False
    connected: bool = True
    # Raw activity signals; None when no source reports them (no shell plugin).
    # input: keyboard or pointer input within the hands-on window.
    input: Optional[bool] = None
    # media: the focused app has an MPRIS player that is Playing (True), only
    # players that are not (False), or none (None). Derived from `players`.
    media: Optional[bool] = None
    # inhibiting: the focused window holds an idle inhibitor.
    inhibiting: Optional[bool] = None

    @property
    def reason(self):
        for flag, reason in ((self.suspended, "suspended"),
                             (self.locked, "locked"), (self.idle, "idle"),
                             (not self.connected, "disconnected")):
            if flag:
                return reason
        return "active"


@dataclass(frozen=True)
class Span:
    start_ns: int
    end_ns: int
    app_class: str
    workspace: str
    reason: str
    input: Optional[bool] = None
    media: Optional[bool] = None
    inhibiting: Optional[bool] = None


def _recorded(context):
    return (context.app_class, context.workspace, context.reason,
            context.input, context.media, context.inhibiting)


def player_name(name):
    """A player's app name: its desktop entry, or its MPRIS bus name without the
    prefix and instance suffix."""
    name = name.lower()
    if name.startswith(_BUS_PREFIX):
        name = _INSTANCE.sub("", name[len(_BUS_PREFIX):])
    return name


def same_app(app_class, name):
    """Whether a player name belongs to a window class, by name only."""
    app, name = app_class.lower(), player_name(name)
    if not app or not name:
        return False
    return (app == name or app.rsplit(".", 1)[-1] == name or app.startswith(name + "-")
            or (app.startswith("chrome-") and name in _CHROMIUM_PLAYERS))


def focused_media(app_class, players):
    """True if a player of the focused app is Playing, False if it has players
    but none is, None if it has none. Other apps' players never count."""
    states = [playing for name, playing in players.items() if same_app(app_class, name)]
    return any(states) if states else None


class Collector:
    def __init__(self, clock, write, context=None, inhibitor=lambda: None):
        """`inhibitor()` reads whether the focused window holds an idle inhibitor
        now (None if unknown). Hyprland sends no event for it, so the reducer
        asks on focus, fullscreen and input changes; the snapshot net refreshes it."""
        self.clock, self.write, self.inhibitor = clock, write, inhibitor
        self.context = context or Context()
        self.start = clock()
        self.address = ""
        self.windows = {}
        self.pending_class = None
        self.players = {}
        self.asked = False

    def boundary(self, **changes):
        self.boundary_at(self.clock(), **changes)

    def boundary_at(self, end, **changes):
        """End the open span only if the event changes what a span records.

        Title changes re-send `activewindow` for the same window; those, and any
        other event that leaves (app, workspace, reason) and the activity signals
        as they were, extend the span. Media is re-derived for the focused app, so
        a player in another app never splits it.
        """
        context = replace(self.context, **changes)
        context = replace(context, media=focused_media(context.app_class, self.players))
        if _recorded(context) == _recorded(self.context):
            if end < self.start:
                raise ValueError("collector clock moved backwards")
        else:
            self.checkpoint_at(end)
        self.context = context

    def checkpoint(self):
        self.checkpoint_at(self.clock())

    def checkpoint_at(self, end):
        """Commit the open span up to `end` and keep it open: durability, not a change."""
        if end < self.start:
            raise ValueError("collector clock moved backwards")
        if end > self.start:
            c = self.context
            self.write(Span(self.start, end, c.app_class, c.workspace, c.reason,
                            c.input, c.media, c.inhibiting))
        self.start = end

    def snapshot(self, active, workspace, clients):
        self.windows = {w["address"].removeprefix("0x"): w.get("class", "")
                        for w in clients if "address" in w}
        self.address = active.get("address", "").removeprefix("0x")
        inhibiting = active.get("inhibitingIdle")
        self.boundary(app_class=active.get("class", ""), workspace=workspace,
                      connected=True,
                      inhibiting=inhibiting if isinstance(inhibiting, bool) else None)

    def signal(self, flag, value):
        """A desktop signal (runtime.HOOKS). Input stopping is when an inhibitor
        starts to matter, so re-read it then."""
        changes = {flag: value}
        if flag == "input":
            changes["inhibiting"] = self.inhibitor()
        self.boundary(**changes)

    def media(self, players):
        """Every MPRIS player's name and whether it is Playing."""
        self.players = dict(players)
        self.boundary()

    def _focus(self, app, ask):
        changes = {"app_class": app}
        if ask:
            changes["inhibiting"] = self.inhibitor() if app or self.address else None
        self.boundary(**changes)

    def hyprland(self, line):
        event, sep, data = line.partition(">>")
        if not sep:
            return False
        if event == "activewindow":
            app, sep, _ = data.partition(",")
            if not sep:
                return False
            self.pending_class = app
            # Ask here, not only at activewindowv2, so the new app's span starts
            # with its own inhibitor. Title changes re-send this for the same app.
            self.asked = app != self.context.app_class
            self._focus(app, self.asked)
        elif event == "activewindowv2":
            previous, self.address = self.address, data.removeprefix("0x")
            app = self.pending_class
            if app is None:
                app = self.windows.get(self.address, "")
            if self.address:
                self.windows[self.address] = app
            self.pending_class = None
            self._focus(app if self.address else "", self.address != previous and not self.asked)
            self.asked = False
        elif event == "fullscreen":
            # Compositor idle_inhibit rules can depend on fullscreen.
            self.boundary(inhibiting=self.inhibitor() if self.address else None)
        elif event in ("workspace", "workspacev2", "focusedmon", "focusedmonv2"):
            if event == "workspace":
                workspace = data
            else:
                _, sep, workspace = data.partition(",")
                if not sep:
                    return False
            # focusedmonv2 carries an ID, not the workspace name.
            if event == "focusedmonv2":
                return False
            self.boundary(workspace=workspace)
        elif event == "openwindow":
            fields = data.split(",", 3)
            if len(fields) != 4:
                return False
            address, _, app, _ = fields
            # Opening a window does not move focus; its activewindow event does.
            self.windows[address.removeprefix("0x")] = app
        elif event == "closewindow":
            address = data.removeprefix("0x")
            self.windows.pop(address, None)
            self.boundary(**({"app_class": "", "inhibiting": None}
                             if address == self.address else {}))
            if address == self.address:
                self.address = ""
        elif event in ("movewindow", "movewindowv2"):
            fields = data.split(",", 2)
            if len(fields) != (3 if event.endswith("v2") else 2):
                return False
            self.boundary(**({"workspace": fields[-1]}
                            if fields[0].removeprefix("0x") == self.address else {}))
        else:
            return False
        return True
