"""Run the shipped QML with fake native types, without a desktop or processes."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# Support running this file directly, like the repository's build script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diurna.shell_plugin import payload  # noqa: E402


def runner():
    found = shutil.which("qmltestrunner")
    if found:
        return found
    for path in ("/usr/lib/qt6/bin/qmltestrunner", "/usr/lib64/qt6/bin/qmltestrunner"):
        if Path(path).is_file():
            return path
    raise SystemExit("Qt 6 qmltestrunner is required; see README's QML test instructions")


def run(executable, source):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="diurna-qml-test-") as temporary:
        staging = Path(temporary)
        shutil.copytree(root / "tests/qml", staging, dirs_exist_ok=True)
        (staging / "Service.qml").write_text(source, encoding="utf-8")
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           QT_QPA_PLATFORMTHEME="", QT_STYLE_OVERRIDE="Fusion")
        # Local imports replace both Quickshell modules: no compositor, real
        # IdleMonitor or child process is used. All stimuli are synchronous.
        return subprocess.run([executable, "-input", str(staging / "tst_activity.qml"),
                               "-import", str(staging)], env=environment,
                              capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-mutations", action="store_true",
                        help="also prove missing startup/change notifications fail")
    args = parser.parse_args()
    executable = runner()
    source = payload("/synthetic/diurna", 150)["Service.qml"]
    result = run(executable, source)
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, file=sys.stderr, end="")
        return result.returncode
    if args.check_mutations:
        mutations = {
            "startup notification": "        enqueue(monitor.isIdle ? \"idle\" : \"active\")",
            "idle-change notification": '        onIsIdleChanged: if (root.ready) root.enqueue(isIdle ? "idle" : "active")',
        }
        for name, notification in mutations.items():
            if source.count(notification) != 1:
                raise SystemExit(f"mutation target changed: {name}")
            mutant = run(executable, source.replace(notification, ""))
            failures = [line for line in mutant.stdout.splitlines()
                        if line.startswith("FAIL!") and "::ActivityProducer::test_" in line]
            if mutant.returncode == 0 or not failures:
                print(mutant.stdout, end="")
                print(mutant.stderr, file=sys.stderr, end="")
                raise SystemExit(f"mutation was not caught by behavior assertions: {name}")
            print(f"Caught missing {name} with behavior assertions:")
            print("\n".join(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
