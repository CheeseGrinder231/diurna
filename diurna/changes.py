"""Tell bar readers that stored spans changed: a stamp file plus a Waybar signal.

No sockets or network: the stamp wakes inotify watchers and Waybar re-runs its
`signal`-tagged custom module on SIGRTMIN+N. A failed notification never stops
collection.
"""
import os
from pathlib import Path
import signal

# Waybar custom-module "signal" number; `diurna install --waybar` writes the same.
WAYBAR_SIGNAL = 11


def waybar_pids(proc=Path("/proc"), uid=None):
    uid = os.getuid() if uid is None else uid
    try:
        entries = list(os.scandir(proc))
    except OSError:
        return []
    pids = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if (Path(entry.path) / "comm").read_text().strip() == "waybar":
                pids.append(int(entry.name))
        except OSError:
            continue
    return sorted(pids)


class Changes:
    """Coalesce span writes; `flush` once per event batch, not per span."""

    def __init__(self, directory, proc=Path("/proc"), kill=os.kill):
        self.stamp = directory / "changed"
        self.proc, self.kill = proc, kill
        self.dirty = False
        self.count = 0

    def mark(self):
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        self.dirty = False
        self.count += 1
        try:
            # In-place write: inotify sees IN_CLOSE_WRITE, file watchers keep their watch.
            with open(self.stamp, "w") as stream:
                stream.write(f"{self.count}\n")
        except OSError:
            pass
        for pid in waybar_pids(self.proc):
            try:
                self.kill(pid, signal.SIGRTMIN + WAYBAR_SIGNAL)
            except OSError:
                continue
