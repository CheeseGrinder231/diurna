from contextlib import ExitStack
import os
from pathlib import Path
import selectors
import signal
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from diurna import bar, runtime, today
from diurna.changes import WAYBAR_SIGNAL, Changes, waybar_pids
from diurna.store import Store

SECOND = 1_000_000_000
MINUTE = 60 * SECOND


def short_path(test, directory):
    # Scratch can exceed sockaddr_un's path bound; /proc/self/fd addresses the same directory.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    test.addCleanup(os.close, fd)
    return Path(f"/proc/self/fd/{fd}")


def fake_proc(directory, processes):
    for pid, name in processes.items():
        (directory / str(pid)).mkdir(parents=True)
        (directory / str(pid) / "comm").write_text(name + "\n")
    (directory / "self").mkdir()
    return directory


class BarUpdateTests(unittest.TestCase):
    def test_focus_change_on_fake_event_socket_updates_the_module_at_event_time(self):
        """The fake clock reads 25 min at the focus event; the module refreshes with
        no further clock advance, inside the 2 s bound, with the new total."""
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = short_path(self, temporary)
            store = Store(directory / "spans.sqlite3")
            stack.callback(store.close)
            proc = fake_proc(Path(temporary) / "proc", {4242: "waybar", 4243: "firefox"})
            # Collector start and the startup snapshot read 0; the focus event reads 25 min.
            readings = iter([0, 0])
            now = {"ns": 0}

            def clock():
                now["ns"] = next(readings, 25 * MINUTE)
                return now["ns"]
            updates = []

            def waybar(pid, sig):
                # What Waybar's `exec` would print on this signal, read at signal time.
                summary = today.summarize(today.rows(directory / "spans.sqlite3", 0, now["ns"]),
                                          0, now["ns"])
                updates.append((now["ns"], pid, sig, bar.bar(summary)))

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as hyprland:
                hyprland.bind(str(directory / ".socket2.sock"))
                hyprland.listen()

                def compositor():
                    with hyprland.accept()[0] as client:
                        client.sendall(b"activewindow>>firefox,PRIVATE\nactivewindowv2>>bb\n")
                worker = threading.Thread(target=compositor)
                worker.start()
                stack.callback(worker.join)
                stack.enter_context(patch.object(runtime, "snapshot", return_value=(
                    {"class": "foot", "address": "0xaa"}, "1",
                    [{"class": "foot", "address": "0xaa"}])))
                stack.enter_context(patch.object(runtime, "policy", return_value={
                    "safety_seconds": 60, "ipc_timeout_seconds": 3}))
                stack.enter_context(patch.object(runtime.time, "monotonic", return_value=0))
                stack.enter_context(patch.object(runtime.signal, "set_wakeup_fd", return_value=-1))
                stack.enter_context(patch.object(runtime.signal, "signal"))
                changes = Changes(directory, proc=proc, kill=waybar)
                with selectors.DefaultSelector() as selector, \
                        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as events, \
                        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control, \
                        self.assertRaises(ConnectionError):
                    # The compositor closes after one focus change; the collector exits.
                    runtime._loop(selector, events, control, store, clock, directory,
                                  directory, directory / "control.sock", changes)
            focus = [u for u in updates if u[3]["text"] != "0m"]
            self.assertEqual(len(focus), 1)
            at, pid, sig, module = focus[0]
            self.assertEqual((pid, sig), (4242, signal.SIGRTMIN + WAYBAR_SIGNAL))
            self.assertLessEqual(at - 25 * MINUTE, 2 * SECOND)
            self.assertEqual(module["text"], "25m")
            self.assertTrue(module["tooltip"].startswith("Today 25m active\nfoot  25m"))
            self.assertTrue((directory / "changed").exists())
            self.assertNotIn(b"PRIVATE", (directory / "spans.sqlite3").read_bytes())


class ChangesTests(unittest.TestCase):
    def test_only_own_waybar_processes_are_signalled_and_failures_are_contained(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            proc = fake_proc(directory / "proc", {7: "waybar", 8: "waybar-helper", 9: "waybar"})
            self.assertEqual(waybar_pids(proc), [7, 9])
            self.assertEqual(waybar_pids(proc, uid=os.getuid() + 1), [])
            self.assertEqual(waybar_pids(directory / "absent"), [])
            calls = []

            def kill(pid, sig):
                calls.append(pid)
                raise ProcessLookupError()

            changes = Changes(directory / "missing-dir", proc=proc, kill=kill)
            changes.flush()
            self.assertEqual(calls, [])  # nothing changed, nothing sent
            changes.mark()
            changes.mark()
            changes.flush()  # stamp write and both kills fail without raising
            self.assertEqual(calls, [7, 9])
            changes.flush()
            self.assertEqual(calls, [7, 9])
