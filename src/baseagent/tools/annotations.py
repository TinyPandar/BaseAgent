"""Annotation presets for built-in tools (see docs/tool_annotations.md).

``destructive`` means the tool may delete or overwrite data outside the session's own
state: workspace files, processes, the workspace-shared memory store or external
systems. Session-local bookkeeping (plan, notes, history summary) is not destructive.
"""

from baseagent.kernel.tools import ANNOTATION_KEYS, RETRY_SAFE_ANNOTATIONS, ToolAnnotations, mcp_annotation_hints

# Reads the workspace, session or a local corpus; no side effect.
READ = ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)
# Changes session-local state only, guarded by an expected revision.
SESSION_STATE = ToolAnnotations(read_only=False, destructive=False, idempotent=True, open_world=False)
# Overwrites or deletes workspace data, guarded by expected hashes/revisions (a repeat fails).
OVERWRITE = ToolAnnotations(read_only=False, destructive=True, idempotent=True, open_world=False)
# Runs an arbitrary program: anything may happen, including network access.
COMMAND = ToolAnnotations(read_only=False, destructive=True, idempotent=False, open_world=True)
# Starts child agents whose own tool calls are authorized individually.
DELEGATION = ToolAnnotations(read_only=False, destructive=False, idempotent=False, open_world=False)

__all__ = ["ANNOTATION_KEYS", "COMMAND", "DELEGATION", "OVERWRITE", "READ", "RETRY_SAFE_ANNOTATIONS",
           "SESSION_STATE", "ToolAnnotations", "mcp_annotation_hints"]
