"""Headless marketplace lifecycle checks with fake processes and desktop types."""
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_shell_plugin import runner, run


def main():
    root = Path(__file__).resolve().parents[1]
    executable = runner()
    # Exercise the same signals against the repository-ready component.
    activity = (root / "plugin/Activity.qml").read_text().replace(
        'property var command: ["diurna"]', 'property var command: ["/synthetic/diurna"]')
    result = run(executable, activity)
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, file=sys.stderr, end="")
        return result.returncode
    import os
    import subprocess
    with tempfile.TemporaryDirectory(prefix="diurna service ") as temporary:
        staging = Path(temporary)
        shutil.copytree(root / "tests/qml", staging, dirs_exist_ok=True)
        shutil.copytree(root / "tests/qml_marketplace_service", staging, dirs_exist_ok=True)
        for name in ("Service.qml", "Activity.qml"):
            shutil.copyfile(root / "plugin" / name, staging / name)
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           QT_QPA_PLATFORMTHEME="", QT_STYLE_OVERRIDE="Fusion")
        result = subprocess.run([executable, "-input", str(staging / "tst_service.qml"),
                                 "-import", str(staging)], env=environment,
                                capture_output=True, text=True)
    print(result.stdout, end="")
    print(result.stderr, file=sys.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
