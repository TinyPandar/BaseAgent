"""Web frontend (``baseagent --ui web``): FastAPI TestClient against a real ChatSession. Fake models only.

``GET /events?wait=0`` returns what is buffered and closes, so SSE is read without
hanging the test client; a browser keeps the stream open and reconnects with
Last-Event-ID. Skipped with a reason when the optional 'web' extra is not installed.
"""

import json
import unittest

from chat_helpers import HAS_WEB, NO_WEB, ChatTestCase, wait_until
from test_main_agent import WAIT, Duplex, Gate, delegate, tool_call

if HAS_WEB:
    from fastapi.testclient import TestClient

    from baseagent.chat import EventBuffer
    from baseagent.web.server import create_app

LOCAL = "http://127.0.0.1:8765"
TOKEN = "s3cret-token-value"


def parse_sse(text):
    """[(id, event dict)] from an SSE body; comments and the retry line are skipped."""
    items = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "data" in fields:
            items.append((fields.get("id"), json.loads(fields["data"])))
    return items


@unittest.skipUnless(HAS_WEB, NO_WEB)
class WebTestCase(ChatTestCase):
    def client(self, session, token=None, host="127.0.0.1", base_url=LOCAL):
        session.open()
        return TestClient(create_app(session, token=token, host=host), base_url=base_url)

    def stream(self, client, headers=None, **params):
        response = client.get("/events", params={"wait": 0, **params}, headers=headers or {})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return parse_sse(response.text)


class WebEventTests(WebTestCase):
    def test_sse_starts_with_a_snapshot_then_sends_events_in_order_with_ids(self):
        model = Duplex(main=[delegate("d1", "Read README.md"), {"content": "已派出 w1。"}],
                       worker=[{"content": "read it. README.md:1-1"}])
        session = self.session(["--session", "demo"], model, ui="web")
        self.assertEqual(self.options, {"host": "127.0.0.1", "port": 8765, "token": None})
        client = self.client(session)
        (first_id, resync), = self.stream(client)
        self.assertEqual(resync["kind"], "resync")
        self.assertEqual(first_id, session.events.cursor(resync["id"]))
        self.assertEqual(client.post("/messages", json={"text": "读 README"}).json(), {"result": "sent"})
        self.idle(session)
        events = self.stream(client, headers={"Last-Event-ID": first_id})
        ids = [event["id"] for _, event in events]
        self.assertEqual(ids, list(range(resync["id"] + 1, resync["id"] + 1 + len(ids))))
        self.assertTrue(all(sse_id == session.events.cursor(event["id"]) for sse_id, event in events))
        kinds = [event["kind"] for _, event in events]
        for earlier, later in (("user_message", "worker_started"), ("worker_started", "assistant_message")):
            self.assertLess(kinds.index(earlier), kinds.index(later))
        self.assertIn("worker_done", kinds)
        self.assertNotIn("resync", kinds)

    def test_last_event_id_replays_only_later_events(self):
        session = self.session(["--session", "demo"], Duplex(main=[{"content": "one"}, {"content": "two"}]), ui="web")
        client = self.client(session)
        client.post("/messages", json={"text": "first"})
        wait_until(lambda: not session.busy)
        events = self.stream(client, after=session.events.cursor(0))
        middle_id, middle = events[len(events) // 2]
        client.post("/messages", json={"text": "second"})
        wait_until(lambda: not session.busy)
        replay = self.stream(client, headers={"Last-Event-ID": middle_id})
        self.assertEqual(replay[0][1]["id"], middle["id"] + 1)
        texts = [event["data"].get("text") for _, event in replay]
        self.assertIn("second", texts)
        self.assertIn("two", texts)
        self.assertEqual(self.stream(client, headers={"Last-Event-ID": replay[-1][0]}), [])

    def test_too_old_or_foreign_last_event_id_gets_resync_first(self):
        session = self.session(["--session", "demo"], Duplex(main=[{"content": "one"}, {"content": "two"}]), ui="web")
        session.events = EventBuffer(capacity=4)
        client = self.client(session)
        for text in ("first", "second"):
            client.post("/messages", json={"text": text})
            wait_until(lambda: not session.busy)
        for stale in (session.events.cursor(1), "0123abcd:3"):
            events = self.stream(client, headers={"Last-Event-ID": stale})
            self.assertEqual(events[0][1]["kind"], "resync", stale)
            history = events[0][1]["data"]["snapshot"]["history"]
            self.assertEqual([item["text"] for item in history], ["first", "one", "second", "two"])
            self.assertEqual(len(events), 1)


class WebCommandTests(WebTestCase):
    def test_status_tell_cancel_and_messages(self):
        gate = Gate({"tool_calls": [tool_call("g2", "glob", {"pattern": "*.md"})]})
        hold = Gate({"content": "never"})
        self.gates = [gate, hold]
        model = Duplex(main=[delegate("d1", "A"), {"content": "已派出 w1。"}, delegate("d2", "B"),
                             {"content": "已派出 w2。"}],
                       by_task={"A": [gate, {"content": "A finished"}], "B": [hold]})
        session = self.session(["--session", "demo"], model, ui="web")
        client = self.client(session)
        client.post("/messages", json={"text": "A"})
        self.assertTrue(gate.entered.wait(WAIT))
        wait_until(lambda: not session.busy)
        client.post("/messages", json={"text": "B"})
        self.assertTrue(hold.entered.wait(WAIT))
        wait_until(lambda: not session.busy)
        status = client.get("/status").json()
        self.assertEqual([(worker["id"], worker["status"]) for worker in status["workers"]],
                         [("w1", "running"), ("w2", "running")])
        self.assertEqual(status["budget"]["max_model_calls"], 16)
        told = client.post("/workers/w1/tell", json={"text": "also check LICENSE"})
        self.assertEqual(told.status_code, 200)
        self.assertTrue(told.json()["ok"])
        self.assertEqual(client.post("/workers/w9/tell", json={"text": "x"}).status_code, 409)
        cancelled = client.post("/workers/w2/cancel", json={})
        self.assertEqual(cancelled.json(), {"ok": True, "results": [{"ok": True, "message": "w2 cancelled"}]})
        self.assertEqual(client.post("/workers/w2/cancel", json={}).status_code, 409)
        gate.release()
        self.idle(session)
        self.assertIn("[主 Agent 转达的补充要求]\nalso check LICENSE",
                      [message.get("content") for message in self.worker("w1").messages])
        self.assertEqual(client.get("/status").json()["workers"][1]["status"], "cancelled")
        self.assertEqual(client.post("/messages", json={"text": "/cancel all"}).json(), {"result": "command"})
        self.assertEqual(client.post("/messages", json={"text": "/exit"}).status_code, 409)

    def test_approval_endpoint_allows_denies_and_rejects_a_wrong_digest(self):
        model = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                               {"content": "Created out.txt."}])
        session = self.session(["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])],
                               model, ui="web")
        client = self.client(session)
        client.post("/messages", json={"text": "create out.txt"})
        wait_until(lambda: session.pending_approvals())
        (view,) = self.stream(client)[0][1]["data"]["snapshot"]["approvals"]
        self.assertEqual((view["worker"], view["name"]), ("w1", "write_file"))
        wrong = client.post("/approvals", json={"call_id": view["call_id"], "digest": "0" * 64, "decision": "allow"})
        self.assertEqual(wrong.status_code, 409)
        self.assertIn("digest does not match", wrong.json()["message"])
        self.assertEqual(client.post("/approvals", json={"call_id": view["call_id"], "digest": view["digest"],
                                                         "decision": "maybe"}).status_code, 400)
        self.assertFalse((self.root / "out.txt").exists())
        allowed = client.post("/approvals", json={"call_id": view["call_id"], "digest": view["digest"],
                                                  "decision": "allow", "worker": "w1"})
        self.assertEqual(allowed.json(), {"ok": True, "message": "allow recorded"})
        self.idle(session)
        self.assertEqual((self.root / "out.txt").read_text(), "hello")

    def test_deny_through_the_endpoint(self):
        model = Duplex(main=[delegate("d1", "Create out.txt", allow_writes=True), {"content": "ok"}],
                       worker=[{"tool_calls": [tool_call("w1c", "write_file", self.write_request())]},
                               {"content": "Write was denied."}])
        session = self.session(["--session", "demo", "--allow-write", "--tool-policy", self.policy(["write_file"])],
                               model, ui="web")
        client = self.client(session)
        client.post("/messages", json={"text": "create out.txt"})
        wait_until(lambda: session.pending_approvals())
        (view,) = session.pending_approvals()
        denied = client.post("/approvals", json={"call_id": view["call_id"], "digest": view["digest"], "decision": "deny"})
        self.assertEqual(denied.status_code, 200)
        self.idle(session)
        self.assertFalse((self.root / "out.txt").exists())
        self.assertEqual(self.tool_result("w1")["error"]["code"], "permission_denied")

    def test_posts_accept_json_only_and_validate_fields(self):
        session = self.session(["--session", "demo"], Duplex(), ui="web")
        client = self.client(session)
        self.assertEqual(client.post("/messages", data={"text": "hi"}).status_code, 415)
        self.assertEqual(client.post("/messages", content="hi", headers={"Content-Type": "text/plain"}).status_code, 415)
        self.assertEqual(client.post("/messages", content="{bad", headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(client.post("/messages", json=["hi"]).status_code, 400)
        self.assertEqual(client.post("/messages", json={"text": ""}).status_code, 400)
        self.assertEqual(client.post("/workers/w1/tell", json={}).status_code, 400)


class WebSecurityTests(WebTestCase):
    def test_page_and_assets_are_local_files(self):
        session = self.session(["--session", "demo"], Duplex(), ui="web")
        client = self.client(session)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/static/vendor/pico.min.css", page.text)
        self.assertNotIn("http://", page.text.replace("http-equiv", ""))
        self.assertNotIn("https://", page.text)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])
        for asset in ("/static/app.js", "/static/app.css", "/static/vendor/pico.min.css"):
            self.assertEqual(client.get(asset).status_code, 200, asset)
        self.assertEqual(client.get("/static/../server.py").status_code, 404)
        self.assertEqual(client.get("/static/index.html").status_code, 404)
        self.assertNotIn("access-control-allow-origin", {key.lower() for key in page.headers})
        preflight = client.options("/messages", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", {key.lower() for key in preflight.headers})

    def test_token_is_required_on_every_endpoint(self):
        session = self.session(["--session", "demo"], Duplex(main=[{"content": "hi"}]), ui="web")
        client = self.client(session, token=TOKEN, host="0.0.0.0", base_url="http://192.168.1.20:8765")
        requests = [("get", "/", {}), ("get", "/status", {}), ("get", "/events", {"params": {"wait": 0}}),
                    ("get", "/static/app.js", {}), ("post", "/messages", {"json": {"text": "hi"}}),
                    ("post", "/approvals", {"json": {"call_id": "a", "digest": "b", "decision": "allow"}}),
                    ("post", "/workers/w1/cancel", {"json": {}}), ("post", "/workers/w1/tell", {"json": {"text": "x"}})]
        for method, path, options in requests:
            with self.subTest(path=path):
                self.assertEqual(getattr(client, method)(path, **options).status_code, 401)
                wrong = {"Authorization": "Bearer wrong-token-value"}
                self.assertEqual(getattr(client, method)(path, headers=wrong, **options).status_code, 401)
        self.assertEqual(client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)
        self.assertEqual(client.get("/status", headers={"X-BaseAgent-Token": TOKEN}).status_code, 200)
        page = client.get("/", params={"token": TOKEN})
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"/static/app.js?token={TOKEN}", page.text)
        self.assertEqual(client.get("/events", params={"wait": 0, "token": TOKEN}).status_code, 200)
        self.assertEqual(client.post("/messages", json={"text": "hi"},
                                     headers={"Authorization": f"Bearer {TOKEN}"}).json(), {"result": "sent"})

    def test_non_loopback_without_token_refuses_to_start(self):
        session = self.session(["--session", "demo"], Duplex(), ui="web")
        with self.assertRaises(ValueError):
            create_app(session, host="0.0.0.0")
        with self.assertRaises(ValueError):
            create_app(session, host="192.168.1.20", token="")

    def test_without_a_token_only_loopback_host_headers_are_answered(self):
        session = self.session(["--session", "demo"], Duplex(), ui="web")
        client = self.client(session)
        self.assertEqual(client.get("/status").status_code, 200)
        self.assertEqual(client.get("/status", headers={"Host": "localhost:8765"}).status_code, 200)
        self.assertEqual(client.get("/status", headers={"Host": "[::1]:8765"}).status_code, 200)
        self.assertEqual(client.get("/status", headers={"Host": "evil.example:8765"}).status_code, 403)
        self.assertEqual(client.post("/messages", json={"text": "hi"}, headers={"Host": "evil.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
