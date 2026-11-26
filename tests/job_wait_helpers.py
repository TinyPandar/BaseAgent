"""Controlled processes and forced interleavings for the job_wait tests. Fake models only.

A gated process records its start and pid, then runs until the test writes its gate file; the gate's
content is the exit code. Races are forced with ``Ticks``: a function runs at the n-th look job_wait
takes at the local state, so "finished and interrupted in the same look" is an exact situation.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

from checkpoint_helpers import Script, tool_call

from baseagent.agent import run_agent
from baseagent.agent.inbox import Inbox, InboxMiddleware
from baseagent.agent.interim import InterimAnswerMiddleware, InterimAnswers
from baseagent.tools import job_wait as jw
from baseagent.tools.workspace import coding_tools
from test_interjection_handover import InterjectionCase, tool_data, tool_pairs_closed  # noqa: F401 -- re-exported
from test_jobs import PY, alive, wait_until

GATED = """
import os, sys, time
marker, gate = sys.argv[1], sys.argv[2]
with open(marker, "a") as handle:
    handle.write("start\\n")
with open(marker + ".pid.tmp", "w") as handle:
    handle.write(str(os.getpid()))
os.replace(marker + ".pid.tmp", marker + ".pid")
print("output of the job", flush=True)
deadline = time.monotonic() + 120
while not os.path.exists(gate) and time.monotonic() < deadline:
    time.sleep(0.01)
print("last line", flush=True)
raise SystemExit(int(open(gate).read() or 0))
"""


WAITING_TEXT = "waiting"


def wait_call(call_id, job_id, **arguments):
    return tool_call(call_id, "job_wait", {"job_id": job_id, **arguments})


class Ticks:
    """Run ``actions[n]`` at the n-th look of job_wait (1 = first look of the first wait)."""

    def __init__(self, actions=None):
        self.actions = dict(actions or {})
        self.count = 0

    def __call__(self, job_id):
        self.count += 1
        action = self.actions.get(self.count)
        if action is not None:
            action()


class Process:
    """One gated command: its argv, its gate and what it recorded."""

    def __init__(self, root, name="p"):
        (root / "gated.py").write_text(GATED, encoding="utf-8")
        self.marker, self.gate = root / f"{name}.starts", root / f"{name}.gate"
        self.argv = [PY, "gated.py", str(self.marker), str(self.gate)]

    def open(self, code=0):
        temporary = self.gate.with_suffix(".tmp")
        temporary.write_text(str(code))
        temporary.replace(self.gate)

    def starts(self):
        return len(self.marker.read_text().splitlines()) if self.marker.exists() else 0

    def started(self):
        path = Path(str(self.marker) + ".pid")
        assert wait_until(path.exists), "process never started"
        return int(path.read_text())

    def alive(self):
        return alive(self.started())


class WaitCase(InterjectionCase):
    """A single agent with its own inbox, jobs and a frontend that shows answers at once (like the CLI)."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(jw, "WAIT_MIN", 1))
        self.process = Process(self.root)
        self.inbox = Inbox()
        self.published = []
        self.addCleanup(self.process.open)

    def ticks(self, actions=None):
        ticks = Ticks(actions)
        self.enterContext(patch.object(jw, "_on_tick", ticks))
        return ticks

    def first(self, job_id, call_id="w1", **arguments):
        """The first wait of a new turn: its prompt is a new user input, so it is answered and confirmed."""
        return {"content": WAITING_TEXT, "tool_calls": [wait_call(call_id, job_id, still_needed="the user asked", **arguments)]}

    def printing(self):
        """Answers of a frontend that shows what it is given immediately."""
        answers = InterimAnswers()

        def publish(answer):
            if answer.text != WAITING_TEXT:
                self.published.append((time.time(), answer.text))
            return len(self.published), True
        answers.publish = publish
        return answers

    def gui(self):
        """Answers of a frontend that must confirm what is on screen."""
        answers = InterimAnswers()

        def publish(answer):
            if answer.text == WAITING_TEXT:
                return 0, True           # the plain first wait of a turn is not what these tests are about
            self.published.append((time.time(), answer.text))
            return len(self.published), False
        answers.publish = publish
        return answers

    def jid(self, workspace):
        (job,) = workspace.jobs.list()["jobs"]
        return job["job_id"]

    def finish(self, workspace, code=0, job_id=None):
        """Let the process end and wait until its manager knows (used inside a tick: no timing involved)."""
        self.process.open(code)
        job = workspace.jobs._get(job_id or self.jid(workspace))
        assert job.done.wait(20), "job never ended"

    def own(self, workspace, session_id="s1"):
        workspace.jobs.owner = session_id
        return workspace

    def turn(self, model, workspace, text="train the model", session_id="s1", *, inbox=None, answers=None,
             middleware=None, **kwargs):
        layers = middleware if middleware is not None else [
            InboxMiddleware(inbox if inbox is not None else self.inbox, jobs=workspace.jobs, settles=True),
            InterimAnswerMiddleware(answers if answers is not None else self.printing())]
        return run_agent(model, text, tools=coding_tools(workspace), middleware=layers, store=self.store,
                         session_id=session_id, workspace_root=str(self.root), max_steps=kwargs.pop("max_steps", 12), **kwargs)

    def started_job(self, workspace, session_id="s1", **declared):
        """A turn that only starts the gated process, so the job has a turn, a quote and a responsible agent."""
        model = Script([{"tool_calls": [tool_call("start", "start_job", {"argv": self.process.argv, **declared})]},
                        {"content": "started"}])
        state = self.turn(model, self.own(workspace, session_id), session_id=session_id)
        self.assertEqual(str(state.status), "completed")
        self.process.started()
        return self.jid(workspace)

    def link(self, job_id):
        return json.loads((self.root / ".baseagent" / "jobs" / f"{job_id}.link.json").read_text(encoding="utf-8"))

    def record(self, job_id):
        return json.loads((self.root / ".baseagent" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))


def text_of(messages):
    return "\n".join(str(message.get("content")) for message in messages)


__all__ = ["InterjectionCase", "Process", "Script", "Ticks", "WaitCase", "jw", "text_of", "tool_call", "tool_data",
           "tool_pairs_closed", "wait_call", "wait_until"]
