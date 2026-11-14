"""MCP through the real ``baseagent.main`` composition: modes, strict startup, resume, output, frontends.

Offline: scripted models and the in-repo fake MCP server. Frontend scripts run inside the patched
frontend runner, because ``main`` stops the MCP servers when the frontend returns (as in real use).
"""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_helpers import HAS_TEXTUAL, HAS_WEB, NO_TEXTUAL, NO_WEB, RUNNERS, join_background, wait_until
from mcp_helpers import ECHO, NOTE, Script, process_gone, read_pid, server_entry, tool_call, write_config
from test_main_agent import WAIT, Duplex, delegate, feed

from baseagent.main import main
from baseagent.mcp.trust import TrustStore
from baseagent.mcp.trust import snapshot as trust_snapshot
from baseagent.session import SessionStore


class CliCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "ws"
        self.root.mkdir()
        self.db = self.root.parent / "sessions.sqlite3"
        self.notes = self.root.parent / "notes.txt"
        self.pid_file = self.root.parent / "pid"

    def entry(self, *flags, **options):
        return server_entry("--notes", str(self.notes), "--pid-file", str(self.pid_file), *flags, **options)

    def configure(self, servers=None, name=".mcp.json", trusted=True):
        path = write_config(self.root / name, servers if servers is not None else {"demo": self.entry()})
        if name == ".mcp.json" and trusted:
            # A project .mcp.json needs first-use trust (docs/mcp.md); these tests are about what happens after it.
            snap = trust_snapshot(path)
            TrustStore().trust(path, snap.digest, [server.name for server in snap.servers])
        return path

    def run_cli(self, argv, model=None, inputs=None):
        out, err = io.StringIO(), io.StringIO()
        model = model or Script()
        self.model = model
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch("builtins.input", side_effect=feed(inputs or [])):
            try:
                code = main(["--root", str(self.root), "--db", str(self.db), *argv], model_factory=lambda args: model)
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def state(self, session="s"):
        return SessionStore(self.db).load(session)

    def results(self, session="s"):
        return [json.loads(message["content"]) for message in self.state(session).messages if message.get("role") == "tool"]

    def approve(self, session="s", flag="--approve-tool", digest=None):
        request = self.state(session).metadata["approval_request"]
        return self.run_cli(["--session", session, flag, request["call_id"],
                             "--approval-digest", digest or request["request_digest"]])


class ModeTests(CliCase):
    def test_without_configuration_there_are_no_mcp_tools_and_no_mcp_output(self):
        code, _, err = self.run_cli(["--session", "s", "hello"], Script({"content": "hi"}))
        self.assertEqual(code, 0)
        self.assertFalse([name for name in self.model.tools[0] if name.startswith("mcp__")])
        self.assertNotIn("MCP", err)

    def test_read_only_mode_offers_only_declared_read_only_tools_and_runs_them_without_approval(self):
        self.configure()
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "hi"})]}, {"content": "done"})
        code, out, err = self.run_cli(["--session", "s", "--mode", "read-only", "go"], model)
        self.assertEqual((code, out.strip()), (0, "done"), err)
        self.assertEqual([name for name in model.tools[0] if name.startswith("mcp__")], [ECHO])
        self.assertEqual(self.results()[0]["data"]["text"], "hi")
        self.assertIn(f"MCP: server 'demo' ready, 1 tool(s): {ECHO}", err)
        self.assertTrue(process_gone(read_pid(self.pid_file)), "server outlived the one-shot run")

    def test_a_hidden_tool_named_anyway_is_an_unknown_tool(self):
        self.configure()
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "x"})]}, {"content": "done"})
        code, _, _ = self.run_cli(["--session", "s", "go"], model)
        self.assertEqual(self.results()[0]["error"]["code"], "unknown_tool")
        self.assertFalse(self.notes.exists())

    def test_edit_and_full_modes_wait_for_approval_then_run_the_exact_call(self):
        self.configure()
        for mode in ("edit", "full"):
            with self.subTest(mode=mode):
                session = "s-" + mode
                model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": mode})]})
                code, out, err = self.run_cli(["--session", session, "--mode", mode, "go"], model)
                self.assertEqual(code, 1)
                self.assertIn(NOTE, model.tools[0])
                self.assertEqual(self.state(session).status, "awaiting_approval")
                self.assertNotIn(mode + "\n", self.notes.read_text() if self.notes.exists() else "")
                wrong, _, wrong_err = self.approve(session, digest="0" * 64)
                self.assertEqual(wrong, 1, wrong_err)
                self.assertEqual(self.approve(session)[0], 0)
                code, out, err = self.run_cli(["--session", session, "--mode", mode, "--resume"], Script({"content": "noted"}))
                self.assertEqual((code, out.strip()), (0, "noted"), err)
                self.assertIn(mode + "\n", self.notes.read_text())
                self.assertTrue(process_gone(read_pid(self.pid_file)))

    def test_tool_policy_denial_does_not_run_the_tool(self):
        self.configure()
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"default": "allow", "tools": {NOTE: "deny", ECHO: "deny"}}), encoding="utf-8")
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "x"}), tool_call("c2", ECHO, {"text": "x"})]},
                       {"content": "done"})
        code, _, err = self.run_cli(["--session", "s", "--mode", "edit", "--tool-policy", str(policy), "go"], model)
        self.assertEqual(code, 0, err)
        self.assertEqual([result["error"]["code"] for result in self.results()], ["permission_denied"] * 2)
        self.assertFalse(self.notes.exists())

    def test_flag_takes_precedence_over_the_workspace_file(self):
        self.configure({"project": self.entry()})
        other = self.configure({"flagged": self.entry()}, name="other.json")
        code, _, err = self.run_cli(["--session", "s", "--mcp-config", str(other), "hello"], Script({"content": "hi"}))
        self.assertEqual(code, 0, err)
        names = [name for name in self.model.tools[0] if name.startswith("mcp__")]
        self.assertEqual(names, ["mcp__flagged__echo"])

    def test_mcp_flags_are_rejected_for_session_operations(self):
        code, _, err = self.run_cli(["--list-sessions", "--mcp-strict"])
        self.assertEqual(code, 2)
        self.assertIn("--mcp-config and --mcp-strict apply to", err)


class FailureTests(CliCase):
    def broken(self):
        return {"gone": {"command": str(self.root / "no-such-command")}, "refuses": self.entry("--fail-initialize"),
                "demo": self.entry()}

    def test_broken_servers_are_skipped_by_default_and_the_rest_keeps_working(self):
        self.configure(self.broken())
        model = Script({"tool_calls": [tool_call("c1", ECHO, {"text": "ok"}), tool_call("c2", "glob", {"pattern": "*"})]},
                       {"content": "done"})
        code, out, err = self.run_cli(["--session", "s", "go"], model)
        self.assertEqual((code, out.strip()), (0, "done"), err)
        self.assertIn("MCP warning: server 'gone' failed to start", err)
        self.assertIn("MCP warning: server 'refuses' failed to start (initialize refused)", err)
        self.assertTrue(all(result["ok"] for result in self.results()))
        self.assertTrue(process_gone(read_pid(self.pid_file)))

    def test_strict_startup_failure_exits_with_code_2_before_any_model_call(self):
        self.configure(self.broken())
        model = Script({"content": "never"})
        code, out, err = self.run_cli(["--session", "s", "--mcp-strict", "go"], model)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("MCP error: server 'gone' failed to start", err)
        self.assertEqual(model.tools, [])
        code, _, err = self.run_cli(["--session", "s2", "--mcp-config", str(self.root / "absent.json"), "go"], model)
        self.assertEqual(code, 2)
        self.assertIn("--mcp-config file not found", err)

    def test_crash_and_timeout_during_a_call_come_back_as_tool_failures_and_the_run_finishes(self):
        self.configure({"demo": self.entry(read_only=("echo", "crash", "sleep"), timeout=0.3)})
        model = Script({"tool_calls": [tool_call("c1", "mcp__demo__sleep", {"seconds": 30})]}, {"content": "done"})
        code, out, err = self.run_cli(["--session", "slow", "go"], model)
        self.assertEqual((code, out.strip()), (0, "done"), err)
        self.assertEqual(self.results("slow")[0]["error"]["code"], "timeout")
        self.assertTrue(process_gone(read_pid(self.pid_file)), "a server stuck in a call survived the run")
        self.pid_file.unlink()
        model = Script({"tool_calls": [tool_call("c2", "mcp__demo__crash", {})]},
                       {"tool_calls": [tool_call("c3", ECHO, {"text": "after"}), tool_call("c4", "glob", {"pattern": "*"})]},
                       {"content": "done"})
        code, out, err = self.run_cli(["--session", "s", "go"], model)
        self.assertEqual((code, out.strip()), (0, "done"), err)
        results = self.results()
        self.assertEqual([result["ok"] for result in results], [False, False, True])  # local tools keep working
        self.assertEqual([result["error"]["code"] for result in results[:2]], ["execution_failed"] * 2)
        self.assertIn("process exited", results[0]["error"]["message"])
        self.assertTrue(process_gone(read_pid(self.pid_file)))


class SessionTests(CliCase):
    def test_changing_servers_of_an_unfinished_session_needs_accept_config_changes(self):
        self.configure()
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "x"})]})
        self.assertEqual(self.run_cli(["--session", "s", "--mode", "edit", "go"], model)[0], 1)
        resume = ["--session", "s", "--mode", "edit", "--resume"]
        for label, servers in (("added", {"demo": self.entry(), "second": self.entry()}),
                               ("replaced", {"demo": self.entry("--stderr-bytes", "1")}),
                               ("removed", {})):
            with self.subTest(label):
                self.configure(servers)
                code, _, err = self.run_cli(resume, Script({"content": "never"}))
                self.assertEqual(code, 1)
                self.assertIn("session runtime configuration changed", err)
        self.configure({"demo": self.entry("--stderr-bytes", "1")})
        self.approve()
        # The approval was bound to the old policy; with accepted changes the call is asked again, not run.
        code, _, err = self.run_cli([*resume, "--accept-config-changes"], Script({"content": "never"}))
        self.assertEqual(self.state().status, "awaiting_approval", err)
        self.configure()
        code, _, err = self.run_cli(resume, Script({"content": "never"}))
        self.assertIn("session runtime configuration changed", err)

    def test_json_output_keeps_stdout_clean_and_names_the_pending_mcp_call(self):
        self.configure({"gone": {"command": str(self.root / "no-such-command")}, "demo": self.entry()})
        model = Script({"tool_calls": [tool_call("c1", NOTE, {"text": "x"})]})
        code, out, err = self.run_cli(["--session", "s", "--mode", "edit", "--output-format", "json", "go"], model)
        result = json.loads(out)  # stdout is exactly one JSON object
        self.assertEqual((code, result["status"], result["pending_approval"]["name"]), (1, "awaiting_approval", NOTE))
        for line in ("MCP: starting server 'demo'", "MCP warning: server 'gone' failed to start", "Session: s"):
            self.assertIn(line, err)
        code, out, err = self.run_cli(["--session", "s2", "--mcp-strict", "--output-format", "json", "go"], model)
        self.assertEqual((code, json.loads(out)["exit_code"], json.loads(out)["status"]), (2, 2, "error"))


class FrontendCase(CliCase):
    """Run ``script(session)`` as the frontend of a real ``main`` composition."""

    def drive(self, ui, argv, model, script):
        failures = []

        def runner(session, **options):
            try:
                script(session)
            except BaseException as exc:  # noqa: BLE001 -- Reported after main has cleaned up.
                failures.append(exc)
            finally:
                wait_until(lambda: not session.busy, message="turn thread did not finish")
                if not session.closed:
                    session.exit(force=True, why="test end")
                session.wait(WAIT)
                join_background()
            return 0

        extra = [] if ui == "cli" else ["--ui", ui]
        err = io.StringIO()
        with patch(RUNNERS[ui], runner), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), "--db", str(self.db), *argv, *extra], model_factory=lambda args: model)
        if failures:
            raise failures[0]
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(process_gone(read_pid(self.pid_file)), "server outlived the chat session")

    def single(self):
        return Duplex(worker=[{"tool_calls": [tool_call("c1", NOTE, {"text": "from chat"})]}, {"content": "记下了。"}])


class FrontendTests(FrontendCase):
    ARGV = ["--session", "demo", "--no-main-agent", "--mode", "edit"]

    def test_cli_chat_asks_for_the_mcp_call_by_server_and_tool_name_then_returns_the_result(self):
        self.configure()
        prompts = []
        out = io.StringIO()
        with patch("builtins.input", side_effect=feed(["记一条", "/exit"], prompts, approvals=["y"])), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "--db", str(self.db), *self.ARGV], model_factory=lambda args: self.single())
        self.assertEqual(code, 0)
        self.assertIn(f"Allow {NOTE}? [y/N] ", prompts)
        self.assertIn("记下了。", out.getvalue())
        self.assertEqual(self.notes.read_text(), "from chat\n")
        self.assertTrue(process_gone(read_pid(self.pid_file)))

    def test_cli_chat_denial_does_not_call_the_server(self):
        self.configure()
        with patch("builtins.input", side_effect=feed(["记一条", "/exit"], approvals=["n"])), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(["--root", str(self.root), "--db", str(self.db), *self.ARGV], model_factory=lambda args: self.single())
        self.assertFalse(self.notes.exists())
        self.assertEqual(self.results("demo")[0]["error"]["code"], "permission_denied")

    @unittest.skipUnless(HAS_WEB, NO_WEB)
    def test_web_approval_endpoint_runs_the_mcp_call(self):
        from fastapi.testclient import TestClient

        from baseagent.web.server import create_app
        self.configure()

        def script(session):
            session.open()
            client = TestClient(create_app(session, token=None, host="127.0.0.1"), base_url="http://127.0.0.1:8765")
            self.assertEqual(client.post("/messages", json={"text": "记一条"}).json(), {"result": "sent"})
            wait_until(lambda: session.pending_approvals())
            (view,) = session.pending_approvals()
            self.assertEqual((view["worker"], view["name"]), (None, NOTE))
            self.assertIn("from chat", view["arguments"])
            wrong = client.post("/approvals", json={"call_id": view["call_id"], "digest": "0" * 64, "decision": "allow"})
            self.assertEqual(wrong.status_code, 409)
            self.assertFalse(self.notes.exists())
            allowed = client.post("/approvals", json={"call_id": view["call_id"], "digest": view["digest"], "decision": "allow"})
            self.assertEqual(allowed.json(), {"ok": True, "message": "allow recorded"})
            wait_until(lambda: not session.busy and any(item["kind"] == "assistant_message" for item in session.events_after(0)))
            results = [item["data"] for item in session.events_after(0) if item["kind"] == "tool_result"]
            self.assertEqual([(item["name"], item["ok"]) for item in results], [(NOTE, True)])

        self.drive("web", self.ARGV, self.single(), script)
        self.assertEqual(self.notes.read_text(), "from chat\n")

    @unittest.skipUnless(HAS_TEXTUAL, NO_TEXTUAL)
    def test_tui_approval_modal_runs_the_mcp_call(self):
        from textual.widgets import Input, Static

        from baseagent.tui.app import ApprovalScreen, ChatApp
        from test_tui import SIZE, until
        self.configure()

        def script(session):
            async def run():
                app = ChatApp(session, poll=0.02)
                async with app.run_test(size=SIZE) as pilot:
                    field = app.query_one("#input", Input)
                    field.focus()
                    field.value = "记一条"
                    await pilot.press("enter")
                    await until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#allow"),
                                "no approval modal")
                    details = str(app.screen.query_one("#details", Static).render())
                    self.assertIn(f"工具：{NOTE}", details)
                    self.assertFalse(self.notes.exists())
                    await until(pilot, lambda: app.screen.query_one("#allow").region.width > 0)
                    await pilot.click("#allow")
                    await until(pilot, lambda: any(text == "记下了。" for kind, text in app.entries if kind == "assistant"),
                                "the reply never arrived")
            asyncio.run(run())

        self.drive("tui", self.ARGV, self.single(), script)
        self.assertEqual(self.notes.read_text(), "from chat\n")


class WorkerTests(FrontendCase):
    def test_workers_get_mcp_tools_by_write_grant_and_side_effect_calls_still_need_approval(self):
        self.configure()
        model = Duplex(main=[delegate("d1", "read only"), {"content": "派出 w1"},
                             delegate("d2", "writer", allow_writes=True), {"content": "派出 w2"}],
                       by_task={"read only": [{"tool_calls": [tool_call("r1", ECHO, {"text": "worker"})]}, {"content": "read done"}],
                                "writer": [{"tool_calls": [tool_call("w1", NOTE, {"text": "by worker"})]}, {"content": "write done"}]})

        def script(session):
            session.open()
            session.send("先读")
            wait_until(lambda: not session.busy and not session.running(), message="reader never finished")
            session.send("再写")
            wait_until(lambda: session.pending_approvals(), message="writer never asked")
            (view,) = session.pending_approvals()
            self.assertEqual((view["worker"], view["name"]), ("w2", NOTE))
            self.assertFalse(self.notes.exists())
            self.assertEqual(session.approve(view["call_id"], view["digest"], worker="w2")["ok"], True)
            wait_until(lambda: not session.busy and not session.running(), message="writer never finished")

        self.drive("web", ["--session", "demo", "--mode", "edit"], model, script)
        reader, writer = model.worker_tools[0], model.worker_tools[-1]
        self.assertIn(ECHO, reader)
        self.assertNotIn(NOTE, reader)  # no write grant: only tools declared readOnly
        self.assertIn(NOTE, writer)
        self.assertFalse([name for name in model.main_tools[0] if name.startswith("mcp__")])
        self.assertEqual(self.notes.read_text(), "by worker\n")


if __name__ == "__main__":
    unittest.main()
