"""Offline synthetic knowledge tool loop using real Chroma retrieval and replay.

Run from a checkout: python -B scripts/demo_knowledge_search.py
All files and recordings are synthetic temporary fixtures; no model API is used.
"""

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applications.project_assistant.access import SourceAccess
from applications.project_assistant.corpus import collect_corpus, compute_corpus_version
from applications.project_assistant.rag.embedding import MockEmbedding
from applications.project_assistant.rag.indexing import VectorIndex
from applications.project_assistant.tools import knowledge_package
from baseagent.agent import run_agent
from baseagent.capabilities import assemble_capabilities
from baseagent.llm.response import ModelResponse, TokenUsage
from baseagent.testing import CassetteModel, comparable_state
from baseagent.tools.workspace import Workspace


class SyntheticModel:
    model_name = "synthetic-knowledge-demo"
    generation_params = {"temperature": 0}

    def __init__(self):
        self.responses = iter([
            {"content": None, "tool_calls": [{"id": "search-1", "type": "function", "function": {
                "name": "knowledge_search", "arguments": '{"query":"kernel tools","top_k":1}'}}]},
            {"content": "The kernel executes model and tool calls [README.md:1-2]."},
        ])

    def complete(self, messages, tools=None):
        return ModelResponse(next(self.responses), TokenUsage(3, 2, 5))


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "README.md").write_text("# Kernel\nThe kernel executes model and tool calls.", encoding="utf-8")
        (root / ".env.md").write_text("private synthetic fixture", encoding="utf-8")
        chunks = collect_corpus(root)
        version = compute_corpus_version(chunks)
        index = VectorIndex(MockEmbedding(32), collection_name="knowledge_demo")
        index.clear()
        index.add_chunks(chunks)
        workspace = Workspace(root)
        tools = assemble_capabilities(workspace, packages=["coding"], extensions=[knowledge_package(
            index, SourceAccess(workspace, ("README.md",)), corpus_version=version)])
        cassette = root / ".baseagent" / "synthetic.jsonl"
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), patch("baseagent.agent.agent.uuid4") as uuid:
            uuid.return_value.hex = "synthetic-knowledge-demo"
            with CassetteModel(cassette, SyntheticModel(), mode="record") as model:
                original = run_agent(model, "Explain the kernel", tools=tools)
            with patch.object(index, "search", wraps=index.search) as search, CassetteModel(cassette) as model:
                restored = run_agent(model, "Explain the kernel", tools=tools)
        checks = {"completed": original.status == "completed", "one_actual_tool": original.tool_calls == 1,
                  "tool_executed_during_replay": search.call_count == 1,
                  "state_matches": comparable_state(original) == comparable_state(restored),
                  "private_file_excluded": {c.file_path for c in chunks} == {"README.md"},
                  "citation_in_tool_result": "README.md:1-2" in original.messages[-2]["content"],
                  "write_still_denied": tools.execute("write_file", {
                      "path": "x.md", "content": "x", "expected_sha256": "missing",
                      "expected_instructions": workspace.get_instructions("x.md")["digest"],
                  }).error.code == "permission_denied"}
        print(json.dumps({"validation": "synthetic_offline", "checks": checks,
                          "corpus_version": version, "answer": restored.final_answer}, ensure_ascii=False, indent=2))
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
