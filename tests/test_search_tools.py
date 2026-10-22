import shutil
import subprocess
from unittest.mock import patch

import pytest

from baseagent.capabilities import assemble_capabilities
from baseagent.tools import search
from baseagent.tools.result import ErrorCode, ToolFailure
from baseagent.tools.workspace import Workspace, coding_tools


@pytest.fixture(params=["rg", "python"])
def workspace(tmp_path, monkeypatch, request):
    executable = shutil.which("rg")
    if request.param == "rg" and executable is None:
        pytest.skip("ripgrep is not on PATH")
    monkeypatch.setattr(search.shutil, "which", lambda name: executable if request.param == "rg" else None)
    return Workspace(tmp_path)


def write(workspace, name, content="needle\n"):
    path = workspace.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_regex_case_glob_scope_and_line_numbers(workspace):
    write(workspace, "z.py", "needle\n")
    write(workspace, "src/a.py", "first\r\nNeedle 12\r\nneedle 34\r\n")
    write(workspace, "src/a.txt")
    assert workspace.grep(r"needle \d+", path="src", glob="**/*.py", ignore_case=True) == {
        "matches": [{"path": "src/a.py", "line": 2, "text": "Needle 12"},
                    {"path": "src/a.py", "line": 3, "text": "needle 34"}],
        "truncated": False,
    }
    assert workspace.grep(r"^needle$", path="z.py")["matches"] == [
        {"path": "z.py", "line": 1, "text": "needle"}]
    assert workspace.grep("absent") == {"matches": [], "truncated": False}


def test_python_regex_semantics_are_backend_independent(workspace):
    write(workspace, "a.txt", "needle\rsecond\nİſKı\nrepeat repeat\n")
    for pattern, ignore_case, numbers in [
        (r"(?<=nee)dle", False, [1]),
        (r"^second$", False, [2]),
        (r"iskı", True, [3]),
        (r"(repeat) \1", False, [4]),
        ("", False, [1, 2, 3, 4]),
    ]:
        assert [match["line"] for match in workspace.grep(pattern, ignore_case=ignore_case)["matches"]] == numbers


def test_sorted_globs_including_root_and_nested_files(workspace):
    for name in ["z.py", "a/deep/b.py", "a.py", "a.txt", "a/x.py", ".hidden.py"]:
        write(workspace, name)
    expected = [".hidden.py", "a.py", "a/deep/b.py", "a/x.py", "z.py"]
    assert workspace.glob("**/*.py") == {"matches": expected, "truncated": False}
    assert workspace.glob("*.py")["matches"] == expected
    assert workspace.glob("a/*.py")["matches"] == ["a/x.py"]
    assert workspace.glob("**/*.py", path="a")["matches"] == ["a/deep/b.py", "a/x.py"]
    assert workspace.glob("*.py", path="a.py")["matches"] == ["a.py"]


def test_result_caps_are_exact(workspace):
    write(workspace, "a.py", "needle\nneedle\n")
    write(workspace, "b.py")
    assert len(workspace.grep("needle", max_matches=2)["matches"]) == 2
    assert workspace.grep("needle", max_matches=2)["truncated"]
    assert not workspace.grep("needle", max_matches=3)["truncated"]
    assert workspace.glob("*.py", max_results=1) == {"matches": ["a.py"], "truncated": True}
    assert not workspace.glob("*.py", max_results=2)["truncated"]


def test_long_lines_binary_invalid_utf8_and_huge_files(workspace):
    write(workspace, "long.txt", "needle" + "x" * search.MAX_LINE_LENGTH)
    write(workspace, "binary", b"needle\0data")
    write(workspace, "invalid", b"needle\xff")
    write(workspace, "huge", b"needle" + b"x" * search.MAX_FILE_BYTES)
    result = workspace.grep("needle")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == "long.txt"
    assert len(result["matches"][0]["text"]) == search.MAX_LINE_LENGTH
    assert result["truncated"]


def test_common_ignored_and_protected_paths(workspace):
    write(workspace, "visible.py")
    for directory in [".git", ".venv", "node_modules", "__pycache__", ".baseagent"]:
        write(workspace, f"{directory}/secret.py")
        write(workspace, f"nested/{directory}/secret.py")
    write(workspace, ".env")
    write(workspace, ".env.local")
    write(workspace, ".baseagent-edit-secret")
    assert workspace.grep("needle")["matches"] == [{"path": "visible.py", "line": 1, "text": "needle"}]
    assert workspace.glob("**/*")["matches"] == ["visible.py"]


def test_basic_nested_gitignore_patterns_and_negation(workspace):
    write(workspace, ".gitignore", "*.log\n/cache/\n*.tmp\n!keep.tmp\n")
    write(workspace, "sub/.gitignore", "private.py\n")
    for name in ["debug.log", "sub/debug.log", "cache/x.py", "x.tmp", "sub/private.py"]:
        write(workspace, name)
    for name in ["keep.tmp", "sub/visible.py", "sub/cache/visible.py"]:
        write(workspace, name)
    expected = ["keep.tmp", "sub/cache/visible.py", "sub/visible.py"]
    assert [match["path"] for match in workspace.grep("^needle$")["matches"]] == expected
    assert [name for name in workspace.glob("**/*")["matches"] if not name.endswith(".gitignore")] == expected
    assert workspace.grep("needle", path="sub/private.py")["matches"] == []


def test_confinement_and_symlink_escape(workspace, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "secret.py").write_text("needle", encoding="utf-8")
    (workspace.root / "escape").symlink_to(outside, target_is_directory=True)
    (workspace.root / "link.py").symlink_to(outside / "secret.py")
    write(workspace, "visible.py")
    for path in ["../" + outside.name, str(outside), "escape", "link.py", "escape/secret.py"]:
        for call in [lambda: workspace.grep("needle", path=path), lambda: workspace.glob("*", path=path)]:
            with pytest.raises(ToolFailure) as error:
                call()
            assert error.value.code == ErrorCode.PERMISSION_DENIED
    assert workspace.glob("**/*")["matches"] == ["visible.py"]
    assert len(workspace.grep("needle")["matches"]) == 1


def test_protected_paths_cannot_be_discovered(workspace):
    secret = write(workspace, "sessions/private.py")
    write(workspace, "visible.py")
    protected = Workspace(workspace.root, protected_paths=[secret.parent])
    assert protected.glob("**/*")["matches"] == ["visible.py"]
    assert len(protected.grep("needle")["matches"]) == 1


def test_invalid_arguments_and_missing_path(workspace):
    for pattern in ["[", "a{100000000000000000000}"]:
        with pytest.raises(ToolFailure) as error:
            workspace.grep(pattern)
        assert error.value.code == ErrorCode.INVALID_ARGUMENTS
    for call in [lambda: workspace.grep("x", max_matches=0),
                 lambda: workspace.glob("*", max_results=True),
                 lambda: workspace.glob(""), lambda: workspace.grep("x", glob="../*")]:
        with pytest.raises(ToolFailure):
            call()
    with pytest.raises(ToolFailure) as error:
        workspace.glob("*", path="absent")
    assert error.value.code == ErrorCode.NOT_FOUND


def test_registration_and_retry_safety(workspace):
    write(workspace, "a.py")
    for tools in [coding_tools(workspace), assemble_capabilities(workspace, packages=["coding"])]:
        for name in ["grep", "glob", "search_files"]:
            assert tools.retry_safe(name)
        assert tools.execute("grep", {"pattern": "needle"}).ok
        assert tools.execute("glob", {"pattern": "*.py"}).data["matches"] == ["a.py"]
        assert tools.execute("grep", {"pattern": "["}).error.code == ErrorCode.INVALID_ARGUMENTS


def test_rg_is_used_and_failure_falls_back(tmp_path):
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not on PATH")
    workspace = Workspace(tmp_path)
    write(workspace, "a.py")
    with patch.object(search.subprocess, "run", wraps=subprocess.run) as run:
        native = workspace.grep("needle")
        assert any("--regexp" in call.args[0] for call in run.call_args_list)
        assert any("--files" in call.args[0] for call in run.call_args_list)
    with patch.object(search.shutil, "which", return_value=None):
        assert workspace.grep("needle") == native
    with patch.object(search.subprocess, "run", side_effect=OSError("rg unavailable")):
        assert workspace.grep("needle") == native


def test_nonlocal_backend_does_not_search_host(tmp_path):
    from test_backends import MemoryBackend

    workspace = Workspace(backend=MemoryBackend(tmp_path))
    with patch("os.walk", side_effect=AssertionError("host search")), patch.object(search.subprocess, "run", side_effect=AssertionError("host process")):
        with pytest.raises(ToolFailure) as error:
            workspace.grep("needle")
        assert error.value.code == ErrorCode.EXECUTION_FAILED
