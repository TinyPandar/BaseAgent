"""Replies the agent gives inside a turn, and whether a frontend has shown them to the user.

A frontend prints only the final reply of a turn, so a reply followed by ``job_wait`` used to stay
invisible until the wait was over. Such a reply is published here; ``job_wait`` starts waiting only
when its receipt says it was shown. "Shown" is reported by the frontend that did it: the CLI after
printing, the TUI after the screen refresh that contains it, the Web page after adding it to a visible
page (``POST /shown``). It proves the reply was on the user's screen, not that it was read.

Receipts are per reply and per conversation: showing some other output never confirms this one.
They live in memory. After a restart the reply is in the stored conversation and is shown again
(not sent to the model again).
"""

from __future__ import annotations

import threading
import time

from baseagent.middleware import AgentMiddleware

WAIT_TOOL = "job_wait"


class InterimAnswer:
    def __init__(self, key, text):
        self.key, self.text = key, text
        self.event_id = None
        self.shown_at = None

    @property
    def shown(self):
        return self.shown_at is not None


class InterimAnswers:
    """``publish(answer)`` is set by the chat session: returns (event id, already shown)."""

    def __init__(self, publish=None):
        self.publish = publish
        self._items = {}
        self._lock = threading.RLock()   # held while publishing, so an early confirmation finds the event id

    @property
    def available(self):
        return self.publish is not None

    def show(self, key, text):
        with self._lock:
            answer = self._items.get(key)
            if answer is None and self.publish is not None:
                answer = InterimAnswer(key, text)
                answer.event_id, shown = self.publish(answer)
                if shown:
                    answer.shown_at = time.time()
                self._items[key] = answer
                for old in [item for item, value in self._items.items() if value.shown][:-50]:
                    del self._items[old]
            return answer

    def mark_shown(self, event_ids):
        wanted = set(event_ids)
        with self._lock:
            for answer in self._items.values():
                if answer.event_id in wanted and not answer.shown:
                    answer.shown_at = time.time()

    def pending(self):
        with self._lock:
            return [answer for answer in self._items.values() if not answer.shown]


def answer_key(turn_id, message):
    """Identifies one mid-turn reply: its turn and the first tool call of its message. None without calls."""
    calls = message.get("tool_calls") or ()
    return f"{turn_id}:{calls[0]['id']}" if calls else None


def has_text(message):
    content = message.get("content")
    return isinstance(content, str) and bool(content.strip())


class InterimAnswerMiddleware(AgentMiddleware):
    """Publish a reply that comes together with ``job_wait`` as soon as the model returned it."""

    def __init__(self, answers):
        self.answers = answers

    def after_model(self, state, message):
        calls = message.get("tool_calls") or ()
        if has_text(message) and any(call["function"]["name"] == WAIT_TOOL for call in calls):
            self.answers.show(answer_key(state.turn_id, message), message["content"])

    def show_since(self, state, index):
        """Receipts of every reply after message ``index`` (each is published at most once)."""
        replies = [message for message in state.messages[index + 1:]
                   if message.get("role") == "assistant" and message.get("tool_calls") and has_text(message)]
        return [self.answers.show(answer_key(state.turn_id, message), message["content"]) for message in replies]


__all__ = ["InterimAnswer", "InterimAnswerMiddleware", "InterimAnswers", "answer_key", "has_text"]
