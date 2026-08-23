"""Which repo a chat is working on, and which conversation it is in.

A session is just a LangGraph `thread_id` plus the directory it belongs to.
The agent's actual memory lives in the checkpointer; this table only records
what exists, so the phone can list and switch between sessions.

The point of persisting this at all: a rebooted machine must not lose your
work. The stack this replaces could spawn a session but never resume one,
so a reboot turned every session in the app into a dead end.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id   TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    workdir     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    last_used   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id            INTEGER PRIMARY KEY,
    current_thread_id  TEXT,
    autonomous         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    thread_id     TEXT NOT NULL,
    at            REAL NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Session:
    thread_id: str
    chat_id: int
    workdir: Path
    title: str
    created_at: float
    last_used: float


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        """Release the SQLite file.

        Windows keeps an open handle locked, so anything that creates a store
        in a temporary directory (the tests) cannot clean up without this.
        """
        self.conn.close()

    # -- sessions ---------------------------------------------------------
    def create(self, chat_id: int, workdir: Path, title: str = "") -> Session:
        now = time.time()
        thread_id = f"{Path(workdir).name}-{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            (thread_id, chat_id, str(workdir), title, now, now),
        )
        self.conn.commit()
        self.set_current(chat_id, thread_id)
        return Session(thread_id, chat_id, Path(workdir), title, now, now)

    def get(self, thread_id: str) -> Session | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return self._row(row) if row else None

    def list(self, chat_id: int, limit: int = 10) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE chat_id=? ORDER BY last_used DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def find(self, chat_id: int, selector: str) -> list[Session]:
        """Substring match over thread id and title, newest first.

        Same selection style as MusicLore's /retitle: an ambiguous selector
        lists the matches rather than guessing, because guessing wrong here
        means resuming the wrong piece of work.
        """
        needle = selector.strip().lower()
        return [
            s for s in self.list(chat_id, limit=100)
            if needle in s.thread_id.lower() or needle in s.title.lower()
        ]

    def touch(self, thread_id: str, title: str | None = None) -> None:
        if title:
            self.conn.execute(
                "UPDATE sessions SET last_used=?, title=? WHERE thread_id=?",
                (time.time(), title[:80], thread_id),
            )
        else:
            self.conn.execute(
                "UPDATE sessions SET last_used=? WHERE thread_id=?",
                (time.time(), thread_id),
            )
        self.conn.commit()

    # -- per-chat state ---------------------------------------------------
    def current(self, chat_id: int) -> Session | None:
        row = self.conn.execute(
            "SELECT current_thread_id FROM chat_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return self.get(row["current_thread_id"]) if row and row["current_thread_id"] else None

    def set_current(self, chat_id: int, thread_id: str) -> None:
        self.conn.execute(
            "INSERT INTO chat_state (chat_id, current_thread_id) VALUES (?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET current_thread_id=excluded.current_thread_id",
            (chat_id, thread_id),
        )
        self.conn.commit()

    def autonomous(self, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT autonomous FROM chat_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return bool(row["autonomous"]) if row else False

    def set_autonomous(self, chat_id: int, value: bool) -> None:
        self.conn.execute(
            "INSERT INTO chat_state (chat_id, autonomous) VALUES (?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET autonomous=excluded.autonomous",
            (chat_id, int(value)),
        )
        self.conn.commit()

    # -- usage ------------------------------------------------------------
    def record_usage(self, thread_id: str, model: str, inp: int, out: int) -> None:
        self.conn.execute(
            "INSERT INTO usage VALUES (?,?,?,?,?)",
            (thread_id, time.time(), model, inp, out),
        )
        self.conn.commit()

    def usage_totals(self, thread_id: str | None = None) -> dict[str, tuple[int, int]]:
        """{model: (input_tokens, output_tokens)}; whole account if no thread."""
        if thread_id:
            rows = self.conn.execute(
                "SELECT model, SUM(input_tokens) i, SUM(output_tokens) o "
                "FROM usage WHERE thread_id=? GROUP BY model", (thread_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT model, SUM(input_tokens) i, SUM(output_tokens) o "
                "FROM usage GROUP BY model"
            ).fetchall()
        return {r["model"]: (r["i"] or 0, r["o"] or 0) for r in rows}

    @staticmethod
    def _row(row: sqlite3.Row) -> Session:
        return Session(
            thread_id=row["thread_id"], chat_id=row["chat_id"],
            workdir=Path(row["workdir"]), title=row["title"],
            created_at=row["created_at"], last_used=row["last_used"],
        )
