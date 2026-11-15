"""Harbor environments that fail on purpose, for infrastructure-failure checks without Docker.

``harbor run -c CONFIG`` with ``environment.import_path`` set to one of these runs a real Harbor
trial whose result.json records the failure; no container and no model are involved.
"""

from harbor.environments.base import BaseEnvironment, ExecResult


class _Broken(BaseEnvironment):
    def _validate_definition(self):
        return None

    async def stop(self, delete: bool):
        return None

    async def upload_file(self, source_path, target_path):
        raise RuntimeError("simulated backend failure: upload")

    async def upload_dir(self, source_dir, target_dir):
        raise RuntimeError("simulated backend failure: upload")

    async def download_file(self, source_path, target_path):
        raise RuntimeError("simulated backend failure: download")

    async def download_dir(self, source_dir, target_dir):
        raise RuntimeError("simulated backend failure: download")


class MissingImageEnvironment(_Broken):
    """Fails to start the way a missing image does."""

    @staticmethod
    def type() -> str:
        return "baseagent-missing-image"

    async def start(self, force_build: bool) -> None:
        raise RuntimeError("pull access denied for baseagent/does-not-exist:missing; repository does not exist")

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None) -> ExecResult:
        raise RuntimeError("simulated backend failure: not started")


class FailingExecEnvironment(_Broken):
    """Starts, then every exec raises: the backend fails while the agent runs."""

    @staticmethod
    def type() -> str:
        return "baseagent-failing-exec"

    async def start(self, force_build: bool) -> None:
        return None

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None) -> ExecResult:
        raise RuntimeError("simulated backend failure: container connection lost")
