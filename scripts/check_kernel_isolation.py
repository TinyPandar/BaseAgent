"""Verify the copied kernel in a disposable venv; host code uses only stdlib.

Run with the repository interpreter. The temporary venv installs only
jsonschema>=4.23,<5 and its dependencies. No repository package is installed.
"""

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import venv


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_THIRD_PARTY = {"jsonschema"}


def outside_imports(path, package):
    rejected = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                if node.level > len(parts):
                    rejected.append(f"{path.name}:{node.lineno}: relative import outside package")
                    continue
                prefix = ".".join(parts[:len(parts) - node.level + 1])
                names = [prefix + ("." + node.module if node.module else "")]
            else:
                names = [node.module or ""]
        else:
            continue
        for name in names:
            if name == "baseagent.kernel" or name.startswith("baseagent.kernel."):
                continue
            if name.split(".")[0] not in sys.stdlib_module_names | ALLOWED_THIRD_PARTY:
                rejected.append(f"{path.name}:{node.lineno}: {name}")
            elif name.split(".")[0] == "jsonschema" and package.startswith("baseagent") and path.name != "tools.py":
                rejected.append(f"{path.name}:{node.lineno}: jsonschema outside kernel/tools.py")
    return rejected


RUNNER = textwrap.dedent('''\
    import importlib.metadata
    import json
    from pathlib import Path
    import re
    import sys
    import unittest

    root = Path(sys.argv[1]).resolve()
    repository = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(root / "package"))
    import baseagent.kernel as kernel
    from baseagent.kernel import CancellationToken, KernelState, RunStatus, ToolRegistry, iter_agent

    # Traverse installed, non-extra requirements, without needing packaging.
    # The install report also binds this to the fresh pip resolver's result.
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    frozen = json.loads((root / "frozen.json").read_text(encoding="utf-8"))
    report = json.loads((root / "install.json").read_text(encoding="utf-8"))
    resolved = {normalize(item["metadata"]["name"]) for item in report["install"]}
    assert frozen == sorted(resolved), "pip freeze differs from the fresh installation report"
    closure, pending = set(), ["jsonschema"]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        distribution = importlib.metadata.distribution(name)
        for requirement in distribution.requires or []:
            if ";" in requirement and re.search(r"\\bextra\\b", requirement.split(";", 1)[1]):
                continue
            dependency = normalize(re.match(r"\\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement).group(1))
            if dependency in resolved:
                pending.append(dependency)
    assert resolved == closure, "unexpected packages: " + ", ".join(sorted(resolved - closure))
    version = importlib.metadata.version("jsonschema")
    assert (4, 23) <= tuple(map(int, version.split(".")[:2])) < (5, 0), version

    class FakeModel:
        def __init__(self, replies):
            self.replies = iter(replies)
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            return next(self.replies)

    class IsolationSmoke(unittest.TestCase):
        def test_model_tool_and_serializable_state(self):
            effects, tools = [], ToolRegistry()
            tools.register("add", "Add", {"type": "object", "properties": {
                "a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
                lambda a, b: effects.append(a + b) or a + b)
            model = FakeModel([{"tool_calls": [{"id": "one", "type": "function", "function": {
                "name": "add", "arguments": '{"a": 19, "b": 23}'}}]}, {"content": "42"}])
            state = KernelState(messages=[{"role": "system", "content": "system"},
                                           {"role": "user", "content": "add"}])
            events = list(iter_agent(model, state, tools=tools))
            self.assertEqual((state.status, state.final_answer, model.calls, state.tool_calls),
                             (RunStatus.COMPLETED, "42", 2, 1))
            self.assertEqual(effects, [42])
            self.assertEqual(events[-1].type, "run_stopped")
            self.assertEqual(KernelState.from_dict(json.loads(json.dumps(state.to_dict(), allow_nan=False))), state)

        def test_cancellation_prevents_dispatch(self):
            token = CancellationToken()
            model = FakeModel([])
            state = KernelState(messages=[{"role": "system", "content": "system"},
                                           {"role": "user", "content": "cancel"}])
            for event in iter_agent(model, state, tools=ToolRegistry(), cancellation=token):
                if event.type == "model_started":
                    token.cancel()
            self.assertEqual((state.status, model.calls), (RunStatus.CANCELLED, 0))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IsolationSmoke)
    for filename in json.loads((root / "selected.json").read_text(encoding="utf-8")):
        suite.addTests(unittest.defaultTestLoader.discover(str(root / "tests"), pattern=filename))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unexpected = sorted(name for name in sys.modules if name.startswith("baseagent.")
                        and not (name == "baseagent.kernel" or name.startswith("baseagent.kernel.")))
    assert not unexpected, "loaded non-kernel modules: " + ", ".join(unexpected)
    origin = Path(kernel.__file__).resolve()
    assert origin.is_relative_to(root) and not origin.is_relative_to(repository), str(origin)
    print("kernel origin:", origin)
    print("baseagent module boundary: PASS")
    print("dependency closure: PASS")
    sys.exit(0 if result.wasSuccessful() else 1)
''')


def run(command, *, env, timeout=300):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        # pip's isolated mode ignores credential-bearing user index settings.
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"subprocess failed (exit {result.returncode})")
    return result


def main():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [value for value in configuration["project"]["dependencies"] if value.startswith("jsonschema")]
    if requirements != ["jsonschema>=4.23,<5"]:
        raise RuntimeError("unexpected jsonschema range in pyproject.toml")
    source = ROOT / "src" / "baseagent" / "kernel"
    violations = []
    for path in source.rglob("*.py"):
        package = ".".join(("baseagent", "kernel", *path.parent.relative_to(source).parts))
        violations.extend(outside_imports(path, package))
    if violations:
        raise RuntimeError("non-whitelist kernel imports:\n" + "\n".join(violations))

    selected = []
    for path in sorted((ROOT / "tests").glob("test_kernel*.py")):
        violations = outside_imports(path, "tests")
        if violations:
            print("not copied (depends on modules outside kernel):", path.name)
            for violation in violations:
                print("  " + violation)
        else:
            selected.append(path)
    print("eligible existing kernel test files:", len(selected))

    with tempfile.TemporaryDirectory(prefix="baseagent-kernel-isolation-") as directory:
        root = Path(directory).resolve()
        if root.is_relative_to(ROOT):
            raise RuntimeError("temporary directory must be outside the repository")
        package = root / "package" / "baseagent"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        shutil.copytree(source, package / "kernel", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        tests = root / "tests"
        tests.mkdir()
        for path in selected:
            shutil.copy2(path, tests / path.name)
        (root / "selected.json").write_text(json.dumps([path.name for path in selected]), encoding="utf-8")
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(root / "venv")
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(name, None)
        env["PYTHONIOENCODING"] = "utf-8"
        run([str(python), "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
             "--report", str(root / "install.json"), requirements[0]], env=env)
        frozen = run([str(python), "-I", "-m", "pip", "--isolated", "freeze"], env=env).stdout
        print("pip freeze:\n" + frozen, end="")
        names = []
        for line in frozen.splitlines():
            if not re.fullmatch(r"[A-Za-z0-9._-]+==[^\s]+", line):
                raise RuntimeError("unexpected pip freeze entry")
            names.append(re.sub(r"[-_.]+", "-", line.split("==", 1)[0]).lower())
        (root / "frozen.json").write_text(json.dumps(sorted(names)), encoding="utf-8")
        probe = root / "probe.py"
        probe.write_text(RUNNER, encoding="utf-8")
        result = run([str(python), "-I", "-B", str(probe), str(root), str(ROOT)], env=env, timeout=90)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    if root.exists():
        raise RuntimeError("temporary directory was not cleaned up")
    print("temporary directory cleanup: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("isolation verification FAILED:", str(exc), file=sys.stderr)
        raise SystemExit(1)
