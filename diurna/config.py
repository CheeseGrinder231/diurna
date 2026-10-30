"""Bounded, call-time configuration for the local collector."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
from types import MappingProxyType


# Parser/allocation safety bound, independent of operational settings.
MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class Setting:
    default: float
    minimum: float
    maximum: float
    env: str

    def valid(self, value):
        try:
            return (type(value) in (int, float) and math.isfinite(value)
                    and self.minimum <= value <= self.maximum)
        except (OverflowError, TypeError):
            return False


SCHEMA = {
    "safety_seconds": Setting(60.0, 1.0, 3600.0, "DIURNA_SAFETY_SECONDS"),
    "ipc_timeout_seconds": Setting(3.0, 0.1, 30.0, "DIURNA_IPC_TIMEOUT_SECONDS"),
    # Input this recent counts as hands-on; set in the shell plugin at install.
    "hands_on_seconds": Setting(60.0, 1.0, 3600.0, "DIURNA_HANDS_ON_SECONDS"),
    # A screensaver up longer than this turns the on-screen stretch before it away.
    "screensaver_grace_seconds": Setting(10.0, 0.0, 3600.0, "DIURNA_SCREENSAVER_GRACE_SECONDS"),
}


class InvalidConfig(ValueError):
    """Only fixed reason codes may reach diagnostics."""


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidConfig("duplicate-key")
        value[key] = item
    return value


def _constant(_):
    raise InvalidConfig("nonfinite")


def _file(errors):
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not directory.is_absolute():
        directory = Path.home() / ".config"
    path = directory / "diurna/config.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_nlink > 1):
                raise InvalidConfig("unsafe-file")
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise InvalidConfig("oversized")
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(value, dict):
            raise InvalidConfig("object-required")
        if any(key not in SCHEMA for key in value):
            raise InvalidConfig("unknown-key")
        return value
    except FileNotFoundError:
        errors.add("file:missing")
    except InvalidConfig as error:
        errors.add("file:" + str(error))
    except (ValueError, UnicodeError, RecursionError):
        errors.add("file:malformed")
    except OSError:
        errors.add("file:unavailable")
    return {}


_diagnostics = set()
_diagnostic_lock = threading.Lock()


def _warn(errors):
    global _diagnostics
    with _diagnostic_lock:
        for reason in sorted(errors - _diagnostics):
            print(f"diurna: {reason}; using bounded defaults", file=sys.stderr)
        _diagnostics = set(errors)


def _environment_snapshot():
    # Copy CPython's backing dict before decoding: concurrent changes must not
    # invent a combination of values or interrupt iteration.
    environment = os.environ
    if not isinstance(environment, os._Environ) or type(environment._data) is not dict:
        raise InvalidConfig("unavailable")
    captured = environment._data.copy()
    return {environment.decodekey(key): environment.decodevalue(value)
            for key, value in captured.items()}


def policy():
    """Reload file > named environment > default, validating each setting.

    Malformed, unknown-key and duplicate-key documents fall through together;
    invalid setting values fall through individually. No last-good cache.
    """
    errors = set()
    file_values = _file(errors)
    try:
        environment = _environment_snapshot()
    except (AttributeError, InvalidConfig, UnicodeError):
        environment = {}
        errors.add("env:unavailable")
    values = {}
    for key, setting in SCHEMA.items():
        values[key] = setting.default
        if setting.env in environment:
            try:
                value = json.loads(environment[setting.env], parse_constant=_constant)
                if not setting.valid(value):
                    raise ValueError()
                values[key] = value
            except (ValueError, RecursionError):
                errors.add("env:invalid:" + key)
        if key in file_values:
            if setting.valid(file_values[key]):
                values[key] = file_values[key]
            else:
                errors.add("file:invalid:" + key)
    _warn(errors)
    return MappingProxyType(values)
