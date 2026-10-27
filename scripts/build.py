"""Build a standalone stdlib-only zipapp, excluding tests and working files."""
import argparse
from pathlib import Path
import shutil
import tempfile
import zipapp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="diurna-build-") as staging:
        shutil.copytree(root / "diurna", Path(staging) / "diurna",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (Path(staging) / "__main__.py").write_text(
            "import sys\nfrom diurna.__main__ import main\nsys.exit(main())\n")
        zipapp.create_archive(staging, args.output, interpreter="/usr/bin/python3",
                              compressed=True)


if __name__ == "__main__":
    main()
