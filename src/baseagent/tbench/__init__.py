"""Terminal-Bench 2.0 through Harbor's external-agent interface.

The agent loop stays on the host; every workspace operation and command goes through
``environment.exec`` into the task container (see ``baseagent.backends.ExecBackend``).
Harbor owns containers, ``/app``, grading, timeouts and ``result.json``. Only
``harbor_agent`` imports Harbor; everything else is testable without it.
"""

from .errors import BaseAgentInfrastructureError, BudgetExceededError

__all__ = ["BaseAgentInfrastructureError", "BudgetExceededError"]
