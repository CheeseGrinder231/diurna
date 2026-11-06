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


def check_focus_identity(pattern):
    # Exact selector from omacom/omarchy d5b32d24d37af8f0376a67d98531bb2da629824b,
    # bin/omarchy-launch-or-focus:13. Evaluate only synthetic window metadata.
    selector = r'.[]|select((.class|test("\\b" + $p + "\\b";"i")) or (.title|test("\\b" + $p + "\\b";"i")))|.address'
    unrelated = {"class": "TUI.float", "title": "Package manager", "address": "0x1"}
    browser = {"class": "chromium", "title": "Notes about Diurna Screen Time", "address": "0x2"}
    diurna = {"class": "TUI.float", "title": "Diurna Screen Time", "address": "0x3"}
    for clients, expected in (([unrelated], []), ([unrelated, browser], []),
                              ([unrelated, browser, diurna], ["0x3"])):
        result = subprocess.run(["jq", "-r", "--arg", "p", pattern, selector],
                                input=json.dumps(clients), capture_output=True, text=True,
                                check=True)
        if result.stdout.splitlines() != expected:
            raise SystemExit("Diurna focus identity selected an unrelated window")
    print("Focus selector ignores unrelated TUI.float windows and reuses only Diurna.")


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
        # Parse the helper's evaluated command without executing it. The exact
        # TUI.float class retains Omarchy's float/center/size rules; the title
        # identifies Diurna independently of other floating terminal windows.
        if launch["argv"][:2] != ["omarchy-launch-or-focus", "^Diurna Screen Time$"]:
            raise SystemExit("Unexpected timeline focus command")
        recovered = shlex.split(launch["argv"][2])
        expected = ["uwsm-app", "--", "xdg-terminal-exec", "--app-id=TUI.float",
                    "--title=Diurna Screen Time", "-e", "python3", "-B",
                    str(staging / "plugin.py"), "today", "--watch"]
        if recovered != expected or launch["runner"] != expected[8]:
            raise SystemExit(f"Launcher argument mismatch: {recovered!r}")
        check_focus_identity(launch["argv"][1])
        print("Launcher recovers the exact runner path and arguments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
