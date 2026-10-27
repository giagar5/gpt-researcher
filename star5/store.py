"""SQLite storage for research threads (Perplexity-style conversations)."""

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    mode TEXT NOT NULL,
    lang TEXT NOT NULL,
    tone TEXT NOT NULL,
    max_results INTEGER NOT NULL,
    status TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS turns_thread ON turns(thread_id, seq);
"""

ACTIVE = ("queued", "running")


def new_id():
    return uuid.uuid4().hex[:16]


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return closing(conn)

    @staticmethod
    def _turn(row):
        turn = dict(row)
        turn["sources"] = json.loads(turn["sources"] or "[]")
        turn["files"] = json.loads(turn["files"] or "{}")
        return turn

    def create_thread(self, title):
        now = self.clock()
        thread_id = new_id()
        with self.connect() as conn:
            conn.execute("INSERT INTO threads(id,title,created,updated) VALUES (?,?,?,?)",
                         (thread_id, title[:160], now, now))
        return thread_id

    def add_turn(self, thread_id, *, kind, question, mode, lang, tone, max_results):
        now = self.clock()
        turn_id = new_id()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
                    raise KeyError(thread_id)
                seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE thread_id=?",
                                   (thread_id,)).fetchone()[0]
                conn.execute(
                    "INSERT INTO turns(id,thread_id,seq,kind,question,mode,lang,tone,max_results,status,created)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'queued',?)",
                    (turn_id, thread_id, seq, kind, question, mode, lang, tone, max_results, now))
                conn.execute("UPDATE threads SET updated=? WHERE id=?", (now, thread_id))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return turn_id

    def update_turn(self, turn_id, **fields):
        allowed = {"status", "answer", "sources", "files", "error", "started", "finished", "lang"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid turn fields")
        values = {k: json.dumps(v, ensure_ascii=False) if k in ("sources", "files") else v
                  for k, v in fields.items()}
        assignments = ",".join(f"{name}=?" for name in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE turns SET {assignments} WHERE id=?", (*values.values(), turn_id))
            conn.execute("UPDATE threads SET updated=? WHERE id=(SELECT thread_id FROM turns WHERE id=?)",
                         (self.clock(), turn_id))

    def turn(self, turn_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return None if row is None else self._turn(row)

    def thread(self, thread_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if row is None:
                return None
            turns = [self._turn(r) for r in conn.execute(
                "SELECT * FROM turns WHERE thread_id=? ORDER BY seq", (thread_id,))]
        return {**dict(row), "turns": turns}

    def threads(self, limit=200):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT t.id,t.title,t.created,t.updated,"
                " (SELECT status FROM turns WHERE thread_id=t.id ORDER BY seq DESC LIMIT 1) AS status,"
                " (SELECT mode FROM turns WHERE thread_id=t.id ORDER BY seq LIMIT 1) AS mode,"
                " (SELECT COUNT(*) FROM turns WHERE thread_id=t.id) AS turns"
                " FROM threads t ORDER BY t.updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_thread(self, thread_id):
        with self.connect() as conn:
            files = [json.loads(r[0] or "{}") for r in conn.execute(
                "SELECT files FROM turns WHERE thread_id=?", (thread_id,))]
            deleted = conn.execute("DELETE FROM threads WHERE id=?", (thread_id,)).rowcount
        return bool(deleted), files

    def history(self, thread_id, before_seq):
        with self.connect() as conn:
            return [self._turn(r) for r in conn.execute(
                "SELECT * FROM turns WHERE thread_id=? AND seq<? ORDER BY seq", (thread_id, before_seq))]

    def fail_interrupted(self):
        """After a restart nothing is running any more: mark leftovers instead of leaving them spinning."""
        with self.connect() as conn:
            return conn.execute(
                "UPDATE turns SET status='error', error='interrupted', finished=? WHERE status IN (?,?)",
                (self.clock(), *ACTIVE)).rowcount
