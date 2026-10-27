"""Event loop and hypridle hook transport; Unix sockets only."""
import fcntl
import os
from pathlib import Path
import selectors
import signal
import socket
import time

from .changes import Changes
from .collector import Collector, Context
from .config import policy
from .ipc import Lines, snapshot
from .store import Store
from .sleep import SleepDetector

SECOND = 1_000_000_000
HOOKS = {"idle": ("idle", True), "active": ("idle", False),
         "lock": ("locked", True), "unlock": ("locked", False),
         "suspend": ("suspended", True), "resume": ("suspended", False)}


def paths():
    signature = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    if not signature or "/" in signature or signature in (".", ".."):
        raise ValueError("invalid Hyprland instance signature")
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    directory = runtime / "diurna" / signature
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return runtime / "hypr" / signature, directory


class Clock:
    """Epoch anchor plus suspend-inclusive elapsed time, immune to NTP steps."""
    def __init__(self):
        self.boot = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        self.epoch = time.time_ns()

    def __call__(self):
        return self.epoch + time.clock_gettime_ns(time.CLOCK_BOOTTIME) - self.boot


def flags(directory):
    result = {}
    for flag in ("idle", "locked", "suspended"):
        try:
            value = (directory / flag).read_text()
        except FileNotFoundError:
            value = "0"
        if value not in ("0", "1"):
            raise ValueError("invalid hook state")
        result[flag] = value == "1"
    return result


def notify(event):
    """Persist state before notification, even if the collector is restarting."""
    _, directory = paths()
    flag, value = HOOKS[event]
    with (directory / (flag + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        temporary = directory / (flag + ".new")
        temporary.write_text("1" if value else "0")
        temporary.replace(directory / flag)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(policy()["ipc_timeout_seconds"])
            sock.connect(str(directory / "control.sock"))
            sock.sendall((event + "\n").encode())
            if sock.recv(16) != b"ok\n":
                raise RuntimeError("collector did not acknowledge hook")


def serve(database):
    os.umask(0o077)
    hypr, directory = paths()
    # Lock the database, not just the session socket: two instances must never
    # append overlapping spans into one store.
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with database.with_name(database.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _serve(database, hypr, directory)


def _serve(database, hypr, directory):
    clock = Clock()
    store = Store(database)
    control_path = directory / "control.sock"
    try:
        with selectors.DefaultSelector() as selector, \
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as events, \
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            _loop(selector, events, control, store, clock, hypr, directory, control_path,
                  Changes(directory.parent))
    finally:
        store.close()


def _loop(selector, events, control, store, clock, hypr, directory, control_path, changes):
    # Subscribe before querying initial state so startup cannot miss a focus event.
    events.settimeout(policy()["ipc_timeout_seconds"])
    events.connect(str(hypr / ".socket2.sock"))
    initial = snapshot(hypr, policy()["ipc_timeout_seconds"])
    control_path.unlink(missing_ok=True)
    control.bind(str(control_path))
    control.listen()
    control.setblocking(False)
    events.setblocking(False)
    def write(span):
        store.write(span)
        changes.mark()

    collector = Collector(clock, write,
                          Context(app_class=initial[0].get("class", ""),
                                  workspace=initial[1], **flags(directory)))
    store.resume(collector.start)
    changes.mark()
    collector.snapshot(*initial)
    sleep = SleepDetector()
    changes.flush()
    wake_read, wake_write = socket.socketpair()
    wake_read.setblocking(False)
    wake_write.setblocking(False)
    previous_fd = signal.set_wakeup_fd(wake_write.fileno())
    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, lambda *_: None)
    selector.register(events, selectors.EVENT_READ, "hypr")
    selector.register(control, selectors.EVENT_READ, "control")
    selector.register(wake_read, selectors.EVENT_READ, "stop")
    framing = Lines()
    last_event = time.monotonic()
    try:
        running = True
        while running:
            settings = policy()
            safety = settings["safety_seconds"]
            quiet = safety - (time.monotonic() - last_event)
            # Events that change nothing extend the open span, so a busy terminal
            # can keep one span open for hours. Commit it at least every safety
            # interval, so the live view never mistakes a long stay for a stopped
            # collector and a crash loses at most that much.
            due = safety - (collector.clock() - collector.start) / SECOND
            ready = selector.select(max(0, min(quiet, due)))
            sleep.reconcile(collector)
            if not ready and quiet <= due:
                # Only a quiet interval activates the missed-event net.
                collector.snapshot(*snapshot(hypr, settings["ipc_timeout_seconds"]))
                collector.boundary(**flags(directory))
                collector.checkpoint()
                last_event = time.monotonic()
            for key, _ in ready:
                if key.data == "stop":
                    running = False
                    break
                if key.data == "hypr":
                    data = events.recv(65536)
                    if not data:
                        collector.boundary(connected=False, app_class="")
                        raise ConnectionError("Hyprland event socket disconnected")
                    for line in framing.feed(data):
                        if collector.hyprland(line):
                            last_event = time.monotonic()
                elif key.data == "control":
                    with control.accept()[0] as client:
                        client.settimeout(settings["ipc_timeout_seconds"])
                        try:
                            raw = bytearray()
                            while b"\n" not in raw and len(raw) < 32:
                                chunk = client.recv(32)
                                if not chunk:
                                    break
                                raw.extend(chunk)
                            event = raw.decode("ascii").strip()
                            if event not in HOOKS:
                                continue
                            flag, value = HOOKS[event]
                            collector.boundary(**{flag: value})
                            last_event = time.monotonic()
                            client.sendall(b"ok\n")
                        except (OSError, UnicodeError):
                            # A broken hook client must not kill collection.
                            continue
            if collector.clock() - collector.start >= safety * SECOND:
                collector.checkpoint()
            # One notification per batch of events, after its spans are committed.
            changes.flush()
    finally:
        try:
            sleep.reconcile(collector)
            collector.checkpoint()
            changes.flush()
        finally:
            signal.set_wakeup_fd(previous_fd)
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            wake_read.close()
            wake_write.close()
            control_path.unlink(missing_ok=True)
