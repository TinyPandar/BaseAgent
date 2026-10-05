"""Workspace-bound access to explicitly published session references."""

from pathlib import Path

from .workflow import _state
from .result import ErrorCode, ToolFailure


def _scope(workspace, context, *, publish=False):
    state = _state(context)
    if context.store is None or not context.store.durable or not state.session_id or not state.workspace_root or Path(state.workspace_root).resolve() != workspace.root:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "shared memory requires a persistent session bound to this workspace")
    if publish and not workspace.allow_memory_publish:
        raise ToolFailure(ErrorCode.PERMISSION_DENIED, "shared memory publication requires --allow-memory-publish")
    return state, context.store


def publish_memory(workspace, key, expected_revision, *, context):
    state, store = _scope(workspace, context, publish=True)
    return store.publish_memory(state, workspace.root, key, expected_revision)


def withdraw_memory(workspace, key, expected_revision, *, context):
    state, store = _scope(workspace, context, publish=True)
    return store.withdraw_memory(state, workspace.root, key, expected_revision)


def search_shared_memory(workspace, query="", after_key="", limit=10, *, context):
    _, store = _scope(workspace, context)
    return store.search_shared_memory(workspace.root, query=query, after_key=after_key, limit=limit)
