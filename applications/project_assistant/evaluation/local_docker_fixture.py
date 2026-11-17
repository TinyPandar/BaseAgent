"""Local Docker CLI pipeline smoke, separate from Harbor task environments and grading."""
import contextlib
import io
import json
import shutil
import tempfile
from pathlib import Path
from .terminal_bench import load_subset, score_records

FIXTURE = Path(__file__).parent / "fixtures" / "terminal_bench" / "local-docker-echo"
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
            # The check: tests/test.sh in a fresh container (pipeline check, not Harbor's verifier).
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



def run_local_docker_fixture(*, real=False):
    """Exercise the original CLI + fresh-container verifier with a scripted model."""
    record = docker_executor(real=real)(["sh", str(FIXTURE / "tests" / "test.sh")])
    record.setdefault("id", "local-echo")
    report = score_records([record])
    report.update(subset_size=len(load_subset()["task_ids"]), live_executed=False, provider_calls=0,
                  fixture_only=True, note="Local Docker CLI pipeline smoke. Not a Terminal-Bench score.")
    return report


def planned_local_docker(budget_cny="15"):
    """Legacy --live is print-only; this is not the official Harbor entry point."""
    return {"executed": False, "provider_calls": 0, "cost_cny": 0, "status": "authorization_required",
            "budget_cny": str(budget_cny), "backend": "docker", "harbor_integration": "unverified",
            "agent_command": 'baseagent --root <task workspace> --backend docker --docker-image <task image> --mode full --output-format json "$(cat instruction.md)"',
            "note": "Legacy local Docker command only, not a verified Harbor invocation; use --plan for Harbor. CNY is a soft limit."}
