"""Persistent state: the approval queue, per-person dedupe, and an audit log.

Everything is one SQLite file (stdlib only). Three concerns:

- `queue`      — drafted replies awaiting human approval (and their outcome).
- `contacted`  — every DID we've ever replied to, so we never contact twice.
- `audit`      — append-only log of every decision (queued / sent / skipped),
                 including *why* something was skipped (e.g. crisis-excluded).
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    did         TEXT NOT NULL,
    rkey        TEXT NOT NULL,
    cid         TEXT,
    post_uri    TEXT NOT NULL,
    post_url    TEXT,
    post_text   TEXT NOT NULL,
    draft_text  TEXT NOT NULL,
    confidence  REAL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|skipped
    reply_uri   TEXT,
    enqueued_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS contacted (
    did          TEXT PRIMARY KEY,
    post_uri     TEXT,
    reply_uri    TEXT,
    contacted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    action   TEXT NOT NULL,
    did      TEXT,
    post_uri TEXT,
    detail   TEXT
);
CREATE TABLE IF NOT EXISTS daily_posts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    post_date TEXT NOT NULL,
    uri       TEXT,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_posts(post_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = "responder.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # --- dedupe -----------------------------------------------------------
    def already_contacted(self, did: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM contacted WHERE did = ?", (did,))
        return cur.fetchone() is not None

    def is_queued(self, did: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM queue WHERE did = ? AND status = 'pending'", (did,)
        )
        return cur.fetchone() is not None

    # --- queue ------------------------------------------------------------
    def enqueue(self, candidate: Dict) -> Optional[int]:
        """Add a drafted reply to the approval queue. Returns row id, or None
        if this DID is already contacted or already queued (dedupe)."""
        did = candidate["did"]
        if self.already_contacted(did) or self.is_queued(did):
            return None
        cur = self.conn.execute(
            """INSERT INTO queue
               (did, rkey, cid, post_uri, post_url, post_text, draft_text,
                confidence, enqueued_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                did,
                candidate["rkey"],
                candidate.get("cid"),
                candidate["uri"],
                candidate.get("url"),
                candidate["text"],
                candidate["draft_text"],
                candidate.get("confidence"),
                _now(),
            ),
        )
        self.conn.commit()
        self.log("queued", did, candidate["uri"], f"conf={candidate.get('confidence')}")
        return cur.lastrowid

    def list_pending(self) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM queue WHERE status = 'pending' ORDER BY confidence DESC, id ASC"
        )
        return cur.fetchall()

    def get_pending(self, queue_id: int) -> Optional[sqlite3.Row]:
        """Fetch a single still-pending draft by id, or None if it's gone or
        already resolved (e.g. a stale Telegram tap for an old draft)."""
        cur = self.conn.execute(
            "SELECT * FROM queue WHERE id = ? AND status = 'pending'", (queue_id,)
        )
        return cur.fetchone()

    def mark_sent(self, queue_id: int, did: str, post_uri: str, reply_uri: str) -> None:
        self.conn.execute(
            "UPDATE queue SET status='sent', reply_uri=?, resolved_at=? WHERE id=?",
            (reply_uri, _now(), queue_id),
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO contacted (did, post_uri, reply_uri, contacted_at)
               VALUES (?,?,?,?)""",
            (did, post_uri, reply_uri, _now()),
        )
        self.conn.commit()
        self.log("sent", did, post_uri, reply_uri)

    def mark_skipped(self, queue_id: int, did: str, post_uri: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE queue SET status='skipped', resolved_at=? WHERE id=?",
            (_now(), queue_id),
        )
        self.conn.commit()
        self.log("skipped", did, post_uri, reason)

    # --- audit ------------------------------------------------------------
    def log(self, action: str, did: Optional[str], post_uri: Optional[str],
            detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, action, did, post_uri, detail) VALUES (?,?,?,?,?)",
            (_now(), action, did, post_uri, detail),
        )
        self.conn.commit()

    def stats(self) -> Dict[str, int]:
        out = {}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) c FROM queue GROUP BY status"
        ):
            out[row["status"]] = row["c"]
        out["contacted_total"] = self.conn.execute(
            "SELECT COUNT(*) c FROM contacted"
        ).fetchone()["c"]
        return out

    # --- daily content posting -------------------------------------------
    def posted_today(self, post_date: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM daily_posts WHERE post_date = ?", (post_date,)
        )
        return cur.fetchone()

    def daily_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM daily_posts"
        ).fetchone()["c"]

    def record_daily_post(self, idx: int, text: str, post_date: str, uri: str) -> None:
        self.conn.execute(
            """INSERT INTO daily_posts (idx, text, post_date, uri, posted_at)
               VALUES (?,?,?,?,?)""",
            (idx, text, post_date, uri, _now()),
        )
        self.conn.commit()
        self.log("daily_post", None, uri, f"idx={idx}")

    def close(self) -> None:
        self.conn.close()
