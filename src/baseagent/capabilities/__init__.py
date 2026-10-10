"""Host-level capability composition; deliberately outside the kernel."""

from .assembly import ToolPackage, assemble_capabilities
from .skills import SkillCatalog, SkillMiddleware

__all__ = ["ToolPackage", "assemble_capabilities", "SkillCatalog", "SkillMiddleware"]
