import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ZipappTests(unittest.TestCase):
    def test_built_launcher_propagates_collection_failure(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = directory / "diurna.pyz"
            subprocess.run([sys.executable, str(root / "scripts/build.py"),
                            "--output", str(artifact)], check=True, capture_output=True)
            environment = dict(os.environ)
            # Missing session is now a wait, but invalid configuration must
            # still propagate a real collection failure through the zipapp.
            environment["HYPRLAND_INSTANCE_SIGNATURE"] = "../invalid"
            environment["XDG_RUNTIME_DIR"] = str(directory)
            environment["XDG_STATE_HOME"] = str(directory)
            result = subprocess.run([sys.executable, str(artifact), "collect",
                                     "--database", str(directory / "spans.sqlite3")],
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("collection/hook failed", result.stderr)
            help_result = subprocess.run([sys.executable, str(artifact), "--help"],
                                         env=environment, capture_output=True, text=True)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            config = directory / "shell.json"
            original = b'{"bar":{"layout":{"right":[]}},"plugins":[]}\r\n'
            config.write_bytes(original)
            for command in ("install", "uninstall"):
                result = subprocess.run([sys.executable, str(artifact), command,
                                         "--omarchy-shell", "--config", str(config)],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if command == "install":
                    plugin = directory / "plugins/diurna.activity/Service.qml"
                    self.assertTrue(plugin.is_file())
                    self.assertNotIn("@PROGRAM@", plugin.read_text())
            self.assertEqual(config.read_bytes(), original)

    def test_source_and_zipapp_raw_export_match_from_unrelated_directory(self):
        from diurna.collector import Span
        from diurna.store import Store

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = directory / "diurna.pyz"
            path = directory / "synthetic.db"
            archive = Store(path)
            archive.write(Span(-9007199254740993, 9007199254740993,
                               "終端", "二", "active", False, True, None))
            archive.close()
            subprocess.run([sys.executable, str(root / "scripts/build.py"),
                            "--output", str(artifact)], check=True, capture_output=True)
            arguments = ["export", "--database", str(path), "--start-ns", "-1", "--end-ns", "1"]
            environment = dict(os.environ, PYTHONPATH=str(root),
                               XDG_CONFIG_HOME=str(directory / "config"),
                               XDG_STATE_HOME=str(directory / "state"))
            environment.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
            source = subprocess.run([sys.executable, "-m", "diurna", *arguments],
                                    cwd=directory, env=environment, capture_output=True)
            environment.pop("PYTHONPATH", None)
            built = subprocess.run([sys.executable, str(artifact), *arguments],
                                   cwd=directory, env=environment, capture_output=True)
            self.assertEqual(source.returncode, 0, source.stderr)
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(source.stdout, built.stdout)
            self.assertIn(b'"id":"span:-9007199254740993"', built.stdout)
            self.assertTrue(built.stdout.endswith(
                b'{"format":"diurna.raw-spans","version":1,"type":"complete","rows":1}\n'))

    def test_standalone_finished_export_status_and_catchup(self):
        from diurna.collector import Span
        from diurna.store import Store

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = directory / "diurna.pyz"
            path = directory / "finished.db"
            archive = Store(path)
            archive.write(Span(1, 200_000_000_001, "終端", "private", "active", True))
            archive.write(Span(200_000_000_001, 200_000_000_002, "", "", "unknown"))
            archive.close()
            subprocess.run([sys.executable, str(root / "scripts/build.py"),
                            "--output", str(artifact)], check=True, capture_output=True)
            environment = dict(os.environ, PYTHONPATH=str(root),
                               XDG_CONFIG_HOME=str(directory / "config"),
                               XDG_STATE_HOME=str(directory / "state"))
            environment.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
            arguments = ["periods", "--database", str(path), "--start-ns", "2", "--end-ns", "3"]
            source = subprocess.run([sys.executable, "-m", "diurna", *arguments],
                                    cwd=directory, env=environment, capture_output=True)
            environment.pop("PYTHONPATH", None)
            built = subprocess.run([sys.executable, str(artifact), *arguments],
                                   cwd=directory, env=environment, capture_output=True)
            self.assertEqual(source.returncode, 0, source.stderr)
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(source.stdout, built.stdout)
            self.assertIn(b'"id":"screen-use:1"', built.stdout)
            self.assertNotIn(b'private', built.stdout)
            self.assertTrue(built.stdout.endswith(b'{"rows":1,"type":"complete"}\n'))
            for command in ("publish", "publication-status"):
                result = subprocess.run([sys.executable, str(artifact), command,
                                         "--database", str(path)], cwd=directory,
                                        env=environment, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b'"through_sequence": 1', result.stdout)
            help_result = subprocess.run([sys.executable, str(artifact), "--help"],
                                         cwd=directory, env=environment, capture_output=True)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn(b'periods', help_result.stdout)
