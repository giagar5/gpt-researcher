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
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    turn_id TEXT REFERENCES turns(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    ext TEXT NOT NULL,
    lang TEXT NOT NULL,
    size INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_turn ON uploads(turn_id);
"""

ACTIVE = ("queued", "running")
UPLOAD_PUBLIC = ("id", "kind", "name", "size", "received", "state", "summary", "chars", "meta", "error")


def new_id():
    return uuid.uuid4().hex[:16]


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
            if "brief" not in columns:
                conn.execute("ALTER TABLE turns ADD COLUMN brief TEXT NOT NULL DEFAULT ''")
            for name in ("tokens_in", "tokens_out", "llm_calls"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE turns ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            if "attachments" not in columns:
                conn.execute("ALTER TABLE turns ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            if "location" not in columns:
                conn.execute("ALTER TABLE turns ADD COLUMN location TEXT NOT NULL DEFAULT ''")

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
        turn["attachments"] = json.loads(turn.get("attachments") or "[]")
        turn["location"] = json.loads(turn["location"]) if turn.get("location") else None
        return turn

    @staticmethod
    def _upload(row):
        upload = dict(row)
        upload["meta"] = json.loads(upload["meta"] or "{}")
        return upload

    def create_thread(self, title):
        now = self.clock()
        thread_id = new_id()
        with self.connect() as conn:
            conn.execute("INSERT INTO threads(id,title,created,updated) VALUES (?,?,?,?)",
                         (thread_id, title[:160], now, now))
        return thread_id

    def add_turn(self, thread_id, *, kind, question, mode, lang, tone, max_results, attachments=(), location=None):
        """attachments: ids of finished, unbound uploads; they are bound to the new turn atomically."""
        now = self.clock()
        turn_id = new_id()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
                    raise KeyError(thread_id)
                snapshot = []
                for upload_id in attachments:
                    row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
                    if row is None or row["turn_id"] is not None or row["state"] not in ("processing", "ready"):
                        raise ValueError(f"attachment {upload_id}")
                    snapshot.append({"id": row["id"], "kind": row["kind"], "name": row["name"], "size": row["size"]})
                seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE thread_id=?",
                                   (thread_id,)).fetchone()[0]
                conn.execute(
                    "INSERT INTO turns(id,thread_id,seq,kind,question,mode,lang,tone,max_results,status,created,"
                    "attachments,location) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                    (turn_id, thread_id, seq, kind, question, mode, lang, tone, max_results, now,
                     json.dumps(snapshot, ensure_ascii=False),
                     json.dumps(location, ensure_ascii=False) if location else ""))
                for item in snapshot:
                    conn.execute("UPDATE uploads SET turn_id=?, updated=? WHERE id=?", (turn_id, now, item["id"]))
                conn.execute("UPDATE threads SET updated=? WHERE id=?", (now, thread_id))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return turn_id

    def update_turn(self, turn_id, **fields):
        allowed = {"status", "answer", "sources", "files", "error", "started", "finished", "lang", "brief",
                   "tokens_in", "tokens_out", "llm_calls"}
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
        totals = {key: sum(t[key] for t in turns) for key in ("tokens_in", "tokens_out", "llm_calls")}
        return {**dict(row), "turns": turns, **totals}

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
        """Returns (deleted, report file groups, upload ids) so the caller can remove files on disk."""
        with self.connect() as conn:
            files = [json.loads(r[0] or "{}") for r in conn.execute(
                "SELECT files FROM turns WHERE thread_id=?", (thread_id,))]
            uploads = [r[0] for r in conn.execute(
                "SELECT u.id FROM uploads u JOIN turns t ON u.turn_id=t.id WHERE t.thread_id=?", (thread_id,))]
            deleted = conn.execute("DELETE FROM threads WHERE id=?", (thread_id,)).rowcount
        return bool(deleted), files, uploads

    # ---- uploads ----
    def create_upload(self, *, kind, name, mime, ext, lang, size):
        now = self.clock()
        upload_id = new_id()
        with self.connect() as conn:
            conn.execute("INSERT INTO uploads(id,kind,name,mime,ext,lang,size,state,created,updated)"
                         " VALUES (?,?,?,?,?,?,?,'uploading',?,?)",
                         (upload_id, kind, name, mime, ext, lang, size, now, now))
        return upload_id

    def upload(self, upload_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        return None if row is None else self._upload(row)

    def add_received(self, upload_id, offset, count):
        """Appends only at the expected offset, so a retried or reordered chunk cannot corrupt the file."""
        with self.connect() as conn:
            return conn.execute(
                "UPDATE uploads SET received=received+?, updated=? WHERE id=? AND received=? AND state='uploading'"
                " AND received+?<=size", (count, self.clock(), upload_id, offset, count)).rowcount == 1

    def set_upload(self, upload_id, **fields):
        allowed = {"state", "summary", "chars", "meta", "error", "tokens_in", "tokens_out", "llm_calls"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid upload fields")
        values = {k: json.dumps(v, ensure_ascii=False) if k == "meta" else v for k, v in fields.items()}
        assignments = ",".join(f"{name}=?" for name in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE uploads SET {assignments}, updated=? WHERE id=?",
                         (*values.values(), self.clock(), upload_id))

    def turn_uploads(self, turn_id):
        with self.connect() as conn:
            return [self._upload(r) for r in conn.execute(
                "SELECT * FROM uploads WHERE turn_id=? ORDER BY created", (turn_id,))]

    def delete_upload(self, upload_id):
        with self.connect() as conn:
            return conn.execute("DELETE FROM uploads WHERE id=? AND turn_id IS NULL", (upload_id,)).rowcount == 1

    def pending_bytes(self):
        with self.connect() as conn:
            return conn.execute("SELECT COALESCE(SUM(size),0) FROM uploads WHERE turn_id IS NULL").fetchone()[0]

    def stale_uploads(self, unbound_before, uploading_before):
        with self.connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT id FROM uploads WHERE (turn_id IS NULL AND created<?) OR (state='uploading' AND updated<?)",
                (unbound_before, uploading_before))]

    def forget_uploads(self, ids):
        with self.connect() as conn:
            for upload_id in ids:
                conn.execute("DELETE FROM uploads WHERE id=?", (upload_id,))

    def processing_uploads(self):
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT id FROM uploads WHERE state='processing'")]

    def all_upload_ids(self):
        with self.connect() as conn:
            return {r[0] for r in conn.execute("SELECT id FROM uploads")}

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
