"""Login races use synthetic Unix sockets and a fake clock, never the desktop."""
from contextlib import ExitStack
import configparser
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from diurna import runtime
from diurna.changes import Changes
from diurna.store import Store

SECOND = 1_000_000_000


class StartupTests(unittest.TestCase):
    def setUp(self):
        settings = patch.object(runtime, "policy", return_value={
            "safety_seconds": 60, "ipc_timeout_seconds": 3})
        settings.start()
        self.addCleanup(settings.stop)

    def test_discovery_skips_dead_sockets_and_retries_a_partial_startup(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {}, clear=True):
            directory = Path(temporary)
            for name in ("dead", "live"):
                path = directory / "hypr" / name
                path.mkdir(parents=True)
                (path / ".socket2.sock").touch()
            dead, partial, dead_again, live = Mock(), Mock(), Mock(), Mock()
            dead.connect.side_effect = ConnectionRefusedError()
            dead_again.connect.side_effect = ConnectionRefusedError()
            initial = ({"class": "foot"}, "1", [])
            with patch.object(runtime.socket, "socket", side_effect=[dead, partial, dead_again, live]), \
                    patch.object(runtime, "snapshot", side_effect=[FileNotFoundError(), initial]):
                self.assertIsNone(runtime.session(directory))
                dead.close.assert_called_once()
                partial.close.assert_called_once()
                events, hypr, result = runtime.session(directory)
                self.assertIs(events, live)
                self.assertEqual((hypr.name, result), ("live", initial))
                live.close.assert_not_called()

    def test_multiple_live_sessions_wait_unless_explicit_signature_selects_one(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {}, clear=True):
            directory = Path(temporary)
            for name in ("one", "two"):
                path = directory / "hypr" / name
                path.mkdir(parents=True)
                (path / ".socket2.sock").touch()
            one, two, preferred = Mock(), Mock(), Mock()
            with patch.object(runtime.socket, "socket", side_effect=[one, two, preferred]), \
                    patch.object(runtime, "snapshot", return_value=({}, "", [])):
                self.assertIsNone(runtime.session(directory))
                one.close.assert_called_once()
                two.close.assert_called_once()
                with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "two"}):
                    events, hypr, _ = runtime.session(directory)
                self.assertIs(events, preferred)
                self.assertEqual(hypr.name, "two")
                preferred.close.assert_not_called()

    def test_session_appears_after_35_seconds_without_environment_import_or_restart(self):
        for signature in (None, "stale-signature"):
            with self.subTest(signature=signature), tempfile.TemporaryDirectory() as temporary, \
                    ExitStack() as stack:
                fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
                stack.callback(os.close, fd)
                directory = Path(f"/proc/self/fd/{fd}")
                environment = {"XDG_RUNTIME_DIR": str(directory)}
                if signature:
                    environment["HYPRLAND_INSTANCE_SIGNATURE"] = signature
                stack.enter_context(patch.dict(os.environ, environment, clear=True))
                database = directory / "spans.sqlite3"
                clock = SimpleNamespace(now=0)
                selector = Mock()
                selector.__enter__ = Mock(return_value=selector)
                selector.__exit__ = Mock(return_value=False)
                registered = set()
                selector.register.side_effect = lambda _, __, kind: registered.add(kind)
                waits, errors, workers = [], [], []
                servers = {}
                hypr = directory / "hypr/live-session"

                def appear():
                    hypr.mkdir(parents=True)
                    for name in (".socket2.sock", ".socket.sock"):
                        server = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                        server.bind(str(hypr / name))
                        server.listen()
                        servers[name] = server

                    def respond():
                        try:
                            for command, data in ((b"j/activewindow", {"class": "foot", "title": "PRIVATE"}),
                                                  (b"j/activeworkspace", {"name": "1"}),
                                                  (b"j/clients", [])):
                                with servers[".socket.sock"].accept()[0] as client:
                                    self.assertEqual(client.recv(128), command)
                                    client.sendall(json.dumps(data).encode())
                        except BaseException as error:
                            errors.append(error)
                    worker = threading.Thread(target=respond)
                    worker.start()
                    workers.append(worker)

                def select(timeout):
                    if "hypr" not in registered:
                        waits.append(timeout)
                        clock.now += int(timeout * SECOND)
                        if clock.now >= 35 * SECOND:
                            appear()
                        return []
                    clock.now += 5 * SECOND
                    if "focus" not in registered:
                        registered.add("focus")
                        with servers[".socket2.sock"].accept()[0] as client:
                            client.sendall(b"activewindow>>firefox,PRIVATE\n")
                        return [(SimpleNamespace(data="hypr"), 1)]
                    return [(SimpleNamespace(data="stop"), 1)]

                selector.select.side_effect = select
                stack.enter_context(patch.object(runtime.selectors, "DefaultSelector", return_value=selector))
                stack.enter_context(patch.object(runtime, "Clock", return_value=lambda: clock.now))
                stack.enter_context(patch.object(runtime, "SleepDetector"))
                stack.enter_context(patch.object(runtime.time, "monotonic", side_effect=lambda: clock.now / SECOND))
                stack.enter_context(patch.object(runtime, "Changes", return_value=Changes(
                    directory / "diurna", proc=directory / "no-proc", kill=Mock())))
                runtime.serve(database)
                for worker in workers:
                    worker.join()
                self.assertEqual(errors, [])
                self.assertEqual(os.environ, environment)  # no session import into this process
                store = Store(database)
                stack.callback(store.close)
                rows = store.db.execute(
                    "SELECT start_ns,end_ns,app_class,reason,title FROM spans ORDER BY start_ns").fetchall()
                connected_at = sum(int(t * SECOND) for t in waits)
                self.assertGreaterEqual(connected_at, 35 * SECOND)
                self.assertEqual(waits[:5], [0.25, 0.5, 1, 2, 4])
                self.assertLessEqual(max(waits), 5)
                self.assertEqual(rows[0][0], 0)
                self.assertTrue(all(a[1] == b[0] for a, b in zip(rows, rows[1:])))
                self.assertTrue(all(row[2:] == ("", "unknown", None) for row in rows[:-2]))
                self.assertEqual(sum(b - a for a, b, _, reason, _ in rows if reason == "unknown"),
                                 connected_at)
                self.assertEqual(rows[-2:], [(connected_at, connected_at + 5 * SECOND, "foot", "active", None),
                                            (connected_at + 5 * SECOND, connected_at + 10 * SECOND,
                                             "firefox", "active", None)])
                self.assertNotIn(b"PRIVATE", database.read_bytes())
                self.assertFalse((directory / "diurna/live-session/control.sock").exists())

    def test_stop_while_waiting_commits_unknown_and_restores_signal_handlers(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary}, clear=True))
            clock = SimpleNamespace(now=0)
            selector = Mock()
            selector.__enter__ = Mock(return_value=selector)
            selector.__exit__ = Mock(return_value=False)

            def stop(_):
                # The database lock must already be held, even before Hyprland.
                with self.assertRaises(BlockingIOError):
                    runtime.serve(directory / "db")
                clock.now = 2 * SECOND
                # Exercise the actual signal-to-wakeup transport without waiting.
                os.kill(os.getpid(), signal.SIGTERM)
                wake_read = selector.register.call_args.args[0]
                self.assertEqual(wake_read.recv(1), bytes([signal.SIGTERM]))
                return [(SimpleNamespace(data="stop"), 1)]

            selector.select.side_effect = stop
            stack.enter_context(patch.object(runtime.selectors, "DefaultSelector", return_value=selector))
            stack.enter_context(patch.object(runtime, "Clock", return_value=lambda: clock.now))
            stack.enter_context(patch.object(runtime, "Changes", return_value=Changes(
                directory / "diurna", proc=directory / "no-proc", kill=Mock())))
            previous = signal.getsignal(signal.SIGTERM)
            runtime.serve(directory / "db")
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            store = Store(directory / "db")
            stack.callback(store.close)
            self.assertEqual(store.db.execute("SELECT start_ns,end_ns,reason FROM spans").fetchall(),
                             [(0, 2 * SECOND, "unknown")])

    def test_runtime_directory_falls_back_without_session_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime.os, "getuid", return_value=1234):
            self.assertEqual(runtime.runtime_directory(), Path("/run/user/1234"))
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "relative"}, clear=True):
            with self.assertRaises(ValueError):
                runtime.runtime_directory()

    def test_unit_has_no_start_rate_cutoff_and_delays_real_failure_restarts(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).resolve().parents[1] / "packaging/diurna.service")
        self.assertEqual(unit["Unit"]["StartLimitIntervalSec"], "0")
        self.assertEqual(unit["Service"]["RestartSec"], "5s")
        self.assertEqual(unit["Service"]["Restart"], "on-failure")
        self.assertEqual(unit["Install"]["WantedBy"], "graphical-session.target")
