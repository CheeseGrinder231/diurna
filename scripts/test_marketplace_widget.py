"""Test the marketplace widget with fake processes, without a desktop or data."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from test_shell_plugin import runner


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="diurna-widget-test-") as temporary:
        # Exercise QML URL decoding and launcher quoting with a path that cannot
        # safely be treated as an unquoted shell word.
        staging = Path(temporary) / "space's $dollar;name"
        shutil.copytree(root / "tests/qml_marketplace_widget", staging)
        (staging / "plugin").mkdir()
        shutil.copyfile(root / "plugin/BarWidget.qml", staging / "plugin/BarWidget.qml")
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           QT_QPA_PLATFORMTHEME="", QT_STYLE_OVERRIDE="Fusion")
        result = subprocess.run([runner(), "-input", str(staging / "tst_bar.qml"),
                                 "-import", str(staging)], env=environment,
                                capture_output=True, text=True)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            return result.returncode
        launches = [json.loads(line.split("DIURNA_LAUNCH:", 1)[1])
                    for line in result.stdout.splitlines() if "DIURNA_LAUNCH:" in line]
        if len(launches) != 1:
            raise SystemExit("Missing launcher argument proof")
        launch = launches[0]
        # omarchy-launch-or-focus-tui builds this command before handing it to
        # omarchy-launch-or-focus. Parse only; do not execute it.
        recovered = shlex.split("omarchy-launch-tui " + " ".join(launch["argv"][1:]))
        expected = ["omarchy-launch-tui", "--app-id=TUI.float", "python3", "-B",
                    str(staging / "plugin.py"), "today", "--watch"]
        if recovered != expected or launch["runner"] != expected[4]:
            raise SystemExit(f"Launcher argument mismatch: {recovered!r}")
        print("Launcher recovers the exact runner path and arguments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
