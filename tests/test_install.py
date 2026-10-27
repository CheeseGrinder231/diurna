import json
from pathlib import Path
import re
import tempfile
import unittest

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
        self.assertNotIn("@PROGRAM@", source)
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
