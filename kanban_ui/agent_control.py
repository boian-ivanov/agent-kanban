"""Board → driver control channel.

The per-task agent driver (examples/task-driver.py) exposes a localhost
control socket on an ephemeral port recorded in ``task_runs.control_port``.
This module is the client side used by the API endpoints and the rules
engine: send one JSON-line command and read the driver's JSON-line reply.

Commands:
    {"cmd": "ping" | "status"}
    {"cmd": "steer", "text": "...", "comment_id": N | null}
    {"cmd": "stop", "reason": "...", "to_status": "blocked" | "approved"}
"""

from __future__ import annotations

import json
import socket
from typing import Any


class ControlUnavailable(RuntimeError):
    """Driver control socket unreachable (no live run / port dead)."""


def control_request(
    port: int | None,
    payload: dict[str, Any],
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Send one control command to the driver and return its reply.

    Raises ControlUnavailable when no port is recorded or the socket is
    unreachable / replies garbage.
    """
    if not port:
        raise ControlUnavailable("no control_port recorded for the task run")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(payload) + "\n").encode())
            data = s.recv(65536)
    except OSError as e:
        raise ControlUnavailable(
            f"driver control socket :{port} unreachable: {e}"
        ) from None
    try:
        return json.loads(data.decode())
    except ValueError:
        raise ControlUnavailable(
            f"invalid reply from driver control socket :{port}"
        ) from None
