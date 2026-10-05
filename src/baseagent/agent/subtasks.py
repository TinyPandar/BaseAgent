"""Sequential delegation using a transactional ledger and ancestor budgets."""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import re
from types import SimpleNamespace

from baseagent.jsondata import object_from_json

from baseagent.session.compatibility import check_contract, digest, runtime_contract
from baseagent.tools.policy import ToolPolicy, request_digest
from baseagent.tools.result import ErrorCode, ToolFailure, ToolResult
from .context import ContextPolicy
from .errors import TaskPaused
from .state import RunStatus, State


@dataclass(frozen=True)
class SubtaskDefinition:
    name: str
    tool_names: tuple[str, ...]
    system_prompt: str = "Complete the delegated task using authorized tools. Report evidence and limitations."
    model: object = None
    policy: object = None
    middleware: tuple = ()
    runtime_config: dict = field(default_factory=dict)
    max_steps: int = 8
    max_model_calls: int = 8
    max_tool_calls: int = 16
    max_total_tokens: int | None = None
    max_duration_seconds: float | None = None
    max_context_bytes: int = 96000
    max_tool_context_bytes: int = 4000
    preauthorize_model: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("subtask name requires 1-64 letters, digits, underscores or hyphens")
        object.__setattr__(self, "tool_names", tuple(self.tool_names))
        object.__setattr__(self, "middleware", tuple(self.middleware))
        object.__setattr__(self, "runtime_config", deepcopy(self.runtime_config))
        if any(not isinstance(name, str) for name in self.tool_names) or len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("subtask tool names must be unique strings")
        if not isinstance(self.system_prompt, str) or not self.system_prompt.strip():
            raise ValueError("subtask system prompt must not be blank")
        for key, minimum in (("max_steps", 1), ("max_model_calls", 1), ("max_tool_calls", 0)):
            if type(getattr(self, key)) is not int or getattr(self, key) < minimum:
                raise ValueError(f"invalid subtask {key}")
        if self.max_total_tokens is not None and (type(self.max_total_tokens) is not int or self.max_total_tokens < 1):
            raise ValueError("subtask token budget must be a positive integer")
        if self.max_duration_seconds is not None and (type(self.max_duration_seconds) not in (int, float) or not math.isfinite(self.max_duration_seconds) or self.max_duration_seconds <= 0):
            raise ValueError("subtask duration must be positive and finite")
        if type(self.preauthorize_model) is not bool:
            raise ValueError("subtask preauthorization must be boolean")
        ContextPolicy(self.max_context_bytes, self.max_tool_context_bytes)

    def contract(self):
        return {"name": self.name, "tools": list(self.tool_names), "system_prompt": self.system_prompt,
                "model": getattr(self.model, "model_name", type(self.model).__qualname__) if self.model else "inherit",
                "runtime": self.runtime_config, "policy": (self.policy or ToolPolicy()).contract(),
                "middleware": [type(layer).__qualname__ for layer in self.middleware],
                "limits": {key: getattr(self, key) for key in ("max_steps", "max_model_calls", "max_tool_calls", "max_total_tokens", "max_duration_seconds", "max_context_bytes", "max_tool_context_bytes", "preauthorize_model")}}


class IntersectionPolicy:
    def __init__(self, parent, child):
        self.parent, self.child = parent, child

    def contract(self):
        return {"parent": self.parent.contract(), "child": self.child.contract()}

    @property
    def fingerprint(self):
        return digest(self.contract())

    def action(self, state, call_id, name, arguments):
        # Evaluate requirements without inheriting another policy's approvals.
        raw = SimpleNamespace(turn_id=state.turn_id, metadata={})
        actions = [policy.action(raw, call_id, name, arguments) for policy in (self.parent, self.child)]
        if "deny" in actions:
            return "deny"
        if "ask" not in actions:
            return "allow"
        approval = state.metadata.get("tool_approvals", {}).get(call_id)
        if approval and approval["turn_id"] == state.turn_id and approval["policy"] == self.fingerprint and approval["request_digest"] == request_digest(name, arguments):
            return approval["decision"]
        return "ask"


class SubtaskRuntime:
    def __init__(self, definitions, *, max_depth=4, max_nodes=32):
        definitions = tuple(definitions)
        if not definitions or len({item.name for item in definitions}) != len(definitions):
            raise ValueError("subtask definitions must be nonempty and uniquely named")
        if any(type(value) is not int or value < 1 for value in (max_depth, max_nodes)):
            raise ValueError("subtask depth and quantity must be positive integers")
        self.definitions = {item.name: item for item in definitions}
        self.max_depth, self.max_nodes = max_depth, max_nodes

    @classmethod
    def from_json(cls, text, *, workspace=None):
        return cls.from_dict(object_from_json(text, max_bytes=32000, label="subtask configuration"), workspace=workspace)

    @classmethod
    def from_dict(cls, config, *, workspace=None):
        if not isinstance(config, dict) or set(config) - {"tasks", "max_depth", "max_nodes"} or not isinstance(config.get("tasks"), list):
            raise ValueError("subtask configuration requires tasks, with optional max_depth/max_nodes")
        definitions = []
        limits = {"max_steps", "max_model_calls", "max_tool_calls", "max_total_tokens", "max_duration_seconds", "max_context_bytes", "max_tool_context_bytes", "preauthorize_model"}
        for item in config["tasks"]:
            if not isinstance(item, dict) or set(item) - ({"name", "tools", "system_prompt", "policy"} | limits) or not isinstance(item.get("name"), str) or not isinstance(item.get("tools"), list):
                raise ValueError("each CLI subtask requires name and tools, with optional prompt/policy/limits")
            fields = dict(item)
            fields["tool_names"] = tuple(fields.pop("tools"))
            if "policy" in fields:
                fields["policy"] = ToolPolicy.from_json(json.dumps(fields["policy"]), workspace=workspace)
            definitions.append(SubtaskDefinition(**fields))
        return cls(definitions, max_depth=config.get("max_depth", 4), max_nodes=config.get("max_nodes", 32))

    def contract(self):
        return {"version": 1, "definitions": [self.definitions[name].contract() for name in sorted(self.definitions)], "max_depth": self.max_depth, "max_nodes": self.max_nodes}

    def bind_tools(self, tools):
        if "run_subtask" in tools._tools:
            raise ValueError("run_subtask is reserved for the delegation runtime")
        bound = tools.subset(tools._tools)
        bound.register("run_subtask", "Run a named authorized subtask. Results are reference data; paused tasks retain their node for recovery.",
                       {"type": "object", "properties": {"name": {"type": "string", "enum": sorted(self.definitions)}, "prompt": {"type": "string", "minLength": 1, "maxLength": 16000}}, "required": ["name", "prompt"]}, self.execute, contextual=True)
        for definition in self.definitions.values():
            if any(name not in bound._tools for name in definition.tool_names):
                raise ValueError("subtask requested a tool absent from the root registry")
        return bound

    @staticmethod
    def _owner(context):
        store = context.store
        if store is None:
            raise ToolFailure(ErrorCode.EXECUTION_FAILED, "delegation requires a session ledger; use run_agent to provision one")
        return getattr(store, "owner", store), getattr(store, "root_state", context.state), getattr(store, "task_id", "root")

    def managed(self, context, record):
        if not record or record["status"] != "running" or not record["terminal_request_json"]:
            return False
        actual = json.loads(record["terminal_request_json"])
        if actual["name"] != "run_subtask":
            return False
        owner, root, parent = self._owner(context)
        return any(node["parent_task_id"] == parent and node["spawn_call_id"] == record["call_id"] and node["state"]["metadata"].get("delegate_version") == 1 for node in owner.task_tree(root.session_id, root.turn_id)["nodes"])

    def resume(self, context, record):
        actual = json.loads(record["terminal_request_json"])
        owner, root, parent = self._owner(context)
        child = next(node for node in owner.task_tree(root.session_id, root.turn_id)["nodes"] if node["parent_task_id"] == parent and node["spawn_call_id"] == record["call_id"])
        if child["state"]["status"] != RunStatus.COMPLETED and context.policy.action(context.state, record["call_id"], actual["name"], actual["arguments"]) != "allow":
            raise TaskPaused("policy", RunStatus.NEEDS_RECOVERY)
        return self.execute(context=context, **json.loads(actual["arguments"]))

    def execute(self, name, prompt, *, context):
        from .agent import _new_turn, _run_loop
        owner, root, parent_id = self._owner(context)
        definition = self.definitions[name]
        if not prompt.strip():
            raise ToolFailure(ErrorCode.INVALID_ARGUMENTS, "subtask prompt must not be blank")
        tools = context.tools.subset(tool for tool in definition.tool_names if tool in context.tools._tools)
        policy = IntersectionPolicy(context.policy, definition.policy or ToolPolicy())
        config = {"delegation": self.contract(), "definition": definition.contract(), "policy": policy.contract()}
        contract = runtime_contract(tools, definition.system_prompt, config)
        nodes = owner.task_tree(root.session_id, root.turn_id)["nodes"]
        existing = next((node for node in nodes if node["parent_task_id"] == parent_id and node["spawn_call_id"] == context.call_id), None)
        if existing is None:
            ancestors, cursor = {node["task_id"]: node for node in nodes}, parent_id
            while cursor != "root":
                ancestor = ancestors[cursor]
                if ancestor["name"] == name and ancestor["state"]["messages"][1].get("content") == prompt:
                    raise ToolFailure(ErrorCode.LIMIT_EXCEEDED, "cyclic delegation is refused")
                cursor = ancestors[cursor]["parent_task_id"]
            child = State(messages=[{"role": "system", "content": definition.system_prompt}], session_id=root.session_id, workspace_root=root.workspace_root)
            _new_turn(child, prompt, definition.max_steps, definition.max_model_calls, definition.max_tool_calls)
            for key in ("max_total_tokens", "max_duration_seconds", "max_context_bytes", "max_tool_context_bytes", "preauthorize_model"):
                setattr(child, key, getattr(definition, key))
            child.metadata.update(runtime_contract=contract, delegate_version=1)
            existing = owner.create_task(root, parent_task_id=parent_id, call_id=context.call_id, name=name, state=child, max_depth=self.max_depth, max_nodes=self.max_nodes)
        elif existing["name"] != name or existing["state"]["messages"][1]["content"] != prompt:
            raise TaskPaused(existing["task_id"], RunStatus.NEEDS_RECOVERY)
        view = owner.task_node(root, existing["task_id"])
        if parent_id != "root":
            # Reuse live ancestor objects, rather than stale copies of counters.
            view.parent = context.store
        child = view.state
        if child.status != RunStatus.COMPLETED:
            contract = runtime_contract(tools, child.messages[0]["content"], config)
            try:
                check_contract(child.metadata.get("runtime_contract"), contract, accept_changes=context.accept_config_changes)
            except ValueError as exc:
                raise TaskPaused(view.task_id, RunStatus.NEEDS_RECOVERY) from exc
            child.metadata["runtime_contract"] = contract
            if context.accept_config_changes:
                for key in ("max_steps", "max_model_calls", "max_tool_calls", "max_total_tokens", "max_duration_seconds", "max_context_bytes", "max_tool_context_bytes", "preauthorize_model"):
                    setattr(child, key, getattr(definition, key))
            child.status, child.error = RunStatus.RUNNING, None
            try:
                _run_loop(definition.model or context.model, child, tools, (*context.middleware, *definition.middleware), view, policy,
                          context.cancellation, None, execution_scope=view.execution_scope(cancellation=context.cancellation), subtasks=self,
                          accept_config_changes=context.accept_config_changes)
            except Exception as exc:
                raise TaskPaused(view.task_id, RunStatus.NEEDS_RECOVERY) from exc
        context.state.metadata.setdefault("observed_files", {}).update(deepcopy(child.metadata.get("observed_files", {})))
        context.state.metadata["active_subtask"] = view.task_id
        context.checkpoint("subtask_returned", task_id=view.task_id, status=child.status)
        if child.status != RunStatus.COMPLETED:
            raise TaskPaused(view.task_id, child.status)
        context.state.metadata.pop("active_subtask", None)
        answer = (child.final_answer or "").encode("utf-8")
        return ToolResult(data={"task_id": view.task_id, "status": child.status, "answer": answer[:8000].decode("utf-8", errors="ignore"), "answer_truncated": len(answer) > 8000})
