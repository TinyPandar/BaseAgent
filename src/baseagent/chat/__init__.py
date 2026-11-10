"""Frontend-independent chat layer shared by the CLI, the Textual TUI and the Web UI.

Kernel -> ``ChatSession`` (commands + ``EventBuffer``) -> CLI / TUI / Web. See docs/ui.md.
"""

from .events import KINDS, ChatEvent, EventBuffer
from .middleware import ToolEventMiddleware
from .session import ANY, ApprovalMismatch, ChatSession

__all__ = ["ANY", "ApprovalMismatch", "ChatEvent", "ChatSession", "EventBuffer", "KINDS", "ToolEventMiddleware"]
