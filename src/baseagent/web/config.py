"""Web frontend settings: loopback by default, a token everywhere else. Standard library only."""

from __future__ import annotations

import ipaddress

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def is_loopback(host):
    host = (host or "").strip().strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def web_settings(host=None, port=None, token=None):
    """Validated keyword arguments for ``serve``. Raises ValueError instead of starting unsafely."""
    host = (host or DEFAULT_HOST).strip()
    port = DEFAULT_PORT if port is None else port
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535")
    token = (token or "").strip() or None
    if not is_loopback(host) and token is None:
        raise ValueError(f"refusing to bind {host} without --token (or BASEAGENT_WEB_TOKEN); "
                         "only 127.0.0.1/localhost may run without a token")
    if token is not None and len(token) < 8:
        raise ValueError("--token must be at least 8 characters")
    return {"host": host, "port": port, "token": token}
