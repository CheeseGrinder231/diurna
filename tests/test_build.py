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
