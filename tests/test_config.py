from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diurna import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.home = self.directory / "home"
        stack.enter_context(patch.object(Path, "home", return_value=self.home))
        stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.directory / "xdg"),
        }, clear=True))
        stack.enter_context(patch.object(config, "_diagnostics", set()))
        self.stderr = stack.enter_context(redirect_stderr(io.StringIO()))
        self.path = self.directory / "xdg/diurna/config.json"
        self.path.parent.mkdir(parents=True)

    def test_xdg_location_wins_and_home_fallback_ignores_working_directory(self):
        home_config = self.home / ".config/diurna/config.json"
        home_config.parent.mkdir(parents=True)
        home_config.write_text('{"safety_seconds": 11}')
        self.path.write_text('{"safety_seconds": 12}')
        self.assertEqual(config.policy()["safety_seconds"], 12)
        for value in (None, "", "relative"):
            with self.subTest(xdg=value):
                if value is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = value
                self.assertEqual(config.policy()["safety_seconds"], 11)

    def test_each_setting_falls_through_independently_and_removal_reloads(self):
        os.environ.update(DIURNA_SAFETY_SECONDS="20", DIURNA_IPC_TIMEOUT_SECONDS="4")
        self.path.write_text('{"safety_seconds": false, "ipc_timeout_seconds": 0.5}')
        defaults = {"hands_on_seconds": 60, "screensaver_grace_seconds": 10}
        self.assertEqual(config.policy(), {"safety_seconds": 20, "ipc_timeout_seconds": 0.5,
                                           **defaults})
        self.path.unlink()
        self.assertEqual(config.policy(), {"safety_seconds": 20, "ipc_timeout_seconds": 4,
                                           **defaults})
        os.environ.pop("DIURNA_SAFETY_SECONDS")
        os.environ.pop("DIURNA_IPC_TIMEOUT_SECONDS")
        self.assertEqual(config.policy(), {"safety_seconds": 60, "ipc_timeout_seconds": 3,
                                           **defaults})

    def test_bounds_types_and_nonfinite_numbers_in_file_and_environment(self):
        for key, env, low, high, default in (
            ("safety_seconds", "DIURNA_SAFETY_SECONDS", 1, 3600, 60),
            ("ipc_timeout_seconds", "DIURNA_IPC_TIMEOUT_SECONDS", 0.1, 30, 3),
            ("hands_on_seconds", "DIURNA_HANDS_ON_SECONDS", 1, 3600, 60),
            ("screensaver_grace_seconds", "DIURNA_SCREENSAVER_GRACE_SECONDS", 0, 3600, 10),
        ):
            for raw, expected in (
                (str(low), low), (str(high), high), ("0" if low else "-1", default),
                (str(high + 1), default), ("true", default), ('"2"', default),
                ("null", default), ("[]", default), ("{}", default),
                ("NaN", default), ("Infinity", default), ("-Infinity", default),
                ("1e999", default), ("9" * 400, default),
            ):
                with self.subTest(key=key, value=raw):
                    self.path.write_text('{"' + key + '":' + raw + '}')
                    self.assertEqual(config.policy()[key], expected)
                    self.path.unlink()
                    os.environ[env] = raw
                    self.assertEqual(config.policy()[key], expected)
                    del os.environ[env]

    def test_invalid_documents_discard_all_file_values_without_logging_contents(self):
        os.environ["DIURNA_SAFETY_SECONDS"] = "20"
        for raw in (
            b'{"safety_seconds": 15, "PRIVATE": 1}',
            b'{"safety_seconds": 15, "safety_seconds": 16}',
            b'{"safety_seconds": 15, "ipc_timeout_seconds": NaN}',
            b'[]', b'null', b'"PRIVATE"', b'{PRIVATE', b'\xff',
            b'{"safety_seconds":' + b'[' * 2000,
            b' ' * (config.MAX_CONFIG_BYTES + 1),
        ):
            with self.subTest(raw=raw[:80]):
                self.path.write_bytes(raw)
                self.assertEqual(config.policy()["safety_seconds"], 20)
        self.assertNotIn("PRIVATE", self.stderr.getvalue())
        self.assertNotIn(str(self.directory), self.stderr.getvalue())

    def test_unsafe_files_never_override_environment(self):
        os.environ["DIURNA_SAFETY_SECONDS"] = "20"
        target = self.directory / "target.json"
        target.write_text('{"safety_seconds": 15}')
        self.path.symlink_to(target)
        self.assertEqual(config.policy()["safety_seconds"], 20)
        self.path.unlink()
        os.link(target, self.path)
        self.assertEqual(config.policy()["safety_seconds"], 20)
        self.path.unlink()
        self.path.write_text(target.read_text())
        for mode in (0o666, 0o620, 0o602):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                self.assertEqual(config.policy()["safety_seconds"], 20)
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(config.policy()["safety_seconds"], 20)
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(config.policy()["safety_seconds"], 20)

    def test_diagnostics_repeat_only_after_recovery(self):
        self.path.write_text(json.dumps({"safety_seconds": "PRIVATE"}))
        config.policy()
        once = self.stderr.getvalue()
        config.policy()
        self.assertEqual(self.stderr.getvalue(), once)
        self.path.write_text("{}")
        config.policy()
        self.path.write_text(json.dumps({"safety_seconds": "PRIVATE"}))
        config.policy()
        self.assertEqual(self.stderr.getvalue(), once * 2)
