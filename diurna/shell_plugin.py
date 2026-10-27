"""Owned Omarchy service assets. Config text edits live in install.py."""
from importlib.resources import files
import json
from pathlib import Path
import tempfile

PLUGIN_ID = "diurna.activity"


def payload(executable, timeout):
    manifest = {"schemaVersion": 1, "id": PLUGIN_ID, "name": "Diurna activity",
                "version": "1.0.0", "author": "Diurna", "kinds": ["service"],
                "entryPoints": {"service": "Service.qml"},
                "diurnaProgram": executable, "diurnaIdleSeconds": timeout}
    source = (files("diurna") / "shell_plugin/Service.qml").read_text(encoding="utf-8")
    source = source.replace("@PROGRAM@", json.dumps(executable)).replace("@TIMEOUT@", str(timeout))
    return {"manifest.json": json.dumps(manifest, indent=2) + "\n", "Service.qml": source}


def directory(config):
    return config.parent / "plugins" / PLUGIN_ID


def prepare(config, executable, timeout):
    """Validate before touching shell.json; refuse to overwrite user assets."""
    target = directory(config)
    data = payload(executable, timeout)
    if target.exists():
        if target.is_symlink() or set(p.name for p in target.iterdir()) != set(data) or any(
                (target / name).is_symlink() or (target / name).read_text() != text
                for name, text in data.items()):
            raise ValueError(f"{target} has different assets; move them aside before installing")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".diurna-", dir=target.parent) as staging:
        staged = Path(staging) / PLUGIN_ID
        staged.mkdir()
        for name, text in data.items():
            (staged / name).write_text(text, encoding="utf-8")
        staged.rename(target)


def remove(config):
    """Delete only our unchanged generated files; preserve edited/unknown assets."""
    target = directory(config)
    if not target.exists():
        return ""
    try:
        manifest = json.loads((target / "manifest.json").read_text())
        data = payload(manifest["diurnaProgram"], manifest["diurnaIdleSeconds"])
        if target.is_symlink() or set(p.name for p in target.iterdir()) != set(data) or any(
                (target / name).is_symlink() or (target / name).read_text() != text
                for name, text in data.items()):
            return f"; edited plugin assets kept at {target}"
    except (OSError, ValueError, KeyError, TypeError):
        return f"; unrecognized plugin assets kept at {target}"
    for name in data:
        (target / name).unlink()
    target.rmdir()
    return ""
