"""Local Hyprland request socket and bounded newline event framing."""
import json
import socket

MAX_MESSAGE = 1024 * 1024  # Protocol allocation bound, not operational policy.


class Lines:
    def __init__(self):
        self.buffer = b""

    def feed(self, data):
        self.buffer += data
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        if len(self.buffer) > MAX_MESSAGE or any(len(x) > MAX_MESSAGE for x in lines):
            raise ValueError("oversized Hyprland event")
        return [line.decode("utf-8", errors="replace") for line in lines]


def request(directory, command, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(directory / ".socket.sock"))
        sock.sendall(("j/" + command).encode())
        data = bytearray()
        while chunk := sock.recv(65536):
            data.extend(chunk)
            if len(data) > MAX_MESSAGE:
                raise ValueError("oversized Hyprland response")
    return json.loads(data)


ACTIVE_KEYS = ("class", "address", "inhibitingIdle")


def snapshot(directory, timeout):
    active = request(directory, "activewindow", timeout)
    workspace = request(directory, "activeworkspace", timeout).get("name", "")
    clients = request(directory, "clients", timeout)
    # Drop titles before handing data to the reducer.
    return ({k: active[k] for k in ACTIVE_KEYS if k in active}, workspace,
            [{k: w[k] for k in ("class", "address") if k in w} for w in clients])


def inhibiting(directory, timeout):
    """Whether the focused window holds an idle inhibitor; None if unknown."""
    try:
        value = request(directory, "activewindow", timeout).get("inhibitingIdle")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None
