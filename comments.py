"""Per-ad comments, stored in SQLite.

On Railway, attach a Volume to the service: Railway sets RAILWAY_VOLUME_MOUNT_PATH and the database lives there,
so comments survive redeploys. Without a volume the file sits in the container and is lost on the next deploy.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_NAME, MAX_TEXT = 80, 2000
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or Path(__file__).parent / "data")
PERSISTENT = bool(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"))
_lock = threading.Lock()


def _db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA_DIR / "comments.db")
    con.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, ad TEXT NOT NULL,
        name TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS comments_ad ON comments (client, ad)")
    return con


def _run(sql, args=()):
    with _lock:
        con = _db()
        try:
            rows = con.execute(sql, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()


def list_for(client, ad):
    rows = _run("SELECT id, name, text, created FROM comments WHERE client = ? AND ad = ? ORDER BY id DESC",
                (client, ad))
    return [{"id": r[0], "name": r[1], "text": r[2], "at": r[3]} for r in rows]


def add(client, ad, name, text):
    """Returns an error message, or None when the comment was saved."""
    ad, name, text = str(ad or "").strip(), str(name or "").strip(), str(text or "").strip()
    if not ad:
        return "Missing ad."
    if not name:
        return "Add your name so others know who wrote the comment."
    if not text:
        return "Write a comment first."
    if len(name) > MAX_NAME or len(text) > MAX_TEXT:
        return f"Keep names under {MAX_NAME} characters and comments under {MAX_TEXT}."
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _run("INSERT INTO comments (client, ad, name, text, created) VALUES (?, ?, ?, ?, ?)",
         (client, ad, name, text, created))
    return None


def after(client, last_id, limit=50):
    """Comments newer than last_id (oldest first), for the Apps Script that emails the team."""
    rows = _run("SELECT id, ad, name, text, created FROM comments WHERE client = ? AND id > ? ORDER BY id LIMIT ?",
                (client, int(last_id), limit))
    return [{"id": r[0], "ad": r[1], "name": r[2], "text": r[3], "at": r[4]} for r in rows]


def delete(client, ids):
    """Remove comments by id (admin clean-up); returns how many were deleted."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    before = len(_run(f"SELECT id FROM comments WHERE client = ? AND id IN ({','.join('?' * len(ids))})", (client, *ids)))
    _run(f"DELETE FROM comments WHERE client = ? AND id IN ({','.join('?' * len(ids))})", (client, *ids))
    return before
