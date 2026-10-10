"""Compose built-in and application tool packages without granting permissions."""

from dataclasses import dataclass
from typing import Callable

from baseagent.kernel import ToolRegistry
from baseagent.tools.workspace import coding_tools


BUILTIN_PACKAGES = {
    "coding": ("get_instructions", "read_file", "search_files", "write_file", "edit_file", "run_command", "update_plan", "get_task_state", "verify_command", "git_diff"),
    "memory": ("remember", "forget_memory", "recall_memory", "read_history", "set_history_summary"),
    "shared-memory": ("publish_memory", "search_shared_memory", "withdraw_memory"),
}


@dataclass(frozen=True)
class ToolPackage:
    name: str
    version: str
    install: Callable[[ToolRegistry], None]


def assemble_capabilities(workspace, *, packages=None, extensions=(), skills=None):
    selected = tuple(BUILTIN_PACKAGES if packages is None else packages)
    if len(set(selected)) != len(selected) or any(name not in BUILTIN_PACKAGES for name in selected):
        raise ValueError("unknown or duplicate built-in tool package")
    registry = coding_tools(workspace).subset([tool for name in selected for tool in BUILTIN_PACKAGES[name]])
    contract = {**registry.capability_contract, "tool_packages": list(selected), "extensions": []}
    seen = set(selected)
    for package in extensions:
        if not package.name or not package.version or package.name in seen:
            raise ValueError("tool packages require unique names and nonempty versions")
        seen.add(package.name)
        package.install(registry)
        contract["extensions"].append({"name": package.name, "version": package.version})
    if skills is not None:
        skills.install(registry)
        contract["skills"] = skills.contract()
    registry.capability_contract = contract
    return registry
