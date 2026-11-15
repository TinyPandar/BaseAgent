"""A container that `docker run` created is removed even when the start does not finish.

Two failure kinds while the container starts: Ctrl+C (KeyboardInterrupt) and an exception (timeout, failed
start, no container id). In both, the half-started container is removed and the original error reaches the
caller unchanged; a failed removal is only a note on that error.
"""

import copy
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script
from test_docker_cli import DOCKER, DockerCliCase

from baseagent.backends.docker import DockerBackend, DockerError
from baseagent.main import BACKEND_UNAVAILABLE_EXIT
from baseagent.testing.docker import FakeDocker


def created_then(fake, outcome):
    """A `docker run` hook: the daemon creates the container, then the CLI call ends with ``outcome``.

    ``outcome`` is an exception to raise or a ``(code, stdout, stderr)`` tuple to return.
    """
    def hook(argv):
        if argv[1] != "run":
            return None
        fake._run_container(argv)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    return hook


def foreign_container(fake, name, root):
    """A container that already holds ``name`` and is not this run's (another tool, or a reused name)."""
    fake.containers[name] = {"id": "foreign0001", "root": Path(root), "labels": {"owner": "someone-else"},
                             "network": "bridge", "running": True, "mounts": [], "image": "other:1"}
    fake.names["foreign0001"] = name
    return copy.deepcopy(fake.containers[name])


def touching(fake, name, since=0):
    """Calls after ``since`` that name the container ``name`` or its id directly (rm, stop, start, exec, ...)."""
    ids = {name, *[cid for cid, n in fake.names.items() if n == name and cid.startswith("foreign")]}
    return [argv for argv in fake.calls[since:] if argv[1] != "run" and ids & set(argv)]


class BackendStartTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake = FakeDocker()

    def start(self):
        return DockerBackend(self.root, runner=self.fake, owner=False)

    def assert_removed(self):
        [name] = self.fake.started()
        self.assertIn(name, self.fake.removed())
        self.assertEqual(self.fake.containers, {})

    def test_ctrl_c_during_docker_run_removes_the_created_container(self):
        interrupt = KeyboardInterrupt()
        self.fake.hook = created_then(self.fake, interrupt)
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.start()
        self.assertIs(raised.exception, interrupt)
        self.assertFalse(getattr(raised.exception, "__notes__", None))
        self.assert_removed()

    def test_an_exception_during_docker_run_removes_the_created_container(self):
        timeout = DockerError("Docker command timed out after 120 seconds: docker run -d")
        self.fake.hook = created_then(self.fake, timeout)
        with self.assertRaises(DockerError) as raised:
            self.start()
        self.assertIs(raised.exception, timeout)
        self.assert_removed()

    def test_a_failed_start_after_create_keeps_dockers_message(self):
        message = b"docker: Error response from daemon: failed to create task for container: OCI runtime create failed\n"
        self.fake.hook = created_then(self.fake, (127, b"", message))
        with self.assertRaises(DockerError) as raised:
            self.start()
        self.assertIn("OCI runtime create failed", str(raised.exception))
        self.assert_removed()

    def test_a_missing_container_id_removes_the_created_container(self):
        self.fake.hook = created_then(self.fake, (0, b"\n", b""))
        with self.assertRaisesRegex(DockerError, "did not return a container id"):
            self.start()
        self.assert_removed()

    def test_a_failed_removal_is_a_note_on_the_original_error(self):
        for cleanup in [(1, b"", b"Error response from daemon: removal of container is already in progress\n"),
                        KeyboardInterrupt()]:
            with self.subTest(cleanup=type(cleanup).__name__):
                self.fake = FakeDocker()
                interrupt = KeyboardInterrupt()
                create = created_then(self.fake, interrupt)

                def hook(argv, create=create, cleanup=cleanup):
                    if argv[1] == "rm":
                        if isinstance(cleanup, BaseException):
                            raise cleanup              # a second Ctrl+C while removing
                        return cleanup
                    return create(argv)
                self.fake.hook = hook
                with self.assertRaises(KeyboardInterrupt) as raised:
                    self.start()
                self.assertIs(raised.exception, interrupt)
                [note] = raised.exception.__notes__
                self.assertIn(f"could not remove container {self.fake.started()[0]}", note)
                self.assertIn("the next run in this workspace removes it", note)

    def test_the_next_run_reaps_a_container_whose_removal_failed(self):
        self.fake.hook = lambda argv: (1, b"", b"Error: removal failed\n") if argv[1] == "rm" else \
            created_then(self.fake, KeyboardInterrupt())(argv)
        with self.assertRaises(KeyboardInterrupt):
            self.start()
        [leftover] = self.fake.containers
        # The leftover's pid is this live test process, so mark it as an earlier, dead run.
        self.fake.containers[leftover]["labels"]["baseagent.pid"] = "999999999"
        self.fake.hook = None
        backend = self.start()
        self.assertEqual(backend.reaped, [leftover])
        backend.close()
        self.assertEqual(self.fake.containers, {})

    def test_a_failure_before_docker_run_removes_nothing(self):
        self.fake = FakeDocker(images=())
        with self.assertRaises(DockerError):
            self.start()
        self.assertEqual(self.fake.started(), [])
        self.assertEqual(self.fake.removed(), [])


    # ----- a name that is already taken ------------------------------------------------------------
    def test_a_name_conflict_leaves_the_existing_container_alone(self):
        before = foreign_container(self.fake, "baseagent-taken", self.root)
        with self.assertRaisesRegex(DockerError, "is already in use") as raised:
            DockerBackend(self.root, runner=self.fake, owner=False, name="baseagent-taken")
        self.assertFalse(getattr(raised.exception, "__notes__", None))
        self.assertEqual(self.fake.containers, {"baseagent-taken": before})
        self.assertEqual(touching(self.fake, "baseagent-taken"), [])
        self.assertEqual(self.fake.removed(), [])

    def test_ctrl_c_on_a_name_conflict_leaves_the_existing_container_alone(self):
        # Ctrl+C while `docker run` is rejected (the daemon created nothing for this run).
        before = foreign_container(self.fake, "baseagent-taken", self.root)
        interrupt = KeyboardInterrupt()

        def hook(argv):
            if argv[1] == "run":
                raise interrupt
            return None
        self.fake.hook = hook
        with self.assertRaises(KeyboardInterrupt) as raised:
            DockerBackend(self.root, runner=self.fake, owner=False, name="baseagent-taken")
        self.assertIs(raised.exception, interrupt)
        self.assertEqual(self.fake.containers, {"baseagent-taken": before})
        self.assertEqual(touching(self.fake, "baseagent-taken"), [])

    def test_only_this_runs_container_is_removed_when_both_exist(self):
        # The daemon created this run's container under another name; a same-named stranger stays.
        stranger = foreign_container(self.fake, "baseagent-taken", self.root)
        interrupt = KeyboardInterrupt()

        def hook(argv):
            if argv[1] == "run":
                renamed = list(argv)
                renamed[renamed.index("--name") + 1] = "baseagent-this-run"
                self.fake._run_container(renamed)
                raise interrupt
            return None
        self.fake.hook = hook
        with self.assertRaises(KeyboardInterrupt):
            DockerBackend(self.root, runner=self.fake, owner=False, name="baseagent-taken")
        self.assertEqual(self.fake.containers, {"baseagent-taken": stranger})
        self.assertEqual(self.fake.removed(), ["baseagent-this-run"])

    def test_a_failed_lookup_during_cleanup_is_a_note_on_the_original_error(self):
        for lookup in [(1, b"", b"Error response from daemon: internal error\n"), KeyboardInterrupt()]:
            with self.subTest(lookup=type(lookup).__name__):
                self.fake = FakeDocker()
                timeout = DockerError("Docker command timed out after 120 seconds: docker run -d")
                create = created_then(self.fake, timeout)

                def hook(argv, create=create, lookup=lookup):
                    if argv[1] == "ps" and "-q" in argv:
                        if isinstance(lookup, BaseException):
                            raise lookup
                        return lookup
                    return create(argv)
                self.fake.hook = hook
                with self.assertRaises(DockerError) as raised:
                    self.start()
                self.assertIs(raised.exception, timeout)
                [note] = raised.exception.__notes__
                self.assertIn("could not remove container", note)
                self.assertEqual(self.fake.removed(), [])

    # ----- normal exit -------------------------------------------------------------------------------
    def test_close_does_not_touch_a_container_that_took_the_name(self):
        backend = self.start()
        name = backend.name
        self.fake.containers.pop(name)                          # this run's container removed from outside ...
        stranger = foreign_container(self.fake, name, self.root)  # ... and another container took its name
        since = len(self.fake.calls)
        backend.close()
        self.assertEqual(self.fake.containers, {name: stranger})
        self.assertEqual(touching(self.fake, name, since), [])
        self.assertIsNone(backend.container_id)

    def test_close_removes_this_runs_container_by_id(self):
        backend = self.start()
        container_id = backend.container_id
        backend.close()
        self.assertEqual(self.fake.containers, {})
        self.assertIn(["docker", "rm", "-f", container_id], self.fake.calls)

class CliStartTests(DockerCliCase):
    def test_ctrl_c_while_the_container_starts(self):
        for fmt in ("text", "json"):
            with self.subTest(fmt=fmt):
                self.fake = FakeDocker()
                self.fake.hook = created_then(self.fake, KeyboardInterrupt())
                model = Script([AssertionError("the model must not be called")])
                code, out, err = self.run_cli([*DOCKER, "--output-format", fmt, "--session", f"int-{fmt}", "hi"],
                                              model)
                self.assertEqual(code, 130)
                self.assertEqual(model.seen, [])
                self.assert_all_removed()
                self.assertNotIn("Traceback", err)
                if fmt == "json":
                    result = json.loads(out)
                    self.assertEqual((result["status"], result["exit_code"]), ("interrupted", 130))

    def test_an_exception_while_the_container_starts(self):
        for fmt in ("text", "json"):
            with self.subTest(fmt=fmt):
                self.fake = FakeDocker()
                message = "Docker command timed out after 120 seconds: docker run -d"
                self.fake.hook = created_then(self.fake, DockerError(message))
                model = Script([AssertionError("the model must not be called")])
                code, out, err = self.run_cli([*DOCKER, "--output-format", fmt, "--session", f"exc-{fmt}", "hi"],
                                              model)
                self.assertEqual(code, BACKEND_UNAVAILABLE_EXIT)
                self.assertEqual(model.seen, [])
                self.assert_all_removed()
                self.assertIn(f"Docker error: {message}", err)
                if fmt == "json":
                    result = json.loads(out)
                    self.assertEqual(result["exit_code"], BACKEND_UNAVAILABLE_EXIT)
                    self.assertIn(message, result["error"])

    def test_a_failed_removal_is_reported_with_the_original_error(self):
        self.fake.hook = lambda argv: (1, b"", b"Error: removal failed\n") if argv[1] == "rm" else \
            created_then(self.fake, DockerError("docker run failed"))(argv)
        code, out, err = self.run_cli([*DOCKER, "--session", "warn", "hi"], Script([]))
        self.assertEqual(code, BACKEND_UNAVAILABLE_EXIT)
        self.assertIn("Docker error: docker run failed", err)
        self.assertIn("Docker warning: could not remove container", err)

    def test_sigterm_is_handled_while_the_container_starts(self):
        seen = []

        def hook(argv):
            if argv[1] == "run":
                seen.append(signal.getsignal(signal.SIGTERM))
            return None
        self.fake.hook = hook
        before = signal.getsignal(signal.SIGTERM)
        code, out, err = self.run_cli([*DOCKER, "--session", "term", "hi"], Script([{"content": "ok"}]))
        self.assertEqual(code, 0)
        # SIGTERM during `docker run` raises SystemExit, so the start's cleanup runs instead of the process dying.
        self.assertNotEqual(seen, [before])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assert_all_removed()


    def test_sigterm_or_sighup_while_the_container_starts_removes_it(self):
        for name in ("SIGTERM", "SIGHUP"):
            with self.subTest(signal=name):
                number = getattr(signal, name)
                self.fake = FakeDocker()

                def hook(argv, number=number):
                    if argv[1] != "run":
                        return None
                    self.fake._run_container(argv)
                    # The container exists, `docker run` has not returned: the signal arrives now.
                    signal.getsignal(number)(number, None)
                    return None
                self.fake.hook = hook
                before = signal.getsignal(number)
                model = Script([AssertionError("the model must not be called")])
                code, out, err = self.run_cli([*DOCKER, "--session", f"sig-{name}", "hi"], model)
                self.assertEqual(code, 128 + number)
                self.assertEqual(model.seen, [])
                self.assert_all_removed()
                self.assertEqual(signal.getsignal(number), before)

    def test_a_name_conflict_from_the_cli_leaves_the_existing_container_alone(self):
        before = foreign_container(self.fake, "baseagent-taken", self.root)

        def factory(args, root, protected):
            return DockerBackend(root, protected_paths=protected, runner=self.fake, owner=False, name="baseagent-taken")
        with patch("baseagent.main._make_backend", factory):
            code, out, err = self.run_cli([*DOCKER, "--output-format", "json", "--session", "taken", "hi"],
                                          Script([AssertionError("the model must not be called")]))
        self.assertEqual(code, BACKEND_UNAVAILABLE_EXIT)
        self.assertIn("is already in use", json.loads(out)["error"])
        self.assertNotIn("Docker warning", err)
        self.assertEqual(self.fake.containers, {"baseagent-taken": before})
        self.assertEqual(touching(self.fake, "baseagent-taken"), [])

if __name__ == "__main__":
    unittest.main()
