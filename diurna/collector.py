"""Deterministic event reducer. No I/O, titles, or wall-clock reads here."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Context:
    app_class: str = ""
    workspace: str = ""
    idle: bool = False
    locked: bool = False
    suspended: bool = False
    connected: bool = True

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


def _recorded(context):
    return context.app_class, context.workspace, context.reason


class Collector:
    def __init__(self, clock, write, context=None):
        self.clock, self.write = clock, write
        self.context = context or Context()
        self.start = clock()
        self.address = ""
        self.windows = {}
        self.pending_class = None

    def boundary(self, **changes):
        self.boundary_at(self.clock(), **changes)

    def boundary_at(self, end, **changes):
        """End the open span only if the event changes what a span records.

        Title changes re-send `activewindow` for the same window; those, and any
        other event that leaves (app, workspace, reason) as it was, extend the span.
        """
        context = replace(self.context, **changes)
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
            self.write(Span(self.start, end, c.app_class, c.workspace, c.reason))
        self.start = end

    def snapshot(self, active, workspace, clients):
        self.windows = {w["address"].removeprefix("0x"): w.get("class", "")
                        for w in clients if "address" in w}
        self.address = active.get("address", "").removeprefix("0x")
        self.boundary(app_class=active.get("class", ""), workspace=workspace,
                      connected=True)

    def hyprland(self, line):
        event, sep, data = line.partition(">>")
        if not sep:
            return False
        if event == "activewindow":
            app, sep, _ = data.partition(",")
            if not sep:
                return False
            self.pending_class = app
            self.boundary(app_class=app)
        elif event == "activewindowv2":
            self.address = data.removeprefix("0x")
            app = self.pending_class
            if app is None:
                app = self.windows.get(self.address, "")
            if self.address:
                self.windows[self.address] = app
            self.pending_class = None
            self.boundary(app_class=app if self.address else "")
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
            self.boundary(**({"app_class": ""} if address == self.address else {}))
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
