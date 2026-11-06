"""Show Docker isolation with a local image. No model call and no image pull."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from baseagent.backends import DockerBackend  # noqa: E402
from baseagent.backends.docker import DockerError  # noqa: E402


def main():
    if not shutil.which("docker"):
        print(json.dumps({"status": "skipped", "reason": "docker cli missing", "cost_cny": 0}))
        return 0
    parent = Path(tempfile.mkdtemp(prefix="baseagent-docker-"))
    workspace = parent / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
    (parent / "secret.txt").write_text("host-secret\n", encoding="utf-8")
    try:
        backend = DockerBackend(workspace, image="nginx:alpine", name="baseagent-demo-docker")
    except DockerError as exc:
        print(json.dumps({"status": "skipped", "reason": str(exc), "cost_cny": 0}, ensure_ascii=False))
        return 0
    try:
        ran = backend.execute(["echo", "ok"], timeout=20)
        outside = backend.execute(["sh", "-c", "cat /secret.txt"], timeout=20)
        blocked = backend.execute(["wget", "-q", "-T", "3", "-O", "-", "http://example.com"], timeout=20)
        report = {"status": "ran", "echo_ok": ran.ok, "outside_blocked": not outside.ok,
                  "network_blocked": not blocked.ok, "network": backend.contract()["network"], "cost_cny": 0}
    finally:
        name = backend.name
        backend.close()
    remaining = subprocess.run(["docker", "ps", "-aq", "-f", f"name={name}"], capture_output=True, text=True, check=False)
    report["container_removed"] = remaining.stdout.strip() == ""
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["echo_ok"] and report["outside_blocked"] and report["network_blocked"] and report["container_removed"] else 1


if __name__ == "__main__":
    sys.exit(main())
