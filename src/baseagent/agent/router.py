"""White-box router for the qa and coding workers.

The router classifies and trims context. It does not edit files or run
high-risk commands. Workers keep their own kernel, ledger and approvals.
"""

from __future__ import annotations

import re
import threading

QA = "qa"
CODING = "coding"
CLARIFY = "clarify"
WRITE_TOOLS = frozenset({"write_file", "edit_file", "run_command"})
QA_TOOLS = ("knowledge_search", "read_file", "grep", "glob")
CODING_TOOLS = ("read_file", "grep", "glob", "edit_file", "write_file", "verify_command", "update_plan")
CITATION_RE = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|md):\d+-\d+")
SPILL_RE = re.compile(r"spill[_-]?(?:id|ref)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_-]+)")


def route(prompt, *, classifier=None):
    """Classify a request. The optional classifier is off unless the caller supplies one."""
    text = (prompt or "").strip()
    if classifier is not None:
        decision = classifier(text)
        if decision.get("worker") not in {QA, CODING, CLARIFY}:
            raise ValueError("classifier must return qa, coding or clarify")
        return {**decision, "source": "classifier"}
    if not text or _vague(text):
        return _decision(CLARIFY, "ambiguous request; ask one clarifying question or stay on qa", "vague")
    if _router_self_edit(text):
        return _decision(CODING, "router refuses to edit; transfer the change to the coding worker", "router_self_edit",
                         router_action="refuse")
    if _coding(text):
        return _decision(CODING, "explicit file change and verification", "file_change_and_test")
    if _question(text):
        return _decision(QA, "code question; read-only worker must cite evidence", "code_question")
    return _decision(QA, "uncertain request defaults to read-only qa, never coding", "default_qa")


def _decision(worker, reason, rule, router_action="delegate"):
    return {"worker": worker, "reason": reason, "rule": rule, "router_action": router_action, "source": "rules"}


def _coding(text):
    lowered = text.lower()
    change = any(word in lowered for word in ("fix ", "edit ", "change ", "modify ", "patch ", "改", "修改", "修复"))
    verify = any(word in lowered for word in ("test", "verify", "pytest", "unittest", "跑测试", "验证"))
    named_file = bool(re.search(r"\b[\w./-]+\.(?:py|md)\b", text))
    return change and (verify or named_file)


def _question(text):
    lowered = text.lower()
    return any(word in lowered for word in ("how ", "what ", "why ", "where ", "怎么", "如何", "为什么", "哪里", "防越权"))


def _vague(text):
    lowered = text.lower().strip(" ?.!")
    return lowered in {"hi", "hello", "help", "帮我", "看看", "do something", "改一下"} or len(lowered) < 4


def _router_self_edit(text):
    lowered = text.lower()
    return ("router" in lowered or "你自己" in text) and any(word in lowered or word in text for word in ("edit", "改", "write", "写"))


def router_tool_names():
    """The router may only delegate. Write and command tools are absent on purpose."""
    return ("run_subtask",)


def worker_tool_names(worker):
    if worker == QA:
        return QA_TOOLS
    if worker == CODING:
        return CODING_TOOLS
    raise ValueError("unknown worker")


def handoff(worker, answer, messages):
    """Return a summary and references. Full tool bodies stay in the worker ledger."""
    citations = []
    call_ids = []
    spill_refs = []
    for message in messages:
        if message.get("role") == "assistant":
            for item in message.get("tool_calls") or []:
                if item.get("id"):
                    call_ids.append(item["id"])
        blob = message.get("content") or ""
        for citation in CITATION_RE.findall(blob):
            if citation not in citations:
                citations.append(citation)
        for spill in SPILL_RE.findall(blob):
            if spill not in spill_refs:
                spill_refs.append(spill)
    for citation in CITATION_RE.findall(answer or ""):
        if citation not in citations:
            citations.append(citation)
    summary = " ".join((answer or "").split())
    if len(summary) > 500:
        summary = summary[:500] + "…"
    return {"worker": worker, "summary": summary, "citations": citations,
            "call_ids": call_ids, "spill_refs": spill_refs}


def isolation_report(full_tool_outputs, handoff_payload):
    """Compare router context with the baseline that inlines every tool body."""
    baseline = "\n".join(full_tool_outputs)
    routed = handoff_payload["summary"] + "\n" + " ".join(handoff_payload["citations"])
    baseline_tokens = _tokens(baseline)
    router_tokens = _tokens(routed)
    return {"baseline_tokens": baseline_tokens, "router_tokens": router_tokens,
            "router_fraction": router_tokens / baseline_tokens if baseline_tokens else None}


def _tokens(text):
    return max(1, (len(text.encode("utf-8")) + 3) // 4) if text else 0


class SharedBudget:
    """Atomic reservation across workers. ExecutionScope still enforces dispatch."""

    def __init__(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("shared budget must be a positive integer")
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def reserve(self, amount):
        if type(amount) is not int or amount < 1:
            raise ValueError("reservation must be a positive integer")
        with self._lock:
            if self.used + amount > self.limit:
                return False
            self.used += amount
            return True

    @property
    def remaining(self):
        with self._lock:
            return self.limit - self.used
