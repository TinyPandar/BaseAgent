"""MCP stdio client, configuration and process lifecycle (README plan B). Offline fake server only."""

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcp_helpers import FAKE_SERVER, manager, process_gone, read_pid, server_config, server_entry, write_config

from baseagent.kernel import ToolRegistry
from baseagent.kernel.cancellation import CancellationToken
from baseagent.kernel.result import ErrorCode, ToolFailure
from baseagent.mcp import (McpClient, McpError, McpManager, McpStartupError, ServerConfig, StdioTransport,
                           find_config, load_config)
from baseagent.mcp.config import parse_server
from baseagent.mcp.transport import STDERR_LIMIT
from baseagent.tools.workspace import Workspace, coding_tools


class TempCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def started(self, *configs, **options):
        value = manager(*configs, **options).start()
        self.addCleanup(value.close)
        return value


class ClientTests(TempCase):
    def client(self, *flags, timeout=60.0):
        return self.started(server_config(*flags, timeout=timeout)).clients["demo"]

    def failure(self, client, name, arguments=None, **options):
        with self.assertRaises(ToolFailure) as caught:
            client.call_tool(name, arguments or {}, **options)
        return caught.exception

    def test_initialize_lists_every_page_and_calls_a_tool(self):
        value = self.started()
        names = [tool["name"] for tool in value.tools["demo"]]
        self.assertEqual(names[:3], ["echo", "add_note", "fail"])  # the fake server pages after two tools
        self.assertEqual(value.clients["demo"].server_info["name"], "fake-mcp")
        result = value.clients["demo"].call_tool("echo", {"text": "line one\nline two ✓"})
        self.assertEqual(result.data, {"text": "line one\nline two ✓", "structured": {"echo": "line one\nline two ✓"}})

    def test_messages_are_single_lines_of_utf8_json(self):
        sent = []
        transport = StdioTransport(sys.executable, (FAKE_SERVER,))
        original = transport._send

        def spy(message):
            sent.append(json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
            original(message)

        transport._send = spy
        transport.start()
        self.addCleanup(transport.close)
        client = McpClient("demo", transport)
        client.initialize(10)
        client.call_tool("echo", {"text": "a\nb"})
        self.assertEqual([json.loads(line).get("method") for line in sent],
                         ["initialize", "notifications/initialized", "tools/call"])
        self.assertTrue(all("\n" not in line for line in sent))
        self.assertEqual(json.loads(sent[0])["params"]["protocolVersion"], "2025-11-25")

    def test_tool_error_rpc_error_and_non_json_are_failures_and_the_server_keeps_running(self):
        client = self.client()
        reported = client.call_tool("fail", {})
        self.assertEqual((reported.ok, reported.error.code, reported.error.message), (False, ErrorCode.EXECUTION_FAILED, "tool says no"))
        self.assertIn("error -32000", str(self.failure(client, "rpc_error")))
        self.assertEqual(self.failure(client, "missing").code, ErrorCode.INVALID_ARGUMENTS)
        self.assertIn("not a JSON-RPC message", str(self.failure(client, "garbage")))
        self.assertEqual(client.call_tool("echo", {"text": "still here"}).data["text"], "still here")

    def test_server_exit_during_a_call_is_a_failure_and_later_calls_fail_fast(self):
        client = self.client()
        self.assertIn("process exited (code 7)", str(self.failure(client, "crash")))
        started = time.monotonic()
        self.assertEqual(self.failure(client, "echo", {"text": "x"}).code, ErrorCode.EXECUTION_FAILED)
        self.assertLess(time.monotonic() - started, 2)

    def test_call_timeout_deadline_and_cancellation_interrupt_a_hanging_call(self):
        client = self.client(timeout=0.3)
        started = time.monotonic()
        self.assertEqual(self.failure(client, "sleep", {"seconds": 30}).code, ErrorCode.TIMEOUT)
        self.assertLess(time.monotonic() - started, 5)
        client.call_timeout = 60
        self.assertEqual(self.failure(client, "sleep", {"seconds": 30}, remaining_seconds=lambda: 0.2).code, ErrorCode.TIMEOUT)
        self.assertEqual(self.failure(client, "echo", {"text": "x"}, remaining_seconds=lambda: 0).code, ErrorCode.TIMEOUT)
        flag = threading.Event()
        threading.Timer(0.2, flag.set).start()
        started = time.monotonic()
        cancelled = self.failure(client, "sleep", {"seconds": 30}, cancellation=CancellationToken(flag.is_set))
        self.assertEqual(cancelled.code, ErrorCode.CANCELLED)
        self.assertLess(time.monotonic() - started, 5)

    def test_stderr_is_logging_capped_at_the_command_output_limit(self):
        client = self.client("--stderr-bytes", str(STDERR_LIMIT * 3))
        self.assertEqual(client.call_tool("echo", {"text": "ok"}).data["text"], "ok")
        transport = client.transport
        self.assertIn("fake MCP server log line", transport.stderr_text())
        self.assertLessEqual(len(transport.stderr_text().encode()), STDERR_LIMIT)
        self.assertTrue(transport.stderr_truncated)

    def test_close_stops_the_server_and_its_child_process_group(self):
        pid_file = self.root / "pid"
        value = manager(server_config("--pid-file", str(pid_file), "--spawn-child")).start()
        server, child = read_pid(pid_file), read_pid(str(pid_file) + ".child")
        value.close()
        self.assertTrue(process_gone(server), "server process survived close()")
        self.assertTrue(process_gone(child), "child of the server survived close()")
        value.close()  # idempotent

    def test_transport_reports_a_command_that_cannot_start(self):
        transport = StdioTransport(str(self.root / "no-such-binary"))
        with self.assertRaises(McpError) as caught:
            transport.start()
        self.assertEqual(caught.exception.kind, "closed")


class StartupTests(TempCase):
    def lines(self):
        self.messages = []
        return self.messages.append

    def test_failing_servers_are_skipped_with_a_reason_and_the_others_still_work(self):
        bad = {"missing": ServerConfig("missing", str(self.root / "nope")),
               "refuses": server_config("--fail-initialize", name="refuses"),
               "exits": server_config("--exit-on-start", name="exits"),
               "notools": server_config("--no-tools-capability", name="notools"),
               "badlist": server_config("--bad-list", name="badlist")}
        value = self.started(*bad.values(), server_config(), warn=self.lines())
        self.assertEqual(list(value.clients), ["demo"])
        text = "\n".join(self.messages)
        for name, reason in (("missing", "cannot start server command"), ("refuses", "initialize refused"),
                             ("exits", "process exited"), ("notools", "tools capability"), ("badlist", "no tools array")):
            self.assertRegex(text, f"MCP warning: server '{name}' failed to start \\(.*{reason}.*server skipped")
        self.assertIn("refusing to start", text)  # bounded excerpt of the server's own stderr
        registry = coding_tools(Workspace(self.root))
        self.assertEqual(value.register(registry, allow_write=False), ["mcp__demo__echo"])
        self.assertIn("read_file", registry._tools)

    def test_strict_mode_fails_on_the_first_unusable_server_and_leaves_no_process(self):
        pid_file = self.root / "pid"
        for bad in (server_config("--fail-initialize", name="refuses"),
                    ServerConfig("missing", str(self.root / "nope"))):
            strict = manager(server_config("--pid-file", str(pid_file)), bad, strict=True)
            with self.assertRaises(McpStartupError):
                strict.start()
            self.assertTrue(process_gone(read_pid(pid_file)), "the healthy server was left running")
            pid_file.unlink()

    def test_names_are_prefixed_builtins_are_never_replaced_and_bad_names_are_skipped(self):
        value = self.started(server_config("--extra-tool", "echo", "--bad-name"), warn=self.lines())
        registry = ToolRegistry()
        registry.register("mcp__demo__fail", "builtin with the same name", {"type": "object"}, lambda: "builtin")
        names = value.register(registry, allow_write=True)
        self.assertIn("mcp__demo__echo", names)
        self.assertNotIn("mcp__demo__fail", names)
        self.assertEqual(registry.execute("mcp__demo__fail", {}).data, "builtin")
        text = "\n".join(self.messages)
        self.assertIn("mcp__demo__fail would replace an existing tool; MCP tool skipped", text)
        self.assertIn("mcp__demo__echo would replace an existing tool", text)  # duplicate inside one server
        self.assertIn("'mcp__demo__bad.name' is not a usable tool name", text)
        self.assertEqual(registry.origin("mcp__demo__echo"), ("mcp", "demo"))
        self.assertEqual(registry.origin("mcp__demo__fail"), ("local", None))
        self.assertFalse(registry.retry_safe("mcp__demo__add_note"))
        self.assertTrue(registry.retry_safe("mcp__demo__echo"))  # declared readOnly in the configuration
        strict = self.started(server_config(), strict=True)
        with self.assertRaises(McpStartupError):
            strict.register(registry, allow_write=True)

    def test_read_only_mode_hides_tools_that_are_not_declared_read_only(self):
        value = self.started(warn=self.lines())
        self.assertEqual(value.register(ToolRegistry(), allow_write=False), ["mcp__demo__echo"])
        self.assertIn("not declared readOnly are hidden in read-only mode", self.messages[-1])

    def test_a_registered_handler_refuses_calls_that_do_not_come_from_the_gateway(self):
        notes = self.root / "notes.txt"
        value = self.started(server_config("--notes", str(notes)))
        registry = ToolRegistry()
        value.register(registry, allow_write=True)
        direct = registry.execute("mcp__demo__add_note", {"text": "sneaky"})
        self.assertEqual((direct.error.code, direct.error.message),
                         (ErrorCode.PERMISSION_DENIED, "MCP tools run only through the tool gateway"))
        self.assertFalse(notes.exists())


class ConfigTests(TempCase):
    def test_flag_wins_then_workspace_file_then_off(self):
        self.assertIsNone(find_config(None, self.root))
        self.assertIsNone(McpManager.from_arguments(None, self.root))
        project = write_config(self.root / ".mcp.json")
        other = write_config(self.root / "other.json", {"other": server_entry()})
        self.assertEqual(find_config(None, self.root), project)
        self.assertEqual(find_config(other, self.root), other)
        self.assertEqual([server.name for server in McpManager.from_arguments(other, self.root).servers], ["other"])
        with self.assertRaises(McpStartupError):
            McpManager.from_arguments(self.root / "absent.json", self.root)

    def test_entries_are_validated_one_by_one(self):
        path = write_config(self.root / "c.json", {
            "ok": {"command": "srv", "args": ["a"], "env": {"TOKEN": "secret-value"}, "timeout": 5,
                   "tools": {"search": {"readOnly": True}, "write": {"readOnly": False}, "other": {}}},
            "http": {"type": "http", "url": "https://example.invalid/mcp"},
            "bad__name": {"command": "srv"}, "noargs": {"command": "srv", "args": "a b"},
            "noenv": {"command": "srv", "env": {"A": 1}}, "flag": {"command": "srv", "tools": {"t": {"readOnly": "yes"}}},
            "empty": {}})
        servers, problems = load_config(path)
        (server,) = servers
        self.assertEqual((server.name, server.command, server.args, server.timeout, server.read_only_tools),
                         ("ok", "srv", ("a",), 5.0, frozenset({"search"})))
        self.assertEqual(len(problems), 6)
        self.assertIn("only stdio servers", "\n".join(problems))
        self.assertNotIn("secret-value", server.digest())
        self.assertNotEqual(server.digest(), parse_server("ok", {"command": "srv", "args": ["b"]}).digest())

    def test_an_unreadable_file_disables_mcp_with_a_warning_or_fails_in_strict_mode(self):
        for text in ("{not json", json.dumps({"servers": {}})):
            (self.root / ".mcp.json").write_text(text, encoding="utf-8")
            lines = []
            self.assertIsNone(McpManager.from_arguments(None, self.root, warn=lines.append))
            self.assertIn("MCP warning:", lines[0])
            with self.assertRaises(McpStartupError):
                McpManager.from_arguments(None, self.root, strict=True)

    def test_warnings_default_to_stderr(self):
        from contextlib import redirect_stderr
        (self.root / ".mcp.json").write_text("{", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            McpManager.from_arguments(None, self.root)
        self.assertIn("MCP warning:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
