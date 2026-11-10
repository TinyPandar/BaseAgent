"""FastAPI + SSE server for one chat session (``baseagent --ui web``).

One process serves exactly one ``ChatSession``. The browser gets a static
page, one Server-Sent Events stream (``GET /events``) and a few JSON POST
commands. All rules (approvals, worker limits, the single writer, budgets,
persistence) stay in ``ChatSession``/``WorkerManager``; this module only maps
HTTP to those calls.

Security: bound to 127.0.0.1 by default. Any other address requires a token,
which every request must carry (``Authorization: Bearer``, ``X-BaseAgent-Token``
or ``?token=`` for the page and the EventSource). Without a token, requests
must name a loopback Host (DNS-rebinding guard). There is no CORS middleware,
and POST bodies must be ``application/json``.
"""

from __future__ import annotations

import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from .config import DEFAULT_HOST, is_loopback

STATIC = Path(__file__).with_name("static")
ASSETS = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8",
          "vendor/pico.min.css": "text/css; charset=utf-8"}
STREAM_SECONDS = 300.0   # an EventSource reconnects with Last-Event-ID after the server closes a stream
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.1
MAX_TEXT = 16000
HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
PAGE_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def create_app(session, *, token=None, host=DEFAULT_HOST, stream_seconds=STREAM_SECONDS,
               heartbeat_seconds=HEARTBEAT_SECONDS, poll_seconds=POLL_SECONDS):
    """Build the app for an opened ChatSession. Refuses a non-loopback host without a token.

    ``heartbeat_seconds`` is the idle interval between ``: ping`` comments on ``/events``;
    ``poll_seconds`` how often the stream checks for new events and a disconnected client.
    """
    if not is_loopback(host) and not token:
        raise ValueError(f"refusing to serve {host} without a token")
    if not heartbeat_seconds > 0 or not poll_seconds > 0:
        raise ValueError("heartbeat and poll intervals must be positive")
    app = FastAPI(title="BaseAgent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session = session

    def authorize(request):
        if token:
            supplied = (request.headers.get("x-baseagent-token") or request.query_params.get("token") or "")
            bearer = request.headers.get("authorization", "")
            if bearer.lower().startswith("bearer "):
                supplied = bearer[7:].strip()
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                raise HTTPException(401, "missing or wrong token")
            return
        if not is_loopback(_hostname(request.headers.get("host") or "")):
            raise HTTPException(403, "this server only answers requests addressed to localhost")

    async def body(request):
        authorize(request)
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise HTTPException(415, "POST bodies must be application/json")
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "JSON body must be an object")
        return value

    def text_field(value, name):
        text = value.get(name)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise HTTPException(400, f"{name} must be 1-{MAX_TEXT} characters")
        return text

    # A plain ASGI wrapper rather than @app.middleware("http"): BaseHTTPMiddleware hides the
    # client's disconnect from the endpoint, so request.is_disconnected() never turned true there.
    app.add_middleware(SecurityHeaders)

    @app.get("/")
    def page(request: Request):
        authorize(request)
        suffix = f"?token={quote(token)}" if token else ""
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{ASSET_QUERY}}", suffix)
        return HTMLResponse(html, headers={"Content-Security-Policy": PAGE_POLICY})

    @app.get("/static/{name:path}")
    def asset(name: str, request: Request):
        authorize(request)
        if name not in ASSETS:
            raise HTTPException(404)
        return Response((STATIC / name).read_bytes(), media_type=ASSETS[name])

    @app.get("/status")
    def status(request: Request):
        authorize(request)
        return session.status()

    @app.get("/events")
    async def events(request: Request, wait: float | None = None, after: str | None = None):
        """SSE: one JSON event per message, ``id: <epoch>:<n>``. A stale or missing id starts with resync."""
        authorize(request)
        cursor = session.events.parse_cursor(request.headers.get("last-event-id") or after)
        hold = stream_seconds if wait is None else max(0.0, min(float(wait), stream_seconds))

        async def stream():
            nonlocal cursor
            yield "retry: 2000\n\n"
            deadline = time.monotonic() + hold
            beat = time.monotonic() + heartbeat_seconds
            while True:
                items = session.events_after(cursor)
                for item in items:
                    cursor = item["id"]
                    payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
                    yield f"id: {session.events.cursor(item['id'])}\ndata: {payload}\n\n"
                now = time.monotonic()
                if now >= deadline or await request.is_disconnected():
                    return
                if now >= beat:
                    beat = now + heartbeat_seconds
                    yield ": ping\n\n"
                await anyio.sleep(min(poll_seconds, max(0.0, deadline - now)))

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/messages")
    async def messages(request: Request):
        value = await body(request)
        text = text_field(value, "text")
        if text.strip() in {"/exit", "/quit"}:
            session.events.publish("error", message="Web 端一个进程服务一个会话：在启动它的终端按 Ctrl+C 停止服务。"
                                                    "运行中的 worker 会被取消并记录。")
            return JSONResponse({"result": "exit"}, status_code=409)
        result = await run_in_threadpool(session.submit, text)
        return JSONResponse({"result": result}, status_code=409 if result == "rejected" else 200)

    @app.post("/approvals")
    async def approvals(request: Request):
        value = await body(request)
        call_id, digest, decision = value.get("call_id"), value.get("digest"), value.get("decision")
        if not all(isinstance(item, str) and item for item in (call_id, digest)) or decision not in {"allow", "deny"}:
            raise HTTPException(400, "call_id, digest and decision (allow|deny) are required")
        worker = value.get("worker", "*")
        if worker is not None and not isinstance(worker, str):
            raise HTTPException(400, "worker must be a worker id, null (main agent) or omitted")
        from baseagent.chat import ANY
        target = ANY if worker == "*" else worker
        answer = session.approve if decision == "allow" else session.deny
        result = await run_in_threadpool(lambda: answer(call_id, digest, worker=target))
        return JSONResponse(result, status_code=200 if result["ok"] else 409)

    @app.post("/workers/{worker_id}/cancel")
    async def cancel(worker_id: str, request: Request):
        await body(request)
        results = await run_in_threadpool(session.cancel, worker_id)
        ok = all(item[0] for item in results)
        return JSONResponse({"ok": ok, "results": [{"ok": item[0], "message": item[1]} for item in results]},
                            status_code=200 if ok else 409)

    @app.post("/workers/{worker_id}/tell")
    async def tell(worker_id: str, request: Request):
        value = await body(request)
        ok, message = await run_in_threadpool(session.tell, worker_id, text_field(value, "text").strip())
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)

    return app


class SecurityHeaders:
    """Add HEADERS to every HTTP response without wrapping the request or response body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in HEADERS.items():
                    if key not in headers:
                        headers[key] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _hostname(header):
    """Host header without the port ("[::1]:8765" -> "::1", "localhost:8765" -> "localhost")."""
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def serve(session, *, host=DEFAULT_HOST, port=8765, token=None):
    """Run the server until Ctrl+C; then running workers are cancelled and recorded, as in the CLI."""
    import uvicorn

    session.open()
    app = create_app(session, token=token, host=host)
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{port}/" + (f"?token={quote(token)}" if token else "")
    print(f"BaseAgent Web: {url}", file=sys.stderr)
    print(f"One session per server process: {session.session_id or '(new, created by the first message)'}. "
          "Ctrl+C stops the server.", file=sys.stderr)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        session.exit(force=True, why="Ctrl+C")
    return 0
