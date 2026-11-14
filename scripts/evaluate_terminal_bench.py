"""Score a local Terminal-Bench fixture, or print the paid subset command and stop.

``--fixture --backend docker`` runs the real ``baseagent`` CLI on the local task with
``--backend docker``: a scripted fake model (no provider call) runs the task's check in the
container, then the verifier runs ``tests/test.sh`` in a fresh container. The fake Docker
runner is used unless ``--real-docker`` is given (needs Docker and the image locally).
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.evaluation.terminal_bench import (  # noqa: E402
    FIXTURE, load_subset, planned_live, run_fixture,
)

FIXTURE_IMAGE = "python:3.12-slim"


class ScriptedModel:
    """No provider: run the task's own check through run_command, then answer from its output."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {"tool_calls": [{"id": "tb1", "type": "function", "function": {
                "name": "run_command", "arguments": json.dumps({"argv": ["sh", "tests/test.sh"], "timeout": 30})}}]}
        result = json.loads(messages[-1]["content"])
        return {"content": (result.get("data") or {}).get("stdout", "").strip() or "no output"}


def docker_executor(*, real=False, image=FIXTURE_IMAGE):
    """executor(argv) for run_fixture: the agent runs with --backend docker, then the verifier does."""
    from baseagent.backends import DockerBackend
    from baseagent.main import main

    def executor(argv):
        fake = None
        if not real:
            from baseagent.testing.docker import FakeDocker
            fake = FakeDocker(images=(image,))
        started = []

        def backend_factory(args, root, protected):
            backend = DockerBackend(root, image=image, network="none", protected_paths=protected,
                                    **({"runner": fake} if fake else {}))
            started.append(backend.name)
            return backend

        workspace = Path(tempfile.mkdtemp(prefix="baseagent-tb-"))
        try:
            task = workspace / "task"
            shutil.copytree(FIXTURE, task)
            instruction = (task / "instruction.md").read_text(encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["--root", str(task), "--db", str(task / ".baseagent" / "tb.sqlite3"), "--backend", "docker",
                             "--docker-image", image, "--mode", "full", "--output-format", "json",
                             "--session", "terminal-bench-local-echo", instruction],
                            model_factory=lambda args: ScriptedModel(), backend_factory=backend_factory)
            mode = "real" if real else "fake"
            if code == 3:       # Docker unavailable: infrastructure, not a model failure
                return {"infrastructure": True, "steps": 0, "tokens": 0, "cost_cny": 0, "backend": "docker",
                        "docker": mode, "error": stderr.getvalue().strip()[-300:]}
            result = json.loads(stdout.getvalue())
            # The verifier: tests/test.sh in a fresh container, like Harbor after the agent finishes.
            verifier = DockerBackend(task, image=image, network="none", **({"runner": fake} if fake else {}))
            started.append(verifier.name)
            try:
                check = verifier.execute([argv[0], "tests/test.sh"], timeout=30)
            finally:
                verifier.close()
            output = (check.data or {}).get("stdout", "")
            if fake is not None:
                removed = sorted(fake.removed()) == sorted(started) and not fake.containers
            else:
                import subprocess
                names = [part for name in started for part in ("-f", f"name={name}")]
                listed = subprocess.run(["docker", "ps", "-aq", *names], capture_output=True, text=True, check=False)
                removed = listed.stdout.strip() == ""
            return {"passed": code == 0 and check.ok and "ok" in output, "steps": result["usage"]["steps"],
                    "tokens": result["usage"]["total_tokens"], "cost_cny": 0, "backend": "docker", "docker": mode,
                    "agent_exit_code": code, "container_removed": removed, "final_answer": result["final_answer"]}
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    return executor


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", help="Score the local fake task; no model and no dataset download")
    parser.add_argument("--live", action="store_true", help="Print the paid subset command and stop")
    parser.add_argument("--budget-cny", default="15")
    parser.add_argument("--backend", choices=["docker"], help="--fixture: run the task through baseagent --backend docker")
    parser.add_argument("--real-docker", action="store_true", help="--backend docker: real Docker instead of the fake runner")
    args = parser.parse_args(argv)
    if args.live:
        print(json.dumps(planned_live(args.budget_cny), ensure_ascii=False, indent=2))
        return 0
    if not args.fixture:
        parser.error("choose --fixture for the offline pipe, or --live to print the paid command")
    if args.real_docker and args.backend != "docker":
        parser.error("--real-docker needs --backend docker")
    subset = load_subset()

    def executor(argv):
        return {"passed": True, "steps": 1, "tokens": 0, "cost_cny": 0, "backend": "fixture"}

    report = run_fixture(executor=docker_executor(real=args.real_docker) if args.backend == "docker" else executor)
    report["subset_size"] = len(subset["task_ids"])
    report["live_executed"] = False
    report["provider_calls"] = 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["success_rate"] != 1:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
