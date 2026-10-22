"""Offline CLI composition with persistent Chroma and the real session harness."""

import json
import socket
from unittest.mock import Mock, patch

import pytest

from applications.project_assistant.app import main
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.session import SessionStore


class ScriptedModel:
    def __init__(self, *, denied=False):
        self.calls = 0
        self.denied = denied
        self.evidence = None

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            assert any(t["function"]["name"] == "knowledge_search" for t in tools)
            assert {"git_log", "git_show", "git_pickaxe"} <= {t["function"]["name"] for t in tools}
            response = {"tool_calls": [{"id": "search-1", "type": "function", "function": {
                "name": "knowledge_search", "arguments": '{"query":"kernel"}'}}]}
        else:
            result = json.loads(messages[-1]["content"])
            self.evidence = result["data"]["evidence"]
            assert result["data"]["retrieval_mode"] == "hybrid"
            if self.denied:
                assert self.evidence == []
                response = {"content": "No visible evidence."}
            else:
                assert self.evidence
                response = {"content": "The kernel executes tools [" + self.evidence[0]["citation"] + "]."}
        return ModelResponse(response, TokenUsage(3, 2, 5))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    monkeypatch.delenv("RAG_RETRIEVAL_MODE", raising=False)
    monkeypatch.setenv("RAG_COLLECTION_NAME", "cli_test")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "mock")
    # No socket, provider constructor or paid request is allowed in this workflow.
    with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
         patch("baseagent.main.Model", side_effect=AssertionError("real model forbidden")), \
         patch("applications.project_assistant.app.OpenAIEmbedding", side_effect=AssertionError("embedding API forbidden")), \
         patch("scripts.build_knowledge_base.OpenAIEmbedding", side_effect=AssertionError("embedding API forbidden")):
        root = tmp_path / "project"
        root.mkdir()
        (root / "README.md").write_text("# Kernel\nThe kernel executes tools.\n", encoding="utf-8")
        index = root / ".baseagent" / "index"
        db = root / ".baseagent" / "sessions.sqlite3"
        yield root, index, db


def build(root, index):
    return main(["index", "--corpus-root", str(root), "--output-dir", str(index),
                 "--metadata-file", str(root / ".baseagent" / "corpus.json"), "--mock-embedding"])


def ask_args(root, index, db, *extra):
    return ["ask", "--root", str(root), "--index-dir", str(index), "--db", str(db),
            "--mock-embedding", "--session", "demo", *extra]


def test_index_status_ask_persistence_and_inspection(project, capsys):
    root, index, db = project
    assert build(root, index) == 0
    capsys.readouterr()
    assert main(["status", "--corpus-root", str(root), "--index-dir", str(index)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["matches_current_corpus"] is True
    assert status["corpus_files"] == 1
    assert status["chunk_count"] == status["corpus_chunks"] > 0
    model = ScriptedModel()
    assert main(ask_args(root, index, db, "Explain kernel"), model_factory=lambda args: model) == 0
    assert "README.md:1-2" in capsys.readouterr().out
    state, ledger = SessionStore(db).inspect_session("demo")
    assert state.status == "completed"
    assert state.model_calls == 2 and state.tool_calls == 1
    assert "knowledge_search" in json.dumps(ledger)
    assert main(ask_args(root, index, db, "Another question"),
                model_factory=lambda args: ScriptedModel()) == 0
    capsys.readouterr()
    # Session inspection remains available even after the source changes/index disappears.
    (root / "README.md").write_text("Changed corpus", encoding="utf-8")
    factory = Mock(side_effect=AssertionError("inspection must not initialize model"))
    assert main(ask_args(root, root / "missing", db, "--inspect-session"), model_factory=factory) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection["status"] == "completed" and inspection["ledger"]
    factory.assert_not_called()


def test_stale_and_missing_index_refuse_before_model(project, capsys):
    root, index, db = project
    factory = Mock(side_effect=AssertionError("model must not initialize"))
    assert main(ask_args(root, index, db, "Question"), model_factory=factory) == 1
    assert "index" in capsys.readouterr().err
    assert not index.exists()
    assert build(root, index) == 0
    (root / "README.md").write_text("# Kernel\nChanged source.\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["status", "--corpus-root", str(root), "--index-dir", str(index)]) == 0
    assert json.loads(capsys.readouterr().out)["matches_current_corpus"] is False
    assert main(ask_args(root, index, db, "Question"), model_factory=factory) == 1
    assert "corpus_version mismatch" in capsys.readouterr().err
    factory.assert_not_called()
    assert SessionStore(db).load("demo") is None
    # Only explicit ingestion repairs the binding.
    assert build(root, index) == 0
    assert main(ask_args(root, index, db, "Question"), model_factory=lambda args: ScriptedModel()) == 0


@pytest.mark.parametrize("action", ["deny", "ask"])
def test_source_visibility_respects_read_policy(project, action):
    root, index, db = project
    assert build(root, index) == 0
    policy = root / ".baseagent" / "policy.json"
    policy.write_text(json.dumps({"default": "allow", "tools": {"read_file": {
        "default": "allow", "rules": [{"action": action, "match": {"equals": {"path": "README.md"}}}]}}}))
    assert main(ask_args(root, index, db, "Question", "--tool-policy", str(policy)),
                model_factory=lambda args: ScriptedModel(denied=True)) == 0


def test_config_and_resume_share_harness_semantics(project, capsys):
    root, index, db = project
    assert build(root, index) == 0
    config = root / ".baseagent" / "harness.json"
    config.write_text(json.dumps({"version": 1, "workspace": {"root": str(root), "db": str(db)},
                                  "capabilities": {"tool_packages": []}, "run": {"max_steps": 1}}))
    args = ["ask", "--config", str(config), "--index-dir", str(index), "--mock-embedding", "--session", "demo"]
    model = ScriptedModel()
    assert main([*args, "Question"], model_factory=lambda args: model) == 1
    capsys.readouterr()
    # Explicit scalar override and the saved workspace apply on resume.
    assert main([*args, "--resume", "--max-steps", "3", "--max-model-calls", "3"], model_factory=lambda args: model) == 0
    assert "README.md:1-2" in capsys.readouterr().out


def test_status_missing_and_unversioned_index(project, capsys):
    root, index, db = project
    assert main(["status", "--corpus-root", str(root), "--index-dir", str(index)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["corpus_version"] is None and status["chunk_count"] == 0
    assert status["corpus_files"] == 1 and not status["matches_current_corpus"]
    assert not index.exists()
    assert build(root, index) == 0
    import chromadb

    collection = chromadb.PersistentClient(path=str(index)).get_collection("cli_test", embedding_function=None)
    collection.modify(metadata={"dimension": 1536})
    factory = Mock(side_effect=AssertionError("unversioned index must not initialize model"))
    assert main(ask_args(root, index, db, "Question"), model_factory=factory) == 1
    factory.assert_not_called()


def test_non_path_policy_refused_explicitly(project, capsys):
    root, index, db = project
    assert build(root, index) == 0
    policy = root / ".baseagent" / "policy.json"
    policy.write_text(json.dumps({"default": "allow", "tools": {"read_file": {
        "default": "allow", "rules": [{"action": "deny", "match": {"equals": {"start_line": 1}}}]}}}))
    factory = Mock(side_effect=AssertionError("unsafe policy must not initialize model"))
    assert main(ask_args(root, index, db, "Question", "--tool-policy", str(policy)), model_factory=factory) == 1
    assert "path-only read_file policy" in capsys.readouterr().err
    factory.assert_not_called()
