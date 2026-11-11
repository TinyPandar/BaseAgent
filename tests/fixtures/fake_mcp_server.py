"""Offline stdio MCP server for tests and the demo. Standard library only; no network.

Tools: echo, add_note (records to --notes FILE), fail (isError), crash (exits mid-call),
sleep (never answers in time), garbage (answers with a non-JSON line), rpc_error.
Flags make startup fail in a chosen way: --fail-initialize, --exit-on-start, --no-tools-capability,
--bad-list; --bad-name and --extra-tool NAME add tools the client must skip. --pid-file FILE records the process id; --spawn-child also starts a sleeping child
whose pid goes to FILE.child, so tests can assert that the whole process group is gone.
"""

import argparse
import json
import os
import subprocess
import sys
import time


def tool(name, description, properties=None, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required)}}


TEXT = {"text": {"type": "string"}}
TOOLS = [
    tool("echo", "Return the given text", TEXT, ["text"]),
    tool("add_note", "Append a note (side effect)", TEXT, ["text"]),
    tool("fail", "Always reports a tool error"),
    tool("crash", "Exits the server process during the call"),
    tool("sleep", "Sleeps for the given seconds before answering", {"seconds": {"type": "number"}}, ["seconds"]),
    tool("garbage", "Answers with a line that is not JSON"),
    tool("rpc_error", "Answers with a JSON-RPC error"),
]
BAD_NAME = tool("bad.name", "A name that cannot be registered")


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes")
    parser.add_argument("--pid-file")
    parser.add_argument("--spawn-child", action="store_true")
    parser.add_argument("--fail-initialize", action="store_true")
    parser.add_argument("--exit-on-start", action="store_true")
    parser.add_argument("--no-tools-capability", action="store_true")
    parser.add_argument("--bad-list", action="store_true")
    parser.add_argument("--bad-name", action="store_true")
    parser.add_argument("--stderr-bytes", type=int, default=0)
    parser.add_argument("--extra-tool", action="append", default=[])
    args = parser.parse_args()
    if args.pid_file:
        with open(args.pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    if args.spawn_child:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(args.pid_file + ".child", "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
    print("fake MCP server log line (stderr is not protocol)", file=sys.stderr, flush=True)
    if args.stderr_bytes:
        sys.stderr.write("x" * args.stderr_bytes + "\n")
        sys.stderr.flush()
    if args.exit_on_start:
        print("refusing to start", file=sys.stderr, flush=True)
        return 3
    tools = TOOLS + [tool(name, "Extra tool", TEXT) for name in args.extra_tool] + ([BAD_NAME] if args.bad_name else [])
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue  # notifications (initialized, cancelled) need no answer
        params = message.get("params") or {}
        if method == "initialize":
            if args.fail_initialize:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "initialize refused"}})
                continue
            capabilities = {} if args.no_tools_capability else {"tools": {"listChanged": False}}
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2025-11-25", "capabilities": capabilities,
                "serverInfo": {"name": "fake-mcp", "version": "1"}}})
        elif method == "tools/list":
            if args.bad_list:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": "not a list"}})
            elif not params.get("cursor"):
                # Two pages, so the client's pagination is exercised.
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools[:2], "nextCursor": "page2"}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools[2:]}})
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "resultType": "complete", "content": [{"type": "text", "text": arguments["text"]}],
                    "structuredContent": {"echo": arguments["text"]}, "isError": False}})
            elif name == "add_note":
                if args.notes:
                    with open(args.notes, "a", encoding="utf-8") as handle:
                        handle.write(arguments["text"] + "\n")
                send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "noted"}]}})
            elif name == "fail":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": "tool says no"}], "isError": True}})
            elif name == "crash":
                os._exit(7)
            elif name == "sleep":
                time.sleep(arguments["seconds"])
                send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "awake"}]}})
            elif name == "garbage":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
            elif name == "rpc_error":
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "server-side failure"}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown tool: " + str(name)}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
