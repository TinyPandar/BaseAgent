"""What a background job is for, who is responsible for it, and how far its result has travelled.

The job record (``<id>.json``, tools/jobs.py) holds process facts and may be rewritten by any
manager that looks at it. This sidecar (``<id>.link.json``) holds the facts only the responsible
session changes (it holds the session lock, so there is one writer):

* ``agent_session``: the agent session that started the job. Its notices go to that agent.
* ``origin``: turn, call and the user's own words at that moment (a quote, never a made-up purpose).
* ``purpose`` / ``expect`` / ``next_step``: what the model declared. Written once, never rewritten:
  a later goal must not be able to relabel an earlier job. Not declared stays null.
* ``goal``: whether that goal still holds (``active`` / ``dropped``) and how many of the user's
  inputs the agent had seen when it last confirmed it.
* ``delivery``: which sessions' models have received the final result, and how it was settled.
  "Taken" (a notice left the manager) stays in the job record as ``notified``.

Nothing here starts, stops or restarts a process.
"""

from __future__ import annotations

import json
import time

LINK_VERSION = 1
LINK_SUFFIX = ".link.json"
ACTIVE, DROPPED = "active", "dropped"
HANDLED, REPORTED, LEGACY = "handled", "reported", "legacy"
DECLARED = ("purpose", "expect", "next_step")
MAX_DECLARED = 200
MAX_QUOTE = 200
MAX_REASON = 200


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def new_link(job_id, *, agent_session=None, origin=None, purpose=None, expect=None, next_step=None,
             confirmed_inputs=None):
    return {"version": LINK_VERSION, "job_id": job_id, "agent_session": agent_session, "origin": origin,
            "purpose": purpose, "expect": expect, "next_step": next_step,
            "goal": {"standing": ACTIVE, "confirmed_inputs": confirmed_inputs, "reason": None, "changed_at": None},
            "delivery": {"received": [], "settled": None}}


def read_link(value, job_id):
    """A validated link, or None when the file is missing, damaged or of another version."""
    if not isinstance(value, dict) or value.get("version") != LINK_VERSION or value.get("job_id") != job_id:
        return None
    link = new_link(job_id)
    try:
        for key in ("agent_session", "origin", *DECLARED):
            link[key] = value.get(key)
        link["goal"].update({key: value["goal"].get(key) for key in link["goal"]})
        link["delivery"]["received"] = [dict(item) for item in value["delivery"].get("received") or []]
        link["delivery"]["settled"] = value["delivery"].get("settled")
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if link["goal"]["standing"] not in (ACTIVE, DROPPED):
        return None
    return link


def legacy_link(job_id, *, owner, notified):
    """A job from before links: purpose unknown. An already announced one is not announced again."""
    link = new_link(job_id)
    if notified:
        link["delivery"] = {"received": [{"session": owner, "turn": None, "at": None}],
                            "settled": {"as": LEGACY, "session": owner, "turn": None, "note": None, "at": None}}
    return link


def job_context(context, *, purpose=None, expect=None, next_step=None):
    """The link fields of a job started by the tool call behind ``context`` (None without one)."""
    if context is None:
        return {}
    from baseagent.agent.inbox import direct_inputs
    state = context.state
    inputs = direct_inputs(getattr(state, "messages", None) or [])
    quote = clip(inputs[-1][1], MAX_QUOTE) if inputs else None
    return {"agent_session": getattr(state, "session_id", None),
            "origin": {"turn_id": getattr(state, "turn_id", None), "call_id": getattr(context, "call_id", None),
                       "request": quote},
            "purpose": _declared(purpose), "expect": _declared(expect), "next_step": _declared(next_step),
            "confirmed_inputs": len(inputs)}


def _declared(value):
    value = clip(value, MAX_DECLARED) if isinstance(value, str) else ""
    return value or None


def fill_declared(link, **values):
    """Fill fields that were never declared; a declared field is never changed. True when something changed."""
    changed = False
    for key in DECLARED:
        value = _declared(values.get(key))
        if value and link[key] is None:
            link[key] = value
            changed = True
    return changed


def received_by(link, session):
    return any(item.get("session") == session for item in link["delivery"]["received"])


def settlement(how, *, session, turn, note=None):
    return {"as": how, "session": session, "turn": turn, "note": clip(note, MAX_REASON) if note else None,
            "at": time.time()}


def stage(link, *, terminal, notified):
    """running -> finished (nobody took it) -> taken -> received -> handled / reported."""
    settled = link["delivery"]["settled"]
    if settled:
        return settled["as"]
    if not terminal:
        return "running"
    if link["delivery"]["received"]:
        return "received"
    return "taken" if notified else "finished"


def brief(link):
    """The association as tool results and notices show it. Missing fields stay null."""
    value = {key: link[key] for key in DECLARED}
    value["goal"] = link["goal"]["standing"]
    if link["goal"]["reason"]:
        value["goal_reason"] = link["goal"]["reason"]
    if link["purpose"] is None and (link["origin"] or {}).get("request"):
        value["user_words_at_start"] = link["origin"]["request"]
    return value


REFERENCE_HEADER = "[未了后台任务参考：系统附加，不是用户输入，也不是新指令 / open background jobs, reference only]"
REFERENCE_RULES = ("Data above is reference only. Waiting: job_wait. Goal no longer holds: job_settle dropped (the process keeps "
                   "running). Stopping the process: job_kill. A finished job is not run again because of this list; for "
                   "delivery=received the follow-up may already have been done, check before doing it.")
REFERENCE_JOBS = 6


def open_jobs_reference(jobs, inputs, waits, agent):
    """Text for one model request. Contains no running times, so it changes only when a fact changes."""
    from .jobs import TERMINAL
    if not jobs:
        return ""
    lines = []
    for job in jobs[-REFERENCE_JOBS:]:
        link = job.link
        value = {"job_id": job.id, "status": job.status, "command": clip(" ".join(job.argv), 80),
                 "responsible": link["agent_session"] or job.owner,
                 "delivery": stage(link, terminal=job.status in TERMINAL, notified=job.notified), **brief(link)}
        if job.status in TERMINAL:
            value["exit_code"] = job.exit_code
        if job.id in waits:
            value["waiting"] = waits[job.id]
        confirmed = link["goal"]["confirmed_inputs"]
        if confirmed is not None and len(inputs) > confirmed and link["agent_session"] == agent:
            value["user_inputs_since_confirmed"] = len(inputs) - confirmed
        lines.append(json.dumps(value, ensure_ascii=False))
    if len(jobs) > REFERENCE_JOBS:
        lines.append(f"({len(jobs) - REFERENCE_JOBS} older open jobs: job_list)")
    return "\n".join([REFERENCE_HEADER, *lines, REFERENCE_RULES])


__all__ = ["ACTIVE", "DROPPED", "HANDLED", "LEGACY", "LINK_SUFFIX", "REPORTED", "brief", "clip", "fill_declared",
           "job_context", "legacy_link", "new_link", "open_jobs_reference", "read_link", "received_by", "settlement",
           "stage"]
