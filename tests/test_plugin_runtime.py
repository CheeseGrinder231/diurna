"""Collector ownership uses synthetic subprocesses and locks, never a desktop."""
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from diurna import runtime


# Report before the kernel lock attempt, then substitute only desktop work.
# A held exclusive lock is the synchronization barrier; no sleeps or timing
# assertions are needed to prove that a waiting collector cannot open Store.
CHILD = """
import fcntl
from pathlib import Path
import sys
from diurna import runtime
from diurna.__main__ import main
from diurna.store import Store

flock = fcntl.flock
def announce(lock, flags):
    print("locking", flush=True)
    flock(lock, flags)
runtime.fcntl.flock = announce

def owned(database):
    with database.with_name(database.name + ".lock").open("a") as other:
        try:
            flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("exclusive", flush=True)
        else:
            raise AssertionError("collector entered without an exclusive lock")
    Store(database).close()
    print("entered", flush=True)
runtime._serve = owned
sys.argv = ["diurna", "collect", "--database", sys.argv[1], *sys.argv[2:]]
sys.exit(main())
"""


class PluginCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="diurna plugin ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "synthetic spans.sqlite3"
        self.lock = self.database.with_name(self.database.name + ".lock").open("a")
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def start(self, wait=True):
        root = Path(__file__).resolve().parents[1]
        arguments = ["--wait-for-lock"] if wait else []
        environment = dict(os.environ, XDG_RUNTIME_DIR=str(self.directory / "runtime"),
                           XDG_CONFIG_HOME=str(self.directory / "config"),
                           XDG_STATE_HOME=str(self.directory / "state"),
                           HYPRLAND_INSTANCE_SIGNATURE="synthetic")
        child = subprocess.Popen([sys.executable, "-B", "-c", CHILD,
                                  str(self.database), *arguments],
                                 cwd=root, env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline(), "locking\n")
        return child

    def test_waiter_opens_store_only_after_previous_owner_releases_lock(self):
        child = self.start()
        self.assertFalse(self.database.exists())
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        stdout, stderr = child.communicate()
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout, "exclusive\nentered\n")
        self.assertTrue(self.database.is_file())
        # Process exit releases ownership for the next collector.
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_sigterm_stops_a_waiter_without_opening_store(self):
        child = self.start()
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate()
        self.assertEqual(child.returncode, -signal.SIGTERM, stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(self.database.exists())

    def test_default_collection_still_refuses_a_second_owner(self):
        child = self.start(wait=False)
        stdout, stderr = child.communicate()
        self.assertEqual(child.returncode, 1, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("collection/hook failed", stderr)
        self.assertFalse(self.database.exists())


class PluginSessionOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="diurna-probe-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "default.sqlite3"
        self.control = self.directory / "diurna/synthetic/control.sock"
        self.control.parent.mkdir(parents=True)
        environment = patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(self.directory),
            "HYPRLAND_INSTANCE_SIGNATURE": "synthetic"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        settings = patch.object(runtime, "policy", return_value={"ipc_timeout_seconds": 3})
        settings.start()
        self.addCleanup(settings.stop)

    def server(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(self.control))
        server.listen()
        return server

    def test_live_custom_database_collector_is_preserved_without_a_hook(self):
        server = self.server()
        before = self.control.stat().st_ino
        with patch.object(runtime, "_serve") as collect:
            runtime.serve(self.database, wait_for_lock=True)
        collect.assert_not_called()
        self.assertFalse(self.database.exists())
        self.assertEqual(self.control.stat().st_ino, before)
        with server.accept()[0] as connection:
            self.assertEqual(connection.recv(1), b"")  # No signal was sent.
        # Deferral releases the database lock for the next attempt.
        with self.database.with_name(self.database.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_stale_and_missing_control_sockets_allow_collection(self):
        self.server().close()
        for state in ("stale", "missing"):
            with self.subTest(state=state):
                with patch.object(runtime, "_serve") as collect:
                    runtime.serve(self.database, wait_for_lock=True)
                collect.assert_called_once_with(self.database)
                self.control.unlink(missing_ok=True)

    def test_discovery_fallback_does_not_replace_another_session_socket(self):
        server = self.server()
        for signature in ("", "stale-signature"):
            with self.subTest(signature=signature), \
                    patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": signature}), \
                    patch.object(runtime, "_serve") as collect:
                runtime.serve(self.database, wait_for_lock=True)
                collect.assert_not_called()
                with server.accept()[0] as connection:
                    self.assertEqual(connection.recv(1), b"")

    def test_unexpected_probe_errors_fail_without_entering_collection(self):
        with patch.object(runtime.socket.socket, "connect", side_effect=PermissionError), \
                patch.object(runtime, "_serve") as collect:
            with self.assertRaises(PermissionError):
                runtime.serve(self.database, wait_for_lock=True)
        collect.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_default_collection_does_not_change_legacy_socket_behavior(self):
        with patch.object(runtime, "session_collector_running") as probe, \
                patch.object(runtime, "_serve") as collect:
            runtime.serve(self.database)
        probe.assert_not_called()
        collect.assert_called_once_with(self.database)
