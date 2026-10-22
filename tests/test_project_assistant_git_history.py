"""Offline Git tools and the real assistant harness with a scripted model."""

import io
import json
import os
import socket
import subprocess
from unittest.mock import Mock, patch

import pytest

from applications.project_assistant.app import main
from applications.project_assistant import git_history as module
from applications.project_assistant.git_history import GitHistory, git_history_package
from baseagent.capabilities import assemble_capabilities
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace


@pytest.fixture(autouse=True)
def offline():
    with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
         patch("baseagent.main.Model", side_effect=AssertionError("paid model forbidden")), \
         patch("applications.project_assistant.app.OpenAIEmbedding", side_effect=AssertionError("paid embedding forbidden")):
        yield


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args, date=None):
        dated_env = {**env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else env
        return subprocess.run([module.GIT_BINARY, "-C", str(root), "-c", "user.name=Offline Tester",
                               "-c", "user.email=offline@example.invalid", "-c", "commit.gpgsign=false",
                               *args], env=dated_env, check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    git("init", "-q")
    (root / "README.md").write_text("# Project\nInitial baseline.\n")
    git("add", ".")
    git("commit", "-qm", "Initial baseline", date="2025-01-01T12:00:00+00:00")
    first = git("rev-parse", "HEAD")
    (root / "feature.py").write_text("def recover_progress():\n    return 'history'\n")
    git("add", ".")
    git("commit", "-qm", "Add progress recovery", date="2025-02-01T12:00:00+00:00")
    added = git("rev-parse", "HEAD")
    (root / "README.md").write_text("# Project\nDocument progress recovery.\n")
    git("add", ".")
    git("commit", "-qm", "Document recent changes", date="2025-03-01T12:00:00+00:00")
    return root, git, first, added


def test_log_filters_and_show_stat(repo):
    root, git, first, added = repo
    history = GitHistory(root)
    result = history.git_log()
    assert result["reference_only"] and not result["truncated"]
    assert result["output"].index(added) < result["output"].index(first)
    filtered = history.git_log(since="2025-01-15", until="2025-02-15", author="Offline Tester",
                               path="feature.py", grep="progress recovery")["output"]
    assert added in filtered and first not in filtered
    assert history.git_log(author="Nobody")["output"] == ""
    assert history.git_log(grep="Add.*recovery")["output"] == ""  # literal message
    show = history.git_show(added)["output"]
    assert added in show and "Add progress recovery" in show and "feature.py" in show
    assert "1 file changed" in show and "diff --git" not in show
    assert "def recover_progress" not in show
    assert git("status", "--porcelain") == ""


def test_pickaxe_literal_regex_and_path(repo):
    root, _, first, added = repo
    history = GitHistory(root)
    for result in (history.git_pickaxe("recover_progress"),
                   history.git_pickaxe(r"def recover_.*", regex=True, path="feature.py")):
        assert added in result["output"] and first not in result["output"]
        assert "diff --git" not in result["output"]
    assert history.git_pickaxe("recover_progress", path="README.md")["output"] == ""
    assert history.git_pickaxe("not present")["output"] == ""


@pytest.mark.parametrize("name,args", [
    ("git_show", {"rev": "--output=/tmp/escape"}),
    ("git_show", {"rev": "HEAD:README.md"}),
    ("git_show", {"rev": "HEAD..main"}),
    ("git_show", {"rev": "HEAD;touch injected"}),
    ("git_log", {"author": "--all"}),
    ("git_log", {"since": "-1"}),
    ("git_log", {"until": "--output=escape"}),
    ("git_log", {"grep": "--all"}),
    ("git_pickaxe", {"text": "--output=escape"}),
    ("git_pickaxe", {"text": "x\n--all"}),
    ("git_log", {"path": "../outside"}),
    ("git_show", {"path": "/tmp"}),
    ("git_pickaxe", {"text": "x", "path": "--all"}),
    ("git_log", {"max_count": 0}),
    ("git_log", {"max_count": True}),
])
def test_unsafe_inputs_rejected_before_git(repo, name, args):
    history = GitHistory(repo[0])
    with patch.object(history, "_run", side_effect=AssertionError("invalid input ran git")):
        with pytest.raises(ToolFailure) as error:
            getattr(history, name)(**args)
    assert error.value.code == ErrorCode.INVALID_ARGUMENTS


def test_path_symlink_escape_and_literal_pathspec(repo, tmp_path):
    root, git, _, _ = repo
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ToolFailure):
        GitHistory(root).git_log(path="escape/outside")
    # Magic pathspec syntax is interpreted literally and cannot broaden the search.
    assert GitHistory(root).git_log(path=":(glob)**")["output"] == ""
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(ToolFailure) as error:
        GitHistory(nested).git_log()
    assert error.value.code == ErrorCode.PERMISSION_DENIED


def test_count_cap_and_output_truncation(repo):
    root, git, _, _ = repo
    history = GitHistory(root)
    with patch.object(history, "_execute", return_value={}) as execute:
        history.git_log(max_count=5000)
        assert "--max-count=50" in execute.call_args.args[0]
        history.git_pickaxe("recover", max_count=5000)
        assert "--max-count=50" in execute.call_args.args[0]
    message = root / ".git" / "large-message"
    message.write_text("Large commit\n\n" + "x" * (module.OUTPUT_LIMIT * 3))
    git("commit", "--allow-empty", "-q", "-F", str(message))
    for result in (history.git_log(), history.git_show()):
        assert result["truncated"]
        assert result["output"].endswith(module.TRUNCATION_MARKER)
        assert len(result["output"].encode()) <= module.OUTPUT_LIMIT


def test_environment_and_helpers_cannot_redirect_or_execute(repo, monkeypatch):
    root, git, first, _ = repo
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.log")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!false")
    git("config", "core.fsmonitor", "false")
    git("config", "diff.external", "false")
    git("config", "log.showSignature", "true")
    with patch.object(module.subprocess, "Popen", wraps=subprocess.Popen) as popen:
        assert first in GitHistory(root).git_show(first)["output"]
    argv = popen.call_args.args[0]
    assert argv[0] == "/usr/bin/git" and "--no-pager" in argv
    assert "--no-ext-diff" in argv and "--no-textconv" in argv
    assert not popen.call_args.kwargs.get("shell", False)
    env = popen.call_args.kwargs["env"]
    assert "GIT_DIR" not in env and "GIT_CONFIG_COUNT" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_OPTIONAL_LOCKS"] == "0"


def test_timeout_kills_process(repo):
    process = Mock(stdout=io.BytesIO(b""))
    process.wait.side_effect = [subprocess.TimeoutExpired("git", 10), 0]
    process.__enter__ = Mock(return_value=process)
    process.__exit__ = Mock(return_value=False)
    with patch.object(module.subprocess, "Popen", return_value=process):
        with pytest.raises(ToolFailure) as error:
            GitHistory(repo[0]).git_log()
    assert error.value.code == ErrorCode.TIMEOUT
    process.kill.assert_called_once()


def test_cli_no_index_or_model_needed(repo, capsys):
    root, _, _, added = repo
    for argv in (["log", "--grep", "Add progress"], ["show", added],
                 ["added", "recover_progress"], ["added", "def recover_.*", "--regex"]):
        assert main(["history", *argv, "--root", str(root)]) == 0
        assert added in json.loads(capsys.readouterr().out)["output"]
    assert main(["history", "show", "HEAD:README.md", "--root", str(root)]) == 1
    assert "invalid revision" in capsys.readouterr().err
    assert main(["history", "log", "--root", str(root / "absent")]) == 1
    assert "git history failed" in capsys.readouterr().err


class HistoryModel:
    def __init__(self, added):
        self.added = added
        self.step = 0

    def complete(self, messages, tools):
        names = {tool["function"]["name"] for tool in tools}
        assert {"git_log", "git_show", "git_pickaxe"} <= names
        assert "knowledge_search" not in names
        assert "cite full commit hashes" in messages[0]["content"]
        calls = [("git_log", {}), ("git_show", {"rev": self.added}),
                 ("git_pickaxe", {"text": "recover_progress"})]
        if self.step:
            result = json.loads(messages[-1]["content"])
            assert result["ok"] and self.added in result["data"]["output"]
        if self.step < len(calls):
            name, arguments = calls[self.step]
            response = {"tool_calls": [{"id": f"history-{self.step}", "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        else:
            response = {"content": f"Progress recovery was added in commit {self.added}."}
        self.step += 1
        return ModelResponse(response, TokenUsage(3, 2, 5))


def test_ask_history_tools_with_scripted_model(repo, tmp_path, capsys):
    root, git, _, added = repo
    before = git("status", "--porcelain")
    db = tmp_path / "sessions.sqlite3"
    model = HistoryModel(added)
    assert main(["ask", "What is done, what changed recently, which commit added recover_progress?",
                 "--history-only", "--root", str(root), "--db", str(db), "--session", "history"],
                model_factory=lambda args: model) == 0
    assert added in capsys.readouterr().out
    state, ledger = SessionStore(db).inspect_session("history")
    assert state.status == "completed" and state.tool_calls == 3
    assert all(json.loads(row["result_json"])["ok"] for row in ledger)
    assert git("status", "--porcelain") == before
    assert not (root / ".baseagent" / "rag_index").exists()


def test_package_registration_does_not_grant_write_permissions(repo):
    workspace = Workspace(repo[0])
    registry = assemble_capabilities(workspace, packages=[], extensions=[git_history_package(repo[0])])
    assert registry.execute("git_log", {}).ok
    assert registry.retry_safe("git_log")
    assert not registry.execute("git_show", {"rev": "--all"}).ok
    assert not workspace.allow_write and not workspace.allow_command
    assert registry.capability_contract["extensions"][0]["name"] == "git-history"
