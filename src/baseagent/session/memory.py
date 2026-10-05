"""Process-local session storage using the same transactional execution ledger."""

from contextlib import contextmanager
import sqlite3
import threading
from uuid import uuid4

from .store import SessionBusy, SessionStore


class MemorySessionStore(SessionStore):
    durable = False
    path = lock_dir = None

    def __init__(self):
        self._uri = f"file:baseagent-{uuid4().hex}?mode=memory&cache=shared"
        self._guard = threading.RLock()
        self._lock_owners = threading.local()
        self._sessions = set()
        self._active = threading.local()
        self._closed = False
        self._keeper = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _check_open(self):
        if self._closed:
            raise RuntimeError("memory session store is closed")

    @contextmanager
    def _connection(self):
        # Fresh connections keep operation transactions independent. Serializing
        # access avoids shared-cache table-lock failures across host threads.
        with self._guard:
            self._check_open()
            if getattr(self._active, "connection", False):
                raise RuntimeError("nested memory store transactions are unsupported")
            connection = sqlite3.connect(self._uri, uri=True, isolation_level="IMMEDIATE")
            self._active.connection = True
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA temp_store=MEMORY")
                with connection:
                    yield connection
            finally:
                connection.close()
                self._active.connection = False

    @contextmanager
    def exclusive(self, session_id):
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
            raise ValueError("session_id must be 1-128 nonblank characters")
        with self._guard:
            self._check_open()
            if session_id in self._sessions:
                raise SessionBusy(f"session is already running: {session_id}")
            self._sessions.add(session_id)
        owned = getattr(self._lock_owners, "sessions", set())
        self._lock_owners.sessions = owned | {session_id}
        try:
            yield
        finally:
            self._lock_owners.sessions = owned
            with self._guard:
                self._sessions.remove(session_id)

    def close(self):
        with self._guard:
            if self._closed:
                return
            if self._sessions or getattr(self._active, "connection", False):
                raise SessionBusy("cannot close memory store during an active operation")
            self._keeper.close()
            self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *exc):
        self.close()
