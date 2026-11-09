"""SQLite storage for photos, tags, albums and sources."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- 'local' | 'gdrive'
    location    TEXT NOT NULL,              -- folder path, or Drive folder id ('root' = whole Drive)
    label       TEXT NOT NULL,
    last_scan   REAL,
    UNIQUE(kind, location)
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- 'local' | 'gdrive'
    key         TEXT NOT NULL,              -- absolute path, or Drive file id
    path        TEXT NOT NULL,              -- display path
    filename    TEXT NOT NULL,
    folder      TEXT NOT NULL,
    size        INTEGER,
    mtime       REAL,
    checksum    TEXT,                       -- sha256 (local) or md5 (Drive)
    dhash       TEXT,                       -- 64-bit perceptual hash, hex
    width       INTEGER,
    height      INTEGER,
    taken_at    TEXT,                       -- ISO 8601
    camera      TEXT,
    lat         REAL,
    lon         REAL,
    web_url     TEXT,
    embedding   BLOB,                       -- float32 CLIP vector, L2-normalised
    favorite    INTEGER NOT NULL DEFAULT 0,
    rating      INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    indexed_at  REAL,
    UNIQUE(kind, key)
);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_checksum ON photos(checksum);
CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source_id);

CREATE TABLE IF NOT EXISTS tags (
    photo_id    INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- 'auto' | 'manual' | 'rejected'
    score       REAL,
    PRIMARY KEY (photo_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS albums (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    query       TEXT,                       -- non-null = smart album (saved search)
    cover_id    INTEGER REFERENCES photos(id) ON DELETE SET NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS album_photos (
    album_id    INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    photo_id    INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    added_at    REAL NOT NULL,
    PRIMARY KEY (album_id, photo_id)
);
"""

# Columns returned to the UI (everything except the embedding blob).
PHOTO_COLUMNS = (
    "id, source_id, kind, key, path, filename, folder, size, mtime, checksum, dhash, "
    "width, height, taken_at, camera, lat, lon, web_url, favorite, rating, note, indexed_at"
)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.tx() as c:
            return c.execute(sql, params)

    # ---------------------------------------------------------------- sources
    def add_source(self, kind: str, location: str, label: str) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO sources(kind, location, label) VALUES (?,?,?)",
                (kind, location, label),
            )
            return c.execute(
                "SELECT id FROM sources WHERE kind=? AND location=?", (kind, location)
            ).fetchone()[0]

    def sources(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT s.*, (SELECT COUNT(*) FROM photos p WHERE p.source_id=s.id) AS photo_count "
            "FROM sources s ORDER BY s.id"
        )

    # ---------------------------------------------------------------- photos
    def photo(self, photo_id: int) -> dict[str, Any] | None:
        p = self.one(f"SELECT {PHOTO_COLUMNS} FROM photos WHERE id=?", (photo_id,))
        if p:
            p["tags"] = self.query(
                "SELECT tag, kind, score FROM tags WHERE photo_id=? AND kind!='rejected' "
                "ORDER BY kind='auto', score DESC, tag",
                (photo_id,),
            )
            p["albums"] = self.query(
                "SELECT a.id, a.name FROM albums a JOIN album_photos ap ON ap.album_id=a.id "
                "WHERE ap.photo_id=? ORDER BY a.name",
                (photo_id,),
            )
        return p

    def upsert_photo(self, rec: dict[str, Any]) -> int:
        cols = list(rec.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("kind", "key"))
        with self.tx() as c:
            c.execute(
                f"INSERT INTO photos({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(kind, key) DO UPDATE SET {updates}",
                [rec[k] for k in cols],
            )
            return c.execute(
                "SELECT id FROM photos WHERE kind=? AND key=?", (rec["kind"], rec["key"])
            ).fetchone()[0]

    def set_auto_tags(self, photo_id: int, tags: list[tuple[str, float]]) -> None:
        """Replace auto tags, keeping manual tags and honouring user rejections."""
        with self.tx() as c:
            c.execute("DELETE FROM tags WHERE photo_id=? AND kind='auto'", (photo_id,))
            c.executemany(
                "INSERT OR IGNORE INTO tags(photo_id, tag, kind, score) VALUES (?,?, 'auto', ?)",
                [(photo_id, t, s) for t, s in tags],
            )

    def add_tag(self, photo_ids: list[int], tag: str) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT INTO tags(photo_id, tag, kind, score) VALUES (?,?,'manual',NULL) "
                "ON CONFLICT(photo_id, tag) DO UPDATE SET kind='manual'",
                [(pid, tag) for pid in photo_ids],
            )

    def remove_tag(self, photo_ids: list[int], tag: str) -> None:
        # Mark as rejected so re-indexing will not bring an auto tag back.
        with self.tx() as c:
            c.executemany(
                "INSERT INTO tags(photo_id, tag, kind, score) VALUES (?,?,'rejected',NULL) "
                "ON CONFLICT(photo_id, tag) DO UPDATE SET kind='rejected'",
                [(pid, tag) for pid in photo_ids],
            )

    def tag_counts(self, limit: int = 300) -> list[dict[str, Any]]:
        return self.query(
            "SELECT tag, COUNT(*) AS n, SUM(kind='manual') AS manual FROM tags "
            "WHERE kind!='rejected' GROUP BY tag ORDER BY n DESC, tag LIMIT ?",
            (limit,),
        )

    # ---------------------------------------------------------------- albums
    def albums(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT a.*, (SELECT COUNT(*) FROM album_photos ap WHERE ap.album_id=a.id) AS photo_count, "
            "COALESCE(a.cover_id, (SELECT photo_id FROM album_photos ap WHERE ap.album_id=a.id "
            "ORDER BY added_at LIMIT 1)) AS cover "
            "FROM albums a ORDER BY a.name COLLATE NOCASE"
        )

    def create_album(self, name: str, query: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO albums(name, query, created_at) VALUES (?,?,?)",
                (name, query, time.time()),
            )
            return cur.lastrowid

    def add_to_album(self, album_id: int, photo_ids: list[int]) -> None:
        now = time.time()
        with self.tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO album_photos(album_id, photo_id, added_at) VALUES (?,?,?)",
                [(album_id, pid, now) for pid in photo_ids],
            )

    def remove_from_album(self, album_id: int, photo_ids: list[int]) -> None:
        with self.tx() as c:
            c.executemany(
                "DELETE FROM album_photos WHERE album_id=? AND photo_id=?",
                [(album_id, pid) for pid in photo_ids],
            )

    def stats(self) -> dict[str, Any]:
        return self.one(
            "SELECT COUNT(*) AS photos, SUM(embedding IS NOT NULL) AS embedded, "
            "SUM(favorite) AS favorites, SUM(size) AS bytes, "
            "MIN(taken_at) AS first, MAX(taken_at) AS last FROM photos"
        ) or {}
