"""Source launcher for the Omarchy plugin; no build, install or config writes."""
import json
from pathlib import Path
import sys

from diurna.__main__ import main
from diurna.install import hands_on_seconds, shell_idle_seconds


def shell_config():
    # Omarchy's manager and shell use this fixed path, independently of XDG.
    # Diurna's own policy file still follows XDG_CONFIG_HOME as documented.
    return Path.home() / ".config/omarchy/shell.json"


def settings():
    """Use the same thresholds as manual installation, refreshed on shell load."""
    idle = 150
    try:
        with shell_config().open(encoding="utf-8") as stream:
            text = stream.read(65537)
        if len(text) <= 65536:
            candidate = shell_idle_seconds(text)
            if 1 <= candidate <= 86400:
                idle = candidate
    except (OSError, ValueError, RecursionError):
        pass
    return {"idleSeconds": idle, "handsOnSeconds": hands_on_seconds()}


if __name__ == "__main__":
    if sys.argv[1:] == ["plugin-settings"]:
        print(json.dumps(settings()))
    else:
        sys.exit(main())
