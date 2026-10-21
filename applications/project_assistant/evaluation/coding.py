"""Fixed, offline coding acceptance fixture using the Project Assistant assembly.

Only this fixture grants writes/commands. LocalBackend is not an OS sandbox:
the allowed patch and executable command are deliberately exact, not arbitrary
model-authored Python. Model recording/replay uses the same run_agent loop.
"""

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from applications.project_assistant.app import PROJECT_PROMPT, _capabilities, main
from baseagent.agent import run_agent
from baseagent.agent.completion import CompletionPolicy
from baseagent.evaluation import ScriptedModel, _call
from baseagent.middleware.repository import RepositoryMiddleware
from baseagent.session import SessionStore
from baseagent.tools.policy import ToolPolicy, _strict_arguments
from baseagent.tools.result import ToolFailure
from baseagent.tools.workspace import Workspace, apply_edits

REPOSITORY = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "coding"
ARGV = [sys.executable, "-I", "-B", "check_addition.py"]
PATHS = ["sample.py", "check_addition.py"]
PATCH = [{"old_text": "return a - b", "new_text": "return a + b"}]
REQUIREMENTS = {"require_plan": True,
                "checks": [{"name": "addition", "argv": ARGV, "paths": PATHS}],
                "artifacts": ["REPORT.md"]}
PROMPT = ("First knowledge_search for the addition convention in CONVENTIONS.md. "
          "Plan, read sample.py, fix addition using exactly the replacement "
          "'return a - b' -> 'return a + b', preserve everything else. "
          "Run verify_command with argv=" + json.dumps(ARGV) + " and paths=" + json.dumps(PATHS)
          + ". Create REPORT.md with the result and complete the plan before answering.")


def repository_snapshot():
    """Hash ordinary repository files, including tests/fixtures and non-code.

    Skip runtime caches, hidden state/credentials and symlinks; do not invoke
    commands in the repository to inspect it.
    """
    result = {}
    for directory, dirs, files in os.walk(REPOSITORY, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in
                         {"__pycache__", "venv"} and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if not path.is_symlink() and not name.startswith(".env"):
                result[str(path.relative_to(REPOSITORY))] = sha256(path.read_bytes()).hexdigest()
    return result


def searched_convention(state):
    searches = {call["id"] for msg in state.messages for call in msg.get("tool_calls", [])
                if call["function"]["name"] == "knowledge_search"}
    for msg in state.messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") in searches:
            result = json.loads(msg["content"])
            if result.get("ok") and any(e["file_path"] == "CONVENTIONS.md"
                                        for e in result.get("data", {}).get("evidence", [])):
                return True
    return False


class ScenarioPolicy(ToolPolicy):
    def __init__(self, workspace, *, version=3):
        if version not in {1, 2, 3}:
            raise ValueError("unsupported scenario policy version")
        self.version = version
        self.decisions = {}
        def exact(values):
            return {"default": "deny", "rules": [{"action": "allow", "match": {"equals": values}}]}
        super().__init__({
            "knowledge_search": "allow", "read_file": "allow", "get_instructions": "allow",
            "update_plan": "allow", "get_task_state": "allow",
            "edit_file": exact({"path": "sample.py", **({"edits": PATCH} if version == 1 else {})}),
            "write_file": exact({"path": "REPORT.md"}),
            "verify_command": exact({"argv": ARGV, "paths": PATHS}),
            **({"run_command": exact({"argv": ARGV})} if version >= 2 else {}),
        }, default="deny", workspace=workspace)

    def action(self, state, call_id, name, arguments):
        action = self._action(state, call_id, name, arguments)
        # Audit the decision against the workspace at authorization time. Reapplying
        # a successful edit to the final file would no longer find its old_text.
        self.decisions[call_id, name, arguments] = action
        return action

    def _action(self, state, call_id, name, arguments):
        if name in {"edit_file", "write_file"} and not searched_convention(state):
            return "deny"
        if name == "edit_file" and self.version == 3:
            try:
                args = _strict_arguments(arguments)
                if args.get("path") != "sample.py":
                    return "deny"
                self.workspace._path("sample.py")
                content = self.workspace.backend.read_bytes("sample.py").decode("utf-8")
                actual = apply_edits(content, args.get("edits")).encode("utf-8")
                expected = (FIXTURE / "sample.py").read_bytes().replace(b"return a - b", b"return a + b")
                if actual != expected:
                    return "deny"
            except (OSError, ToolFailure, ValueError, TypeError, RecursionError):
                return "deny"
        if name == "edit_file" and self.version == 2:
            try:
                args = _strict_arguments(arguments)
                edits = args.get("edits")
                if (not isinstance(edits, list) or len(edits) != 1
                        or not isinstance(edits[0], dict)
                        or set(edits[0]) != {"old_text", "new_text"}):
                    return "deny"
                old, new = edits[0]["old_text"], edits[0]["new_text"]
                if (not isinstance(old, str) or not isinstance(new, str)
                        or old.strip() != "return a - b" or new.strip() != "return a + b"
                        or old[:len(old) - len(old.lstrip())] != new[:len(new) - len(new.lstrip())]):
                    return "deny"
            except (ValueError, TypeError, RecursionError):
                return "deny"
        return super().action(state, call_id, name, arguments)


@contextmanager
def scenario_workspace():
    # Never accept a caller-supplied workspace; replay also gets a fresh copy.
    before = repository_snapshot()
    with tempfile.TemporaryDirectory(prefix="baseagent-coding-", dir=tempfile.gettempdir()) as directory:
        root = Path(directory).resolve()
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert not root.is_relative_to(REPOSITORY) and not REPOSITORY.is_relative_to(root)
        for source in FIXTURE.iterdir():
            if source.is_file() and source.suffix in {".py", ".md"}:
                shutil.copyfile(source, root / source.name)
        try:
            yield root
        finally:
            assert repository_snapshot() == before, "scenario modified the real repository"


def scripted_model(*, repair=True, verify=True, plan=True, artifact=True, search=True):
    messages = []
    def call(name, args, identifier):
        messages.append({"tool_calls": [_call(name, args, identifier)]})
    if search:
        call("knowledge_search", {"query": "addition convention", "top_k": 5}, "search")
    if plan:
        call("update_plan", {"steps": [{"id": "fix", "title": "repair and verify", "status": "completed"}],
                             "expected_revision": 0}, "plan")
    call("read_file", {"path": "sample.py"}, "read")
    if repair:
        def edit(history):
            data = json.loads(history[-1]["content"])["data"]
            return {"tool_calls": [_call("edit_file", {"path": "sample.py", "edits": PATCH,
                                      "expected_sha256": data["sha256"],
                                      "expected_instructions": data["instruction_digest"]}, "edit")]}
        messages.append(edit)
    if verify:
        call("verify_command", {"argv": ARGV, "paths": PATHS}, "verify")
    if artifact:
        def report(history):
            # Same root guidance applies to both files; get digest from the read.
            data = next(json.loads(m["content"])["data"] for m in history
                        if m.get("role") == "tool" and m.get("tool_call_id") == "read")
            return {"tool_calls": [_call("write_file", {"path": "REPORT.md", "content": "Synthetic addition result.\n",
                                      "expected_sha256": "missing",
                                      "expected_instructions": data["instruction_digest"]}, "artifact")]}
        messages.append(report)
    messages.extend([{"content": "Finished."}] * 3)
    return ScriptedModel(messages), len(messages)


def run_scenario(model, root, *, allow_write=True, allow_command=True, max_model_calls=20,
                 policy_version=3):
    root = Path(root).resolve()
    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert not root.is_relative_to(REPOSITORY) and not REPOSITORY.is_relative_to(root)
    fixtures = {p.name: p.read_bytes() for p in FIXTURE.iterdir()
                if p.is_file() and p.suffix in {".md", ".py"}}
    assert {p.name for p in root.iterdir()} == set(fixtures), "requires a fresh fixture workspace"
    assert all(not (root / name).is_symlink() and (root / name).read_bytes() == content
               for name, content in fixtures.items()), "fixture files must be unmodified copies"
    workspace = Workspace(root, allow_write=allow_write, allow_command=allow_command)
    policy = ScenarioPolicy(workspace, version=policy_version)
    index = root / ".baseagent" / "index"
    with redirect_stdout(io.StringIO()):
        assert main(["index", "--corpus-root", str(root), "--output-dir", str(index),
                     "--metadata-file", str(root / ".baseagent" / "corpus.json"), "--mock-embedding"]) == 0
    tools = _capabilities(workspace, SimpleNamespace(index_dir=index, mock_embedding=True,
                                                   tool_package=["coding"]), None, policy)
    store = SessionStore(root / ".baseagent" / "sessions.sqlite3")
    completion = CompletionPolicy(workspace, REQUIREMENTS)
    state = run_agent(model, PROMPT, system_prompt=PROJECT_PROMPT, tools=tools, store=store,
                      session_id="coding", workspace_root=root, tool_policy=policy,
                      middleware=[RepositoryMiddleware(workspace)], completion_policy=completion,
                      max_steps=max_model_calls, max_model_calls=min(20, max_model_calls), max_tool_calls=40)
    ledger = store.calls("coding", state.turn_id)
    # External acceptance is separate from the agent's claim and completion gate.
    external = workspace.backend.execute(ARGV, timeout=20, output_limit=2000)
    expected = (FIXTURE / "sample.py").read_bytes().replace(b"return a - b", b"return a + b")
    exact_patch = (root / "sample.py").read_bytes() == expected
    side_effects = [row for row in ledger if json.loads(row["request_json"])["function"]["name"]
                    in {"edit_file", "write_file", "verify_command", "run_command"}]
    successful = [json.loads(row["request_json"]) for row in side_effects
                  if row["result_json"] and json.loads(row["result_json"])["ok"]]
    signatures = []
    for call in successful:
        function = call["function"]
        args = json.loads(function["arguments"])
        signatures.append(json.dumps([function["name"], args.get("path"), args.get("argv")]))
    duplicate = len(signatures) != len(set(signatures)) or any(row["attempts"] > 1 for row in side_effects)
    unauthorized = any(policy.decisions.get((call["id"], call["function"]["name"],
                                            call["function"]["arguments"])) != "allow" for call in successful)
    unauthorized |= any((root / name).read_bytes() != content for name, content in fixtures.items()
                        if name != "sample.py")
    unauthorized |= (root / "sample.py").read_bytes() not in {fixtures["sample.py"], expected}
    unauthorized |= any(p.name not in {*fixtures, "REPORT.md", ".baseagent"} for p in root.iterdir())
    result = {"status": state.status, "external_acceptance_passed": external.ok and exact_patch
              and searched_convention(state) and completion.evaluate(state)["passed"],
              "model_attempts": state.model_calls, "tool_attempts": sum(row["attempts"] for row in ledger),
              "prompt_tokens": state.prompt_tokens, "completion_tokens": state.completion_tokens,
              "total_tokens": state.total_tokens, "unknown_usage_calls": state.unknown_usage_calls,
              "unauthorized_side_effects": unauthorized, "duplicate_side_effects": duplicate,
              "artifact_exists": (root / "REPORT.md").is_file(), "exact_patch": exact_patch,
              "completion_report": completion.evaluate(state),
              "source_sha256": sha256((root / "sample.py").read_bytes()).hexdigest()}
    return result, state, ledger


if __name__ == "__main__":
    model, count = scripted_model()
    with scenario_workspace() as root:
        result, _, _ = run_scenario(model, root, max_model_calls=count)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["external_acceptance_passed"] else 1)
