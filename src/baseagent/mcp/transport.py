"""MCP transports. Only stdio is implemented; ``Transport`` is the seam for Streamable HTTP.

stdio framing follows the MCP transport specification: one JSON-RPC message per
line, UTF-8, no embedded newlines; the server's stderr is logging, not protocol.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Protocol

MAX_MESSAGE_BYTES = 1_000_000
# Same cap as tools.process.run_process keeps per output stream.
STDERR_LIMIT = 20_000
POLL_SECONDS = 0.05


class McpError(Exception):
    """A transport or protocol failure. kind: timeout, cancelled, closed, protocol or rpc."""

    def __init__(self, kind: str, message: str, *, code: int | None = None):
        super().__init__(message)
        self.kind, self.code = kind, code


class Transport(Protocol):
    """What the client needs from a transport. A future HTTP transport implements the same four calls."""

    def start(self) -> None: ...

    def request(self, method: str, params: dict | None, *, timeout: float, cancellation: Any = None) -> dict: ...

    def notify(self, method: str, params: dict | None = None) -> None: ...

    def close(self) -> None: ...


class StdioTransport:
    def __init__(self, command: str, args=(), *, env: dict | None = None, cwd=None, grace_seconds: float = 1.0):
        self.argv = [command, *args]
        self.env, self.cwd, self.grace_seconds = env, cwd, grace_seconds
        self.process: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: dict[int, queue.SimpleQueue] = {}
        self._next_id = 0
        self._closed_reason: str | None = None
        self._stderr = bytearray()
        self.stderr_truncated = False
        self._threads: list[threading.Thread] = []

    # ----- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        environment = {**os.environ, **(self.env or {})}
        # Own process group (session on POSIX), so close() can stop the server and its children.
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                 if os.name == "nt" else {"start_new_session": True})
        try:
            self.process = subprocess.Popen(self.argv, cwd=self.cwd, env=environment, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **group)
        except (OSError, ValueError) as exc:
            self._closed_reason = f"cannot start server command ({type(exc).__name__})"
            raise McpError("closed", self._closed_reason) from exc
        for target in (self._read_stdout, self._read_stderr):
            thread = threading.Thread(target=target, daemon=True, name="mcp-stdio")
            thread.start()
            self._threads.append(thread)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None and self._closed_reason is None

    def stderr_text(self, wait: float = 0.0) -> str:
        """Captured server stderr. ``wait`` > 0 first joins the stderr reader for at most that
        many seconds, so an excerpt taken after a failure includes what the server wrote last."""
        if wait > 0 and len(self._threads) > 1:
            self._threads[1].join(timeout=wait)
        return bytes(self._stderr).decode("utf-8", errors="replace")

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        self._fail_all("closed", "MCP client closed")
        try:
            # Specification order: close stdin, wait, then terminate, then kill.
            process.stdin.close()
        except OSError:
            pass
        if not self._wait(process, self.grace_seconds):
            self._signal_group(process, signal.SIGTERM)
            self._wait(process, self.grace_seconds)
        # Always sweep the group: descendants may outlive the server process itself.
        self._kill_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for thread in self._threads:
            thread.join(timeout=2)

    @staticmethod
    def _wait(process, seconds) -> bool:
        try:
            process.wait(timeout=seconds)
            return True
        except subprocess.TimeoutExpired:
            return False

    @staticmethod
    def _signal_group(process, signum) -> None:
        if os.name == "nt":
            process.terminate()
            return
        try:
            os.killpg(process.pid, signum)
        except OSError:
            pass

    @staticmethod
    def _kill_group(process) -> None:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        if process.poll() is None:
            process.kill()

    # ----- messages ----------------------------------------------------------------------
    def _send(self, message: dict) -> None:
        # json.dumps escapes newlines inside strings, so the line never contains an embedded one.
        line = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            if self._closed_reason is not None or self.process is None:
                raise McpError("closed", self._closed_reason or "MCP server is not running")
            try:
                self.process.stdin.write(line)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise McpError("closed", "MCP server closed its input") from exc

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(self, method: str, params: dict | None, *, timeout: float, cancellation: Any = None) -> dict:
        waiter: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            if self._closed_reason is not None:
                raise McpError("closed", self._closed_reason)
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                        **({"params": params} if params is not None else {})})
            deadline = time.monotonic() + timeout
            while True:
                try:
                    kind, value = waiter.get(timeout=POLL_SECONDS)
                    break
                except queue.Empty:
                    if cancellation is not None and cancellation.cancelled():
                        self._abandon(request_id, method, "cancelled")
                        raise McpError("cancelled", "MCP request cancelled") from None
                    if time.monotonic() >= deadline:
                        self._abandon(request_id, method, "timeout")
                        raise McpError("timeout", f"MCP request timed out after {timeout:g} seconds") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if kind == "result":
            return value
        if kind == "rpc":
            raise McpError("rpc", value[1], code=value[0])
        raise McpError(kind, value)

    def _abandon(self, request_id: int, method: str, reason: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)
        if method == "initialize":
            return
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except McpError:
            pass

    def _fail_all(self, kind: str, reason: str, *, close: bool = True) -> None:
        with self._lock:
            if close and self._closed_reason is None:
                self._closed_reason = reason
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter.put((kind, reason))

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        try:
            while True:
                raw = stream.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    break
                if not raw.endswith(b"\n") and len(raw) > MAX_MESSAGE_BYTES:
                    # The stream cannot be resynchronized after an oversized line.
                    self._fail_all("protocol", "MCP server sent a message above the size limit")
                    self._kill_group(self.process)
                    return
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError
                except (ValueError, RecursionError):
                    # Which request this line answered is unknown: fail the waiting ones, keep the server.
                    self._fail_all("protocol", "MCP server wrote a line that is not a JSON-RPC message", close=False)
                    continue
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            code = self.process.poll()
            self._fail_all("closed", "MCP server process exited" + (f" (code {code})" if code is not None else ""))

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            if "id" in message:
                # Server-to-client requests (sampling, elicitation, roots) are outside this client.
                reply = ({"result": {}} if message["method"] == "ping"
                         else {"error": {"code": -32601, "message": "method not supported by this client"}})
                try:
                    self._send({"jsonrpc": "2.0", "id": message["id"], **reply})
                except McpError:
                    pass
            return
        with self._lock:
            waiter = self._pending.get(message.get("id")) if type(message.get("id")) is int else None
        if waiter is None:
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code") if type(error.get("code")) is int else -32603
            waiter.put(("rpc", (code, str(error.get("message", ""))[:500])))
        elif isinstance(message.get("result"), dict):
            waiter.put(("result", message["result"]))
        else:
            waiter.put(("protocol", "MCP response has neither a result object nor an error"))

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        try:
            while chunk := stream.read1(4096):
                room = STDERR_LIMIT - len(self._stderr)
                self._stderr.extend(chunk[:max(room, 0)])
                self.stderr_truncated |= len(chunk) > room
        except (OSError, ValueError):
            pass
