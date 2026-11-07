import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from diurna import install, shell_plugin
from diurna.changes import WAYBAR_SIGNAL

# Omarchy 3-style JSONC: comments, URL-like strings and a commented-out module.
WAYBAR = """// Waybar config
{
  "reload_style_on_change": true,
  "layer": "top", /* inline */
  "modules-left": ["custom/omarchy", "hyprland/workspaces"],
  "modules-center": ["clock"],
  "modules-right": [
    "group/tray-expander",
    // "custom/disabled",
    "network",
    "battery"
  ],
  "clock": { "format": "{:%A %H:%M}", "tooltip-format": "http://x//y {calendar}" },
  "network": { "format-wifi": "\\"{essid}\\"" }
}
"""
SHELL = """{
  "bar": {
    "layout": {
      "left": [
        {
          "id": "omarchy.menu"
        }
      ],
      "right": [
        {
          "id": "omarchy.network"
        },
        {
          "id": "omarchy.power"
        }
      ]
    },
    "position": "top"
  },
  "version": 1
}
"""
PROGRAM = "/home/someone/.local/bin/diurna"


def comments_stripped(text):
    # Good enough for these fixtures: no comment markers inside their strings
    # except the URL, which sits in a string the regex skips.
    return json.loads(re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                             lambda m: m.group(1) or "", text, flags=re.S))


class InstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text)
        path.chmod(0o640)
        return path

    def test_waybar_round_trip_is_byte_identical_and_idempotent(self):
        path = self.write("config.jsonc", WAYBAR)
        original = path.read_bytes()
        install.install("waybar", path, PROGRAM)
        installed = path.read_bytes()
        config = comments_stripped(installed.decode())
        self.assertEqual(config["modules-right"][0], "custom/diurna")
        module = config["custom/diurna"]
        self.assertEqual(module["exec"], PROGRAM + " bar")
        self.assertEqual(module["return-type"], "json")
        self.assertEqual(module["signal"], WAYBAR_SIGNAL)
        self.assertEqual(module["interval"], 60)
        self.assertIs(module["escape"], True)
        self.assertEqual(module["on-click"], "omarchy-launch-or-focus-tui --app-id=TUI.float "
                         + PROGRAM + " today --watch")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.directory / "config.jsonc.diurna-backup").read_bytes(), original)
        self.assertIn("already installed", install.install("waybar", path, PROGRAM))
        self.assertEqual(path.read_bytes(), installed)
        install.uninstall("waybar", path)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.directory / "config.jsonc.diurna-backup").exists())
        self.assertIn("not installed", install.uninstall("waybar", path))
        self.assertEqual(path.read_bytes(), original)

    def test_new_backups_are_private_for_both_targets_and_permissive_umasks(self):
        for target, original in (("waybar", WAYBAR), ("omarchy-shell", SHELL)):
            for mode in (0o600, 0o640, 0o644):
                for mask in (0o000, 0o022, 0o077):
                    with self.subTest(target=target, mode=oct(mode), umask=oct(mask)):
                        folder = self.directory / f"{target}-{mode:o}-{mask:o}"
                        folder.mkdir(mode=0o755)
                        path = folder / "config.json"
                        data = original.replace("\n", "\r\n").encode()
                        path.write_bytes(data)
                        path.chmod(mode)
                        backup = path.with_name(path.name + install.BACKUP_SUFFIX)
                        previous = os.umask(mask)
                        try:
                            install.install(target, path, PROGRAM)
                        finally:
                            os.umask(previous)
                        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(backup.read_bytes(), data)
                        self.assertEqual(path.stat().st_mode & 0o777, mode)
                        self.assertIn("already installed", install.install(target, path, PROGRAM))
                        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                        install.uninstall(target, path)
                        self.assertEqual(path.read_bytes(), data)
                        self.assertEqual(path.stat().st_mode & 0o777, mode)
                        self.assertFalse(backup.exists())

    def test_backup_is_private_before_publication_and_failure_leaves_config_unchanged(self):
        path = self.write("config.jsonc", WAYBAR)
        path.chmod(0o600)
        backup = path.with_name(path.name + install.BACKUP_SUFFIX)

        def fail_publication(source, destination):
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(source).read_bytes(), path.read_bytes())
            self.assertEqual(Path(destination), backup)
            self.assertFalse(backup.exists())
            raise OSError("synthetic publication failure")

        with patch.object(install.os, "link", side_effect=fail_publication) as publish:
            with self.assertRaisesRegex(OSError, "synthetic publication failure"):
                install.install("waybar", path, PROGRAM)
        publish.assert_called_once()
        self.assertEqual(path.read_text(), WAYBAR)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_backup_created_concurrently_is_not_overwritten(self):
        path = self.write("config.jsonc", WAYBAR)
        backup = path.with_name(path.name + install.BACKUP_SUFFIX)
        real_link = os.link

        def race_publication(source, destination):
            backup.write_text("keep this existing backup")
            return real_link(source, destination)

        with patch.object(install.os, "link", side_effect=race_publication):
            with self.assertRaises(FileExistsError):
                install.install("waybar", path, PROGRAM)
        self.assertEqual(backup.read_text(), "keep this existing backup")
        self.assertEqual(path.read_text(), WAYBAR)
        self.assertEqual(set(self.directory.iterdir()), {path, backup})

    def test_new_backup_refuses_a_dangling_symlink(self):
        path = self.write("config.jsonc", WAYBAR)
        backup = path.with_name(path.name + install.BACKUP_SUFFIX)
        other = self.directory / "not-the-backup"
        backup.symlink_to(other)
        with self.assertRaises(FileExistsError):
            install.install("waybar", path, PROGRAM)
        self.assertTrue(backup.is_symlink())
        self.assertFalse(other.exists())
        self.assertEqual(path.read_text(), WAYBAR)
        self.assertEqual(set(self.directory.iterdir()), {path, backup})

    def test_waybar_edits_after_install_survive_uninstall_and_keep_backup(self):
        path = self.write("config.jsonc", WAYBAR)
        install.install("waybar", path, PROGRAM)
        edited = path.read_text().replace('"battery"', '"battery", "cpu"')
        path.write_text(edited)
        message = install.uninstall("waybar", path)
        self.assertIn("backup kept", message)
        self.assertEqual(path.read_text(), WAYBAR.replace('"battery"', '"battery", "cpu"'))
        self.assertTrue((self.directory / "config.jsonc.diurna-backup").exists())
        # A later install refuses to overwrite a backup that differs from the config.
        with self.assertRaises(install.InstallError):
            install.install("waybar", path, PROGRAM)

    def test_waybar_module_moved_by_user_is_still_removed(self):
        path = self.write("config.jsonc", WAYBAR)
        install.install("waybar", path, PROGRAM)
        text = path.read_text().replace('"custom/diurna", "group/tray-expander"',
                                        '"group/tray-expander"')
        text = text.replace('"modules-center": ["clock"]', '"modules-center": ["clock", "custom/diurna"]')
        path.write_text(text)
        install.uninstall("waybar", path)
        config = comments_stripped(path.read_text())
        self.assertNotIn("custom/diurna", config)
        self.assertEqual(config["modules-center"], ["clock"])

    def test_waybar_multi_bar_symlink_and_refusals(self):
        bars = '[\n  {"modules-right": []},\n  {"modules-right": ["clock"]}\n]\n'
        target = self.write("real.jsonc", bars)
        link = self.directory / "config.jsonc"
        link.symlink_to(target)
        install.install("waybar", link, PROGRAM)
        self.assertTrue(link.is_symlink())
        installed = json.loads(target.read_text())
        self.assertEqual(installed[0]["modules-right"], ["custom/diurna"])
        self.assertNotIn("custom/diurna", installed[1])
        install.uninstall("waybar", link)
        self.assertEqual(target.read_text(), bars)
        for text in ('{"layer": "top"}', '{"modules-right": ["custom/diurna"]}', "{oops}",
                     '{"modules-right": [] /* unterminated'):
            with self.subTest(text=text):
                path = self.write("bad.jsonc", text)
                with self.assertRaises(install.InstallError):
                    install.install("waybar", path, PROGRAM)
                self.assertEqual(path.read_text(), text)
        with self.assertRaises(install.InstallError):
            install.install("waybar", self.directory / "absent.jsonc", PROGRAM)

    def test_omarchy_shell_round_trip_is_byte_identical(self):
        path = self.write("shell.json", SHELL)
        install.install("omarchy-shell", path, PROGRAM)
        config = json.loads(path.read_text())
        self.assertEqual(config["plugins"], [{"id": "diurna.activity"}])
        assets = shell_plugin.directory(path)
        self.assertEqual(json.loads((assets / "manifest.json").read_text())["entryPoints"],
                         {"service": "Service.qml"})
        source = (assets / "Service.qml").read_text()
        self.assertIn(json.dumps(PROGRAM), source)
        self.assertIn("timeout: 150", source)
        self.assertIn("timeout: 60\n        respectInhibitors: false", source)
        self.assertNotIn("@PROGRAM@", source)
        self.assertNotIn("@HANDS_ON@", source)
        right = config["bar"]["layout"]["right"]
        self.assertEqual(right[0], {
            "id": "diurna", "type": "command", "exec": PROGRAM + " bar", "interval": 60,
            "onClick": "omarchy-launch-or-focus-tui --app-id=TUI.float " + PROGRAM
                       + " today --watch", "tooltip": "Diurna"})
        self.assertEqual(right[1:], [{"id": "omarchy.network"}, {"id": "omarchy.power"}])
        installed = path.read_text()
        install.install("omarchy-shell", path, PROGRAM)
        self.assertEqual(path.read_text(), installed)
        install.uninstall("omarchy-shell", path)
        self.assertEqual(path.read_text(), SHELL)
        self.assertFalse(assets.exists())

    def test_hands_on_window_comes_from_config_at_install(self):
        path = self.write("shell.json", SHELL)
        with patch.object(install, "policy", return_value={"hands_on_seconds": 89.6}):
            install.install("omarchy-shell", path, PROGRAM)
        assets = shell_plugin.directory(path)
        self.assertEqual(json.loads((assets / "manifest.json").read_text())
                         ["diurnaHandsOnSeconds"], 90)
        self.assertIn("timeout: 90\n", (assets / "Service.qml").read_text())
        install.uninstall("omarchy-shell", path)
        self.assertEqual(path.read_text(), SHELL)
        self.assertFalse(assets.exists())

    def test_unchanged_plugin_from_an_earlier_version_uninstalls(self):
        # Diurna before the activity split shipped plugin 1.0.0 (idle only).
        path = self.write("shell.json", SHELL)
        install.install("omarchy-shell", path, PROGRAM)
        assets = shell_plugin.directory(path)
        for name, text in shell_plugin.payload(PROGRAM, 150).items():
            (assets / name).write_text(text)
        self.assertNotIn("diurnaHandsOnSeconds", (assets / "manifest.json").read_text())
        self.assertNotIn("Mpris", (assets / "Service.qml").read_text())
        message = install.uninstall("omarchy-shell", path)
        self.assertNotIn("kept", message)
        self.assertEqual(path.read_text(), SHELL)
        self.assertFalse(assets.exists())

    def test_omarchy_shell_rewritten_config_still_uninstalls(self):
        path = self.write("shell.json", SHELL)
        install.install("omarchy-shell", path, PROGRAM)
        # omarchy-shell reformats shell.json whenever the bar is rearranged.
        config = json.loads(path.read_text())
        right = config["bar"]["layout"]["right"]
        right.append(right.pop(0))
        path.write_text(json.dumps(config, indent=2) + "\n")
        install.uninstall("omarchy-shell", path)
        self.assertEqual(json.loads(path.read_text()), json.loads(SHELL))

    def test_shell_plugin_existing_array_comments_and_crlf_round_trip(self):
        for plugins in ('"plugins": [],', '"plugins": [{"id": "other"}],'):
            text = SHELL.replace('"version": 1', plugins + '\n  "version": 1')
            for original in (text.encode(), text.replace("\n", "\r\n").encode()):
                path = self.directory / "shell.json"
                path.write_bytes(original)
                install.install("omarchy-shell", path, PROGRAM)
                self.assertEqual(json.loads(path.read_text())["plugins"][0], {"id": "diurna.activity"})
                install.uninstall("omarchy-shell", path)
                self.assertEqual(path.read_bytes(), original)

    def test_shell_bar_only_install_upgrades_and_restores_original(self):
        path = self.write("shell.json", SHELL)
        path.with_name(path.name + install.BACKUP_SUFFIX).write_text(SHELL)
        path.write_text(install.shell_bar_add(SHELL, PROGRAM))
        install.install("omarchy-shell", path, PROGRAM)
        self.assertEqual(json.loads(path.read_text())["plugins"], [{"id": "diurna.activity"}])
        install.uninstall("omarchy-shell", path)
        self.assertEqual(path.read_text(), SHELL)

    def test_shell_plugin_other_edits_and_files_are_preserved(self):
        path = self.write("shell.json", SHELL)
        install.install("omarchy-shell", path, PROGRAM)
        data = json.loads(path.read_text())
        data["plugins"].append({"id": "another.service"})
        path.write_text(json.dumps(data))
        assets = shell_plugin.directory(path)
        service = assets / "Service.qml"
        service.write_text(service.read_text() + "// user edit\n")
        message = install.uninstall("omarchy-shell", path)
        self.assertIn("edited plugin assets kept", message)
        self.assertEqual(json.loads(path.read_text())["plugins"], [{"id": "another.service"}])
        self.assertTrue(service.read_text().endswith("// user edit\n"))

    def test_shell_plugin_collision_and_invalid_array_leave_config_untouched(self):
        path = self.write("shell.json", SHELL)
        assets = shell_plugin.directory(path)
        assets.mkdir(parents=True)
        (assets / "user-file").write_text("keep me")
        with self.assertRaises(install.InstallError):
            install.install("omarchy-shell", path, PROGRAM)
        self.assertEqual(path.read_text(), SHELL)
        self.assertFalse(path.with_name(path.name + install.BACKUP_SUFFIX).exists())
        path.write_text(SHELL.replace('"version": 1', '"plugins": {}, "version": 1'))
        with self.assertRaises(install.InstallError):
            install.install("omarchy-shell", path, PROGRAM)
        self.assertEqual((assets / "user-file").read_text(), "keep me")

    def test_executable_paths_are_shell_quoted(self):
        execute, click = install.commands("/home/a b/diurna")
        self.assertEqual(execute, "'/home/a b/diurna' bar")
        self.assertTrue(click.endswith("'/home/a b/diurna' today --watch"))
