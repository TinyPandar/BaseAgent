"""Edit constraints, approval/resume, and transactional check failures."""

import json
from pathlib import Path
import sys

import pytest

from baseagent.agent import run_agent
from baseagent.agent.state import RunStatus
from baseagent.session import SessionStore
from baseagent.tools.edit_policy import EditPolicy, changed_lines
from baseagent.tools.policy import ToolPolicy
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace, coding_tools


class Model:
    def __init__(self, *messages):
        self.messages = iter(messages)

    def complete(self, messages, tools):
        return next(self.messages)


def write_args(workspace, path="result.txt", content="one\ntwo\n"):
    return dict(path=path, content=content, expected_sha256="missing",
                expected_instructions=workspace.get_instructions(path)["digest"])


def tool_call(name, args):
    # Compact/non-ASCII serialization must still match the original approval.
    return {"id": "edit", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, separators=(",", ":"), ensure_ascii=False)}}


@pytest.mark.parametrize("path", [".env", ".env.local", ".git/config", "server.pem", "nested/key.key", "secrets/token.txt", "id_rsa.pub", "nested/id_rsa"])
def test_default_denials_are_tool_errors(tmp_path, path):
    workspace = Workspace(tmp_path, allow_write=True)
    result = coding_tools(workspace).execute("write_file", dict(
        path=path, content="secret", expected_sha256="missing",
        expected_instructions=workspace.get_instructions()["digest"]))
    assert result.error.code == ErrorCode.PERMISSION_DENIED
    assert not (tmp_path / path).exists()


def test_allowlist_deny_precedence_and_canonical_paths(tmp_path):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(
        allowlist=["src/**/*.py"], denylist=["src/private/**"]))
    for path in ("src/main.py", "src/pkg/module.py"):
        workspace.write_file(**write_args(workspace, path))
    for path in ("README.md", "src/private/module.py", "src/../README.md"):
        with pytest.raises(ToolFailure, match="edit policy"):
            workspace.write_file(**write_args(workspace, path))


def test_diff_count_and_threshold_boundary(tmp_path):
    assert changed_lines("a\nb\n", "a\nc\n") == 2
    assert changed_lines("a\r\n", "a\n") == 2
    assert changed_lines("a", "a\n") == 2
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(max_changed_lines=2))
    workspace.write_file(**write_args(workspace))
    with pytest.raises(ToolFailure, match="approval required"):
        workspace.write_file(**write_args(workspace, "large.txt", "a\nb\nc\n"))
    assert not (tmp_path / "large.txt").exists()


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_large_changes_use_existing_approval_and_resume(tmp_path, name, decision):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(max_changed_lines=1))
    tools = coding_tools(workspace)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    if name == "write_file":
        args = write_args(workspace, content="一\n二\n")
        original = None
    else:
        (tmp_path / "result.txt").write_bytes(b"old\r\n")
        observed = workspace.read_file("result.txt")
        args = dict(path="result.txt", edits=[dict(old_text="old", new_text="new")],
                    expected_sha256=observed["sha256"], expected_instructions=observed["instruction_digest"])
        original = b"old\r\n"
    options = dict(tools=tools, store=store, session_id="edit-policy")
    state = run_agent(Model({"tool_calls": [tool_call(name, args)]}), "edit", **options)
    assert state.status == RunStatus.AWAITING_APPROVAL
    assert state.tool_calls == 0
    assert ((tmp_path / "result.txt").read_bytes() if original else None) == original
    request = state.metadata["approval_request"]
    store.decide_tool("edit-policy", "edit", decision, request["request_digest"])
    result = run_agent(Model({"content": "done"}), **options)
    assert result.status == RunStatus.COMPLETED
    tool_result = json.loads(result.messages[-2]["content"])
    assert tool_result["ok"] == (decision == "allow")
    if decision == "deny":
        assert ((tmp_path / "result.txt").read_bytes() if original else None) == original
        assert original is not None or not (tmp_path / "result.txt").exists()
    else:
        assert (tmp_path / "result.txt").read_bytes() == ("一\n二\n".encode() if name == "write_file" else b"new\r\n")


def test_explicit_tool_deny_overrides_threshold_approval(tmp_path):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(max_changed_lines=0))
    state = run_agent(Model({"tool_calls": [tool_call("write_file", write_args(workspace))]}, {"content": "done"}),
                      "edit", tools=coding_tools(workspace), tool_policy=ToolPolicy({"write_file": "deny"}))
    assert state.status == RunStatus.COMPLETED
    assert not (tmp_path / "result.txt").exists()


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("failure", ["exit", "timeout", "launch"])
def test_failed_checks_restore_bytes_or_remove_new_file(tmp_path, existing, failure):
    target = tmp_path / "result.txt"
    original = b"old\r\nlast"
    if existing:
        target.write_bytes(original)
    command = {
        "exit": [sys.executable, "-c", "import sys; print('check stdout'); print('check stderr', file=sys.stderr); sys.exit(3)"],
        "timeout": [sys.executable, "-c", "import time; print('before timeout', flush=True); time.sleep(10)"],
        "launch": [str(tmp_path / "nonexistent-executable")],
    }[failure]
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(
        post_edit_check=command, post_edit_check_timeout=0.2 if failure == "timeout" else 5))
    args = write_args(workspace)
    args["expected_sha256"] = workspace.file_hash("result.txt")
    result = coding_tools(workspace).execute("write_file", args)
    assert result.error.code == ErrorCode.EXECUTION_FAILED
    assert "rolled back" in result.error.message
    if failure == "exit":
        assert "check stdout" in result.error.message
        assert "check stderr" in result.error.message
    elif failure == "timeout":
        assert "before timeout" in result.error.message
    assert target.read_bytes() == original if existing else not target.exists()


def test_check_runs_in_workspace_without_command_capability(tmp_path):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(post_edit_check=[
        sys.executable, "-c", "from pathlib import Path; assert Path('result.txt').read_text() == 'one\\ntwo\\n'"
    ]))
    result = workspace.write_file(**write_args(workspace))
    assert result["sha256"] == workspace.file_hash("result.txt")


@pytest.mark.parametrize("kwargs", [dict(max_changed_lines=-1), dict(max_changed_lines=True),
    dict(allowlist=["../escape"]), dict(denylist="*.key"), dict(post_edit_check="echo nope"),
    dict(post_edit_check=[]), dict(post_edit_check_timeout=float("inf"))])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        EditPolicy(**kwargs)


def test_failed_check_rolls_back_replacement_and_keeps_observation(tmp_path):
    from baseagent.agent.state import State
    from baseagent.tools.context import ToolContext

    target = tmp_path / "result.txt"
    target.write_bytes(b"old\r\n")
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(
        post_edit_check=[sys.executable, "-c", "raise SystemExit(1)"]))
    state = State(turn_id="turn")
    context = ToolContext(state, lambda *args, **kwargs: None)
    observed = workspace.read_file("result.txt", context=context)
    before = dict(state.metadata["observed_files"]["result.txt"])
    with pytest.raises(ToolFailure, match="rolled back"):
        workspace.edit_file("result.txt", [dict(old_text="old", new_text="new")],
                            observed["sha256"], observed["instruction_digest"], context=context)
    assert target.read_bytes() == b"old\r\n"
    assert state.metadata["observed_files"]["result.txt"] == before


def test_check_holds_file_lock_and_rolls_back_on_cancellation(tmp_path, monkeypatch):
    from baseagent.kernel.cancellation import Cancelled
    from baseagent.tools.editing import file_lock

    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(post_edit_check=["configured-check"]))
    def interrupted(*args, **kwargs):
        with pytest.raises(ToolFailure, match="another harness writer"):
            with file_lock(workspace, tmp_path / "result.txt"):
                pytest.fail("transaction lock was released before check")
        raise Cancelled()
    monkeypatch.setattr(workspace, "_run_bounded_command", interrupted)
    with pytest.raises(Cancelled):
        workspace.write_file(**write_args(workspace))
    assert not (tmp_path / "result.txt").exists()


def test_policy_change_invalidates_large_edit_approval(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(max_changed_lines=0))
    args = write_args(workspace)
    state = run_agent(Model({"tool_calls": [tool_call("write_file", args)]}), "edit",
                      tools=coding_tools(workspace), store=store, session_id="change")
    store.decide_tool("change", "edit", "allow", state.metadata["approval_request"]["request_digest"])
    changed = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(max_changed_lines=1))
    with pytest.raises(ValueError):
        run_agent(Model(), tools=coding_tools(changed), store=store, session_id="change")
    state = run_agent(Model(), tools=coding_tools(changed), store=store, session_id="change", accept_config_changes=True)
    assert state.status == RunStatus.AWAITING_APPROVAL
    assert not (tmp_path / "result.txt").exists()


def test_allowlist_basename_does_not_allow_directory_descendants(tmp_path):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(allowlist=["*.py"]))
    with pytest.raises(ToolFailure, match="allowlist"):
        workspace.write_file(**write_args(workspace, "folder.py/secret.txt"))


def test_invalid_write_arguments_reach_schema_validation(tmp_path):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(max_changed_lines=0))
    args = write_args(workspace)
    args["content"] = 123
    state = run_agent(Model({"tool_calls": [tool_call("write_file", args)]}, {"content": "done"}),
                      "edit", tools=coding_tools(workspace))
    assert state.status == RunStatus.COMPLETED
    assert json.loads(state.messages[-2]["content"])["error"]["code"] == "invalid_arguments"
    assert not (tmp_path / "result.txt").exists()


def test_check_that_changes_target_does_not_leave_stale_success_metadata(tmp_path):
    workspace = Workspace(tmp_path, allow_write=True, edit_policy=EditPolicy(post_edit_check=[
        sys.executable, "-c", "from pathlib import Path; Path('result.txt').write_text('unexpected')"
    ]))
    with pytest.raises(ToolFailure, match="check modified the edited file"):
        workspace.write_file(**write_args(workspace))
    assert not (tmp_path / "result.txt").exists()
