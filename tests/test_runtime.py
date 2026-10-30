from contextlib import ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from diurna import ipc, runtime
from diurna import config
from diurna.changes import Changes
from diurna.store import Store
from diurna.sleep import SleepDetector


class ProtocolTests(unittest.TestCase):
    def test_framing_handles_fragmented_and_batched_events(self):
        framing = ipc.Lines()
        self.assertEqual(framing.feed(b"activewin"), [])
        self.assertEqual(framing.feed(b"dow>>foot,title\nworkspace>>2\nopen"),
                         ["activewindow>>foot,title", "workspace>>2"])
        self.assertEqual(framing.feed(b"window>>aa,2,foot,title\n"), ["openwindow>>aa,2,foot,title"])
        with self.assertRaises(ValueError):
            framing.feed(b"x" * (ipc.MAX_MESSAGE + 1))

    def test_real_unix_request_protocol_and_title_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            # Scratch can exceed sockaddr_un's path bound. Linux proc-fd paths
            # address the same owned directory without writing outside TMPDIR.
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd)
            directory = Path(f"/proc/self/fd/{fd}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(directory / ".socket.sock"))
                server.listen()
                errors = []

                def respond():
                    try:
                        for expected, data in ((b"j/activewindow", {"class": "foot", "address": "0xaa", "title": "PRIVATE"}),
                                               (b"j/activeworkspace", {"name": "1"}),
                                               (b"j/clients", [{"class": "foot", "address": "0xaa", "title": "PRIVATE"}])):
                            with server.accept()[0] as client:
                                self.assertEqual(client.recv(128), expected)
                                client.sendall(json.dumps(data).encode())
                    except BaseException as error:
                        errors.append(error)
                worker = threading.Thread(target=respond)
                worker.start()
                try:
                    result = ipc.snapshot(directory, 3)
                    self.assertEqual(result, ({"class": "foot", "address": "0xaa"}, "1", [{"class": "foot", "address": "0xaa"}]))
                finally:
                    worker.join()
                self.assertEqual(errors, [])

    def test_hook_persists_even_when_daemon_is_down(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(runtime, "paths", return_value=(directory, directory)), \
                    patch.object(runtime, "policy", return_value={"ipc_timeout_seconds": 1}):
                with self.assertRaises(OSError):
                    runtime.notify("lock")
                self.assertEqual(runtime.flags(directory), {"idle": False, "locked": True, "suspended": False,
                                                          "input": None})

    def test_hook_acknowledgement_and_state_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd)
            directory = Path(f"/proc/self/fd/{fd}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(directory / "control.sock"))
                server.listen()
                messages = []

                def respond():
                    with server.accept()[0] as client:
                        messages.append(client.recv(32))
                        client.sendall(b"ok\n")
                worker = threading.Thread(target=respond)
                worker.start()
                try:
                    with patch.object(runtime, "paths", return_value=(directory, directory)), \
                            patch.object(runtime, "policy", return_value={"ipc_timeout_seconds": 3}):
                        runtime.notify("resume")
                finally:
                    worker.join()
                self.assertEqual(messages, [b"resume\n"])
                self.assertFalse(runtime.flags(directory)["suspended"])


class RuntimeTests(unittest.TestCase):
    def test_event_loop_focus_hook_net_and_shutdown_use_fake_time(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            (directory / "locked").write_text("1")
            store = Store(directory / "db")
            stack.callback(store.close)
            clock = SimpleNamespace(now=0)
            steps = iter([(10, "hypr"), (20, "control"), (30, None), (40, "stop")])

            def select(_):
                clock.now, kind = next(steps)
                return [(SimpleNamespace(data=kind), 1)] if kind else []

            events, control, client = Mock(), Mock(), Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.recv.return_value = b"lock\n"
            control.accept.return_value = (client, "")
            events.recv.return_value = b"activewindow>>firefox,PRIVATE\n"
            selector = Mock()
            # The empty step is the missed-event net: it reconciles and checkpoints.
            selector.select.side_effect = select
            stack.enter_context(patch.object(runtime, "flags", side_effect=[
                {"idle": False, "locked": False, "suspended": False},
                {"idle": False, "locked": True, "suspended": False}]))
            stack.enter_context(patch.object(runtime, "snapshot", side_effect=[
                ({"class": "foot"}, "1", []), ({"class": "firefox"}, "2", [])]))
            stack.enter_context(patch.object(runtime, "policy", return_value={"safety_seconds": 60, "ipc_timeout_seconds": 3}))
            stack.enter_context(patch.object(runtime.time, "monotonic", return_value=0))
            stack.enter_context(patch.object(runtime, "SleepDetector"))
            stack.enter_context(patch.object(runtime.signal, "set_wakeup_fd", return_value=-1))
            stack.enter_context(patch.object(runtime.signal, "signal"))
            changes = Changes(directory, proc=directory / "proc", kill=Mock())
            runtime._loop(selector, events, control, store, lambda: clock.now, directory,
                          directory, directory / "control.sock", changes)
            self.assertEqual(store.db.execute("SELECT start_ns,end_ns,app_class,workspace,reason FROM spans").fetchall(),
                             [(0, 10, "foot", "1", "active"),
                              (10, 20, "firefox", "1", "active"),
                              (20, 30, "firefox", "1", "locked"),
                              (30, 40, "firefox", "2", "locked")])
            client.sendall.assert_called_once_with(b"ok\n")
            # Startup, focus, hook, net and shutdown batches each notify once.
            self.assertEqual((directory / "changed").read_text(), "5\n")

    def test_no_op_events_extend_the_span_and_checkpoint_each_safety_interval(self):
        second = 1_000_000_000
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            store = Store(directory / "db")
            stack.callback(store.close)
            clock = SimpleNamespace(now=0)
            waits = []

            def select(timeout):
                # A terminal retitling itself every second for 150 s, then stop.
                waits.append(timeout)
                clock.now += second
                return [(SimpleNamespace(data="stop" if clock.now > 150 * second
                                         else "hypr"), 1)]

            events = Mock()
            events.recv.return_value = b"activewindow>>foot,PRIVATE\nactivewindowv2>>aa\n"
            selector = Mock()
            selector.select.side_effect = select
            snapshots = stack.enter_context(patch.object(runtime, "snapshot", return_value=(
                {"class": "foot", "address": "0xaa"}, "1", [{"class": "foot", "address": "0xaa"}])))
            stack.enter_context(patch.object(runtime, "flags", return_value={
                "idle": False, "locked": False, "suspended": False}))
            stack.enter_context(patch.object(runtime, "policy", return_value={"safety_seconds": 60, "ipc_timeout_seconds": 3}))
            stack.enter_context(patch.object(runtime.time, "monotonic",
                                             side_effect=lambda: clock.now / second))
            stack.enter_context(patch.object(runtime, "SleepDetector"))
            stack.enter_context(patch.object(runtime.signal, "set_wakeup_fd", return_value=-1))
            stack.enter_context(patch.object(runtime.signal, "signal"))
            runtime._loop(selector, events, Mock(), store, lambda: clock.now, directory,
                          directory, directory / "control.sock",
                          Changes(directory, proc=directory / "proc", kill=Mock()))
            self.assertEqual(store.db.execute("SELECT start_ns,end_ns,app_class,workspace,reason FROM spans").fetchall(),
                             [(0, 60 * second, "foot", "1", "active"),
                              (60 * second, 120 * second, "foot", "1", "active"),
                              (120 * second, 151 * second, "foot", "1", "active")])
            # Events kept arriving, so the missed-event net never ran a snapshot.
            self.assertEqual(snapshots.call_count, 1)
            self.assertEqual((waits[0], waits[59], waits[60]), (60, 1, 60))
            # Startup, two checkpoints and shutdown notify; no-op events do not.
            self.assertEqual((directory / "changed").read_text(), "4\n")

    def test_shell_signals_and_suspend_gap_reach_stored_spans(self):
        second = 1_000_000_000
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            store = Store(directory / "db")
            stack.callback(store.close)
            clock = SimpleNamespace(now=0, gap=0)
            detector = SleepDetector(lambda: (clock.gap * second, 0))
            events, control, client = Mock(), Mock(), Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.recv.side_effect = [b"idle\n", b"lock\n", b"unlock\n", b"active\n"]
            control.accept.return_value = (client, "")
            steps = iter([(10, "control"), (20, "control"), (140, None),
                          (150, "control"), (160, "control"), (170, "stop")])

            def select(_):
                now, kind = next(steps)
                clock.now = now * second
                if now >= 140:
                    clock.gap = 120
                return [(SimpleNamespace(data=kind), 1)] if kind else []

            selector = Mock()
            selector.select.side_effect = select
            stack.enter_context(patch.object(runtime, "SleepDetector", return_value=detector))
            stack.enter_context(patch.object(runtime, "policy", return_value={"safety_seconds": 60, "ipc_timeout_seconds": 3}))
            stack.enter_context(patch.object(runtime, "snapshot", return_value=({"class": "foot"}, "1", [])))
            stack.enter_context(patch.object(runtime, "flags", side_effect=[
                {"idle": False, "locked": False, "suspended": False},
                {"idle": True, "locked": True, "suspended": False}]))
            stack.enter_context(patch.object(runtime.time, "monotonic", return_value=0))
            stack.enter_context(patch.object(runtime.signal, "set_wakeup_fd", return_value=-1))
            stack.enter_context(patch.object(runtime.signal, "signal"))
            runtime._loop(selector, events, control, store, lambda: clock.now,
                          directory, directory, directory / "control.sock",
                          Changes(directory, proc=directory / "proc", kill=Mock()))
            rows = store.db.execute("SELECT start_ns,end_ns,reason FROM spans").fetchall()
            self.assertEqual(rows, [(a * second, b * second, reason) for a, b, reason in
                                   [(0, 10, "active"), (10, 20, "idle"), (20, 140, "suspended"),
                                    (140, 150, "locked"), (150, 160, "idle"), (160, 170, "active")]])
            self.assertEqual(client.sendall.call_count, 4)

    def test_boottime_clock_includes_sleep_and_ignores_wall_clock_steps(self):
        with patch.object(runtime.time, "clock_gettime_ns", side_effect=[100, 110, 1000]), \
                patch.object(runtime.time, "time_ns", return_value=10000) as epoch:
            clock = runtime.Clock()
            self.assertEqual(clock(), 10010)
            self.assertEqual(clock(), 10900)
            epoch.assert_called_once()

    def test_config_precedence_validation_and_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "diurna"
            directory.mkdir()
            path = directory / "config.json"
            path.write_text('{"safety_seconds": 15}')
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": temporary,
                                           "DIURNA_SAFETY_SECONDS": "20"}, clear=True):
                self.assertEqual(config.policy()["safety_seconds"], 15)
                path.write_text('{"safety_seconds": -1}')
                self.assertEqual(config.policy()["safety_seconds"], 20)
                path.write_text('{"safety_seconds": 5, "safety_seconds": 10}')
                self.assertEqual(config.policy()["safety_seconds"], 20)
                with patch.dict("os.environ", {"DIURNA_SAFETY_SECONDS": "NaN"}):
                    self.assertEqual(config.policy()["safety_seconds"], 60)
