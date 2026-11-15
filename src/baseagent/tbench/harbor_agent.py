"""Harbor external agent: ``--agent-import-path baseagent.tbench.harbor_agent:BaseAgentHarborAgent``.

Pinned to Harbor 0.26.0 (``HARBOR_VERSION``). Nothing is installed into the task container
and nothing is written under its working directory except what the model's tools write:
the loop runs in the Harbor process on the host and reaches the container only through
``environment.exec``. Provider credentials are read from the host environment
(``BASEAGENT_LLM_*`` or ``DEEPSEEK_*``) and never passed to the container.

Agent kwargs (``--ak key=value`` or ``agents[].kwargs`` in a job config):

* ``scripted`` - ``correct`` or ``wrong``: offline fixture models, zero tokens, no provider.
* ``budget_cny`` - required for a real model: per-task CNY cap; a stop raises
  ``BudgetExceededError``.
* ``max_command_timeout_sec`` - per-command upper bound (default 60).
* ``max_steps``, ``max_model_calls``, ``max_tool_calls`` - loop limits.
* ``agent_timeout_sec`` - the task's agent wall clock when it cannot be read from the trial.
* ``input_cny_per_million``, ``output_cny_per_million`` - pricing for the CNY budget
  (default: the existing metering rates in ``baseagent.agent.workers``).
* ``usd_per_cny`` - explicit exchange factor; without it ``cost_usd`` stays empty.
"""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from harbor.agents.base import BaseAgent as HarborBaseAgent

from .errors import BaseAgentInfrastructureError
from .runner import RunConfig, run_in_environment
from .settings import HARBOR_VERSION, AgentSettings


def _check_harbor_version():
    try:
        installed = version("harbor")
    except PackageNotFoundError:
        installed = None
    if installed != HARBOR_VERSION:
        raise RuntimeError(f"BaseAgent's Harbor adapter is pinned to harbor=={HARBOR_VERSION} (found {installed}); "
                           "rerun the acceptance checks in docs/terminal_bench.md before changing the pin")


def agent_timeout_from_trial(logs_dir: Path) -> float | None:
    """The agent wall clock Harbor 0.26.0 applies to this trial, read from its own records.

    Harbor does not pass the timeout to external agents. The trial directory holds the
    resolved ``config.json``; the task's ``task.toml`` gives the base timeout. The formula
    mirrors Harbor 0.26.0 ``Trial._resolve_timeout_sec``: min(base, max) * multiplier.
    """
    try:
        from harbor.models.task.task import Task
        from harbor.models.trial.config import TrialConfig
        config = TrialConfig.model_validate(json.loads((Path(logs_dir).parent / "config.json").read_text("utf-8")))
        base = config.agent.override_timeout_sec
        if base is None:
            base = Task(config.task.get_task_id().get_local_path()).config.agent.timeout_sec
        if base is None:
            return None
        multiplier = (config.agent_timeout_multiplier if config.agent_timeout_multiplier is not None
                      else config.timeout_multiplier)
        return min(base, config.agent.max_timeout_sec or float("inf")) * multiplier
    except Exception:  # noqa: BLE001 -- best effort; the caller records an unknown deadline
        return None


class BaseAgentHarborAgent(HarborBaseAgent):
    @staticmethod
    def name() -> str:
        return "baseagent"

    def version(self) -> str | None:
        try:
            return version("baseagent")
        except PackageNotFoundError:
            return "unknown"

    def __init__(self, logs_dir: Path, model_name: str | None = None, *args, **kwargs):
        own = {key: kwargs.pop(key) for key in list(kwargs) if key in AgentSettings.FIELDS}
        super().__init__(logs_dir, model_name, *args, **kwargs)
        _check_harbor_version()
        self.settings = AgentSettings.from_kwargs(own)

    @classmethod
    def preflight(cls, kwargs=None, env=None) -> None:
        super().preflight(kwargs, env)
        _check_harbor_version()
        AgentSettings.from_kwargs({key: value for key, value in (kwargs or {}).items() if key in AgentSettings.FIELDS})

    async def setup(self, environment) -> None:
        # Nothing is installed into the task container.
        return None

    def _model(self):
        if self.settings.scripted:
            from .scripted import ScriptedFixtureModel
            return ScriptedFixtureModel(self.settings.scripted)
        try:
            from baseagent.main import initialize_model
            return initialize_model(self.model_name or None)
        except Exception as exc:  # noqa: BLE001
            raise BaseAgentInfrastructureError(f"model configuration failed: {type(exc).__name__}: {exc}") from None

    async def run(self, instruction, environment, context) -> None:
        settings = self.settings
        timeout, source = settings.agent_timeout_sec, "agent_kwarg"
        if timeout is None:
            timeout = agent_timeout_from_trial(self.logs_dir)
            source = "trial_config" if timeout is not None else "unknown"
        config = RunConfig(max_steps=settings.max_steps, max_model_calls=settings.max_model_calls,
                           max_tool_calls=settings.max_tool_calls,
                           max_command_timeout_sec=settings.max_command_timeout_sec,
                           agent_timeout_sec=timeout, deadline_source=source, budget_cny=settings.budget_cny,
                           input_cny_per_million=settings.input_cny_per_million,
                           output_cny_per_million=settings.output_cny_per_million,
                           usd_per_cny=settings.usd_per_cny, provider_calls_are_billed=not settings.scripted,
                           extra_metadata={"model": "scripted:" + settings.scripted if settings.scripted
                                           else (self.model_name or os.getenv("BASEAGENT_LLM_MODEL")
                                                 or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"),
                                           "harbor_version": HARBOR_VERSION})
        await run_in_environment(instruction, environment, context, model=self._model(), config=config,
                                 logs_dir=self.logs_dir)
