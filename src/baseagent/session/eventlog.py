"""Snapshot event pages, bounded following and explicit prefix retention."""

from hashlib import sha256
import json
import math
import time


def page(store, session_id, *, after=0, limit=100):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("event cursor must be nonnegative and limit 1-1000")
    with store._connection() as connection:
        connection.execute("BEGIN")
        if not connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise ValueError("session does not exist")
        floor = connection.execute("SELECT through_sequence FROM event_retention WHERE session_id=?", (session_id,)).fetchone()
        floor = floor[0] if floor else 0
        rows = connection.execute("SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                                  (session_id, max(after, floor), limit + 1)).fetchall()
    values = [{"sequence": row["sequence"], "session_id": row["session_id"], "turn_id": row["turn_id"], **json.loads(row["event_json"])} for row in rows[:limit]]
    return {"events": values, "next_after": values[-1]["sequence"] if values else max(after, floor),
            "has_more": len(rows) > limit, "pruned_through": floor, "history_lost": after < floor}


def follow(store, session_id, *, after=0, limit=100, duration=30.0, poll_interval=0.25, cancellation=None):
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 60:
        raise ValueError("follow duration must be finite and between 0 and 60 seconds")
    if type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or not 0.05 <= poll_interval <= 5:
        raise ValueError("poll interval must be between 0.05 and 5 seconds")
    deadline = time.monotonic() + duration
    cursor = after
    first = True
    while first or time.monotonic() < deadline:
        first = False
        if cancellation is not None:
            cancellation.check()
        value = page(store, session_id, after=cursor, limit=limit)
        cursor = value["next_after"]
        if value["events"] or value["history_lost"]:
            yield value
        if not value["has_more"]:
            wait = max(0, min(poll_interval, deadline - time.monotonic()))
            if cancellation is not None:
                cancellation.wait(wait)
            else:
                time.sleep(wait)


def _preview(connection, session_id, through, keep):
    if not connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
        raise ValueError("session does not exist")
    # Prefix only; retain at least keep latest events, even when through is high.
    boundary = connection.execute("SELECT sequence FROM events WHERE session_id=? ORDER BY sequence DESC LIMIT 1 OFFSET ?",
                                  (session_id, keep - 1)).fetchone()
    cutoff = min(through, boundary[0] - 1) if boundary else 0
    count, last = connection.execute("SELECT COUNT(*), MAX(sequence) FROM events WHERE session_id=? AND sequence<=?",
                                     (session_id, cutoff)).fetchone()
    floor = connection.execute("SELECT through_sequence FROM event_retention WHERE session_id=?", (session_id,)).fetchone()
    value = {"session_id": session_id, "requested_through": through, "keep_latest": keep,
             "delete_count": count, "prune_through": last or 0, "previous_floor": floor[0] if floor else 0}
    value["preview_digest"] = sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def prune(store, session_id, *, through, keep_latest=100, expected_digest=None):
    if type(through) is not int or through < 0 or type(keep_latest) is not int or not 1 <= keep_latest <= 100000:
        raise ValueError("prune cursor must be nonnegative; keep_latest must be 1-100000")
    if expected_digest is None:
        with store._connection() as connection:
            connection.execute("BEGIN")
            return {**_preview(connection, session_id, through, keep_latest), "applied": False}
    with store.exclusive(session_id), store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        value = _preview(connection, session_id, through, keep_latest)
        if value["preview_digest"] != expected_digest:
            raise ValueError("event retention preview changed; inspect a fresh preview before applying")
        if value["delete_count"]:
            connection.execute("DELETE FROM events WHERE session_id=? AND sequence<=?", (session_id, value["prune_through"]))
            connection.execute("""INSERT INTO event_retention(session_id, through_sequence, deleted_count) VALUES(?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET through_sequence=MAX(through_sequence, excluded.through_sequence),
                deleted_count=deleted_count+excluded.deleted_count""", (session_id, value["prune_through"], value["delete_count"]))
        return {**value, "applied": True}
