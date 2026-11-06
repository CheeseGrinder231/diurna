import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from diurna import install, shell_plugin

ROOT = Path(__file__).resolve().parents[1]


class MarketplaceTests(unittest.TestCase):
    def test_manifest_is_loadable_without_build_and_preserves_identity(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["id"], shell_plugin.PLUGIN_ID)
        self.assertEqual(manifest["name"], "Diurna - Omarchy Screen Time")
        self.assertEqual(set(manifest["kinds"]), {"service", "bar-widget"})
        for entry in manifest["entryPoints"].values():
            path = (ROOT / entry).resolve()
            self.assertTrue(path.is_relative_to(ROOT))
            self.assertTrue(path.is_file())
            self.assertNotIn("@PROGRAM@", path.read_text())
        self.assertFalse(manifest["barWidget"]["allowMultiple"])
        self.assertEqual(manifest["barWidget"]["defaultSection"], "right")

    def test_activity_component_stays_in_sync_with_legacy_behavior(self):
        subprocess.run([sys.executable, str(ROOT / "scripts/build_plugin.py"), "--check"],
                       check=True, capture_output=True)

    def test_source_launcher_works_outside_checkout_and_reads_settings_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="diurna plugin ") as temporary:
            directory = Path(temporary)
            config = directory / "config"
            (config / "diurna").mkdir(parents=True)
            (directory / ".config/omarchy").mkdir(parents=True)
            shell = directory / ".config/omarchy/shell.json"
            shell.write_text('{"idle":{"screensaver":90,"lock":300}}\n')
            settings = config / "diurna/config.json"
            settings.write_text('{"hands_on_seconds":45}\n')
            originals = {p: p.read_bytes() for p in (shell, settings)}
            environment = dict(os.environ, HOME=str(directory), XDG_CONFIG_HOME=str(config),
                               XDG_STATE_HOME=str(directory / "state"))
            environment.pop("PYTHONPATH", None)
            for command in (["--help"], ["plugin-settings"]):
                result = subprocess.run([sys.executable, "-B", str(ROOT / "plugin.py"), *command],
                                        cwd=directory, env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if command == ["plugin-settings"]:
                    self.assertEqual(json.loads(result.stdout),
                                     {"idleSeconds": 90, "handsOnSeconds": 45})
            self.assertFalse((directory / "state").exists())
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_bad_shell_settings_fall_back_without_overwrite(self):
        spec = importlib.util.spec_from_file_location("plugin_runner", ROOT / "plugin.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "shell.json"
            from unittest.mock import patch
            for text in ("{broken", "x" * 65537,
                         '{"idle":{"screensaver":1e99,"lock":1e99}}'):
                config.write_text(text)
                with patch.object(runner, "shell_config", return_value=config), \
                        patch.object(runner, "hands_on_seconds", return_value=60):
                    self.assertEqual(runner.settings(), {"idleSeconds": 150, "handsOnSeconds": 60})
                self.assertEqual(config.read_text(), text)

    def test_legacy_installer_refuses_and_uninstaller_preserves_repository_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory / "shell.json"
            original = '{"plugins":[],"bar":{"layout":{"right":[]}}}\n'
            config.write_text(original)
            assets = shell_plugin.directory(config)
            assets.mkdir(parents=True)
            (assets / "manifest.json").write_bytes((ROOT / "manifest.json").read_bytes())
            (assets / "user-file").write_text("keep")
            before = {p: p.read_bytes() for p in assets.iterdir()}
            with self.assertRaises(install.InstallError):
                install.install("omarchy-shell", config, "/synthetic/diurna")
            self.assertEqual(config.read_text(), original)
            self.assertFalse(config.with_name("shell.json.diurna-backup").exists())
            message = install.uninstall("omarchy-shell", config)
            self.assertIn("unrecognized plugin assets kept", message)
            self.assertEqual({p: p.read_bytes() for p in assets.iterdir()}, before)
