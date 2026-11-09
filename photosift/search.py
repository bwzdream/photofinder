"""Search: a small query language + semantic (CLIP) ranking.

Examples
  dog on the beach                 semantic search
  tag:dog after:2023 camera:iphone combine filters with free text
  sunset -tag:people               '-' negates a filter
  album:"Paris 2024" is:favorite   quoted values
  similar:42                       photos that look like photo #42

Filters: tag, album, folder, file, camera, source (local|gdrive), after, before,
year, date, is:favorite, has:gps, has:note, rating (minimum stars), similar.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .db import Database
from .embedder import Embedder

_TOKEN = re.compile(r'(-?)([a-z]+)(:|>=|<=)("[^"]*"|\S+)|"([^"]*)"|(\S+)', re.I)
_KEYS = {"tag", "album", "folder", "file", "camera", "source", "after", "before",
         "year", "date", "is", "has", "rating", "similar"}


@dataclass
class ParsedQuery:
    text: str = ""
    filters: list[tuple[bool, str, str]] = field(default_factory=list)  # (negated, key, value)

    @property
    def similar(self) -> int | None:
        for neg, k, v in self.filters:
            if k == "similar" and not neg and v.isdigit():
                return int(v)
        return None


def parse_query(q: str) -> ParsedQuery:
    out = ParsedQuery()
    words: list[str] = []
    for m in _TOKEN.finditer(q or ""):
        neg, key, op, val, quoted, word = m.groups()
        if key and key.lower() in _KEYS:
            val = val.strip('"')
            if key.lower() == "rating" and op == "<=":
                continue  # only "at least N stars" is supported
            out.filters.append((neg == "-", key.lower(), val))
        elif quoted is not None:
            words.append(quoted)
        else:
            words.append(m.group(0))
    out.text = " ".join(words).strip()
    return out


class SearchEngine:
    def __init__(self, db: Database, embedder_factory: Callable[[], Embedder],
                 version: Callable[[], int]):
        self.db = db
        self._embedder_factory = embedder_factory
        self._version = version
        self._cache_version = -1
        self._ids = np.zeros(0, dtype=np.int64)
        self._matrix = np.zeros((0, 1), dtype=np.float32)
        self._row: dict[int, int] = {}
        self._lock = threading.Lock()
        self._text_cache: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------ embeddings
    def _refresh(self) -> None:
        v = self._version()
        with self._lock:
            if v == self._cache_version:
                return
            rows = self.db.query("SELECT id, embedding FROM photos WHERE embedding IS NOT NULL")
            if rows:
                self._ids = np.array([r["id"] for r in rows], dtype=np.int64)
                self._matrix = np.stack(
                    [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
                )
            else:
                self._ids = np.zeros(0, dtype=np.int64)
                self._matrix = np.zeros((0, 1), dtype=np.float32)
            self._row = {int(i): n for n, i in enumerate(self._ids)}
            self._cache_version = v

    def embedding(self, photo_id: int) -> np.ndarray | None:
        self._refresh()
        n = self._row.get(photo_id)
        return None if n is None else self._matrix[n]

    def _text_vec(self, text: str) -> np.ndarray:
        if text not in self._text_cache:
            emb = self._embedder_factory()
            prompts = [text, f"a photo of {text}."]
            v = emb.encode_texts(prompts).mean(axis=0)
            self._text_cache[text] = v / (np.linalg.norm(v) or 1.0)
            if len(self._text_cache) > 256:
                self._text_cache.pop(next(iter(self._text_cache)))
        return self._text_cache[text]

    # ------------------------------------------------------------ filters
    def _filter_sql(self, pq: ParsedQuery, depth: int = 0) -> tuple[str, list[Any]]:
        clauses, params = [], []
        for neg, key, val in pq.filters:
            sql, p = self._one_filter(key, val, depth)
            if sql is None:
                continue
            clauses.append(f"NOT ({sql})" if neg else f"({sql})")
            params.extend(p)
        return (" AND ".join(clauses) or "1=1"), params

    def _one_filter(self, key: str, val: str, depth: int) -> tuple[str | None, list[Any]]:
        like = f"%{val}%"
        if key == "tag":
            return ("EXISTS (SELECT 1 FROM tags t WHERE t.photo_id=p.id AND t.kind!='rejected' "
                    "AND t.tag = ? COLLATE NOCASE)", [val])
        if key == "album":
            album = self.db.one("SELECT * FROM albums WHERE name = ? COLLATE NOCASE", (val,))
            if album and album["query"] and depth < 3:  # smart album = saved search
                ids = [r["id"] for r in self.search(album["query"], limit=100_000,
                                                    _depth=depth + 1)["items"]]
                return (f"p.id IN ({','.join(str(int(i)) for i in ids) or 'NULL'})", [])
            return ("EXISTS (SELECT 1 FROM album_photos ap JOIN albums a ON a.id=ap.album_id "
                    "WHERE ap.photo_id=p.id AND a.name = ? COLLATE NOCASE)", [val])
        if key == "folder":
            return "p.folder LIKE ?", [like]
        if key == "file":
            return "p.filename LIKE ?", [like]
        if key == "camera":
            return "p.camera LIKE ?", [like]
        if key == "source":
            return "p.kind = ?", [val.lower()]
        if key == "after":
            return "p.taken_at >= ?", [val]
        if key == "before":
            return "p.taken_at < ?", [val]
        if key in ("year", "date"):
            return "p.taken_at LIKE ?", [f"{val}%"]
        if key == "rating" and val.isdigit():
            return "p.rating >= ?", [int(val)]
        if key == "is":
            v = val.lower()
            if v in ("favorite", "fav", "favourite", "starred"):
                return "p.favorite = 1", []
            if v == "duplicate":
                return ("p.checksum IN (SELECT checksum FROM photos GROUP BY checksum "
                        "HAVING COUNT(*) > 1)", [])
            if v == "untagged":
                return ("NOT EXISTS (SELECT 1 FROM tags t WHERE t.photo_id=p.id "
                        "AND t.kind='manual')", [])
            return ("EXISTS (SELECT 1 FROM tags t WHERE t.photo_id=p.id AND t.kind!='rejected' "
                    "AND t.tag = ? COLLATE NOCASE)", [val])
        if key == "has":
            v = val.lower()
            if v in ("gps", "location"):
                return "p.lat IS NOT NULL", []
            if v == "note":
                return "p.note != ''", []
            if v == "camera":
                return "p.camera IS NOT NULL", []
        return None, []

    def _lexical_hits(self, text: str, candidate_sql: str, params: list[Any]) -> set[int]:
        """Photos whose tags, filename, folder or note mention every query word."""
        words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1]
        if not words:
            return set()
        conds, p = [], list(params)
        for w in words:
            conds.append(
                "(p.filename LIKE ? OR p.folder LIKE ? OR p.note LIKE ? OR EXISTS ("
                "SELECT 1 FROM tags t WHERE t.photo_id=p.id AND t.kind!='rejected' "
                "AND (t.tag = ? COLLATE NOCASE OR t.tag LIKE ?)))"
            )
            p += [f"%{w}%", f"%{w}%", f"%{w}%", w, f"{w} %"]
        rows = self.db.query(
            f"SELECT p.id FROM photos p WHERE {candidate_sql} AND {' AND '.join(conds)}", p
        )
        return {r["id"] for r in rows}

    # ------------------------------------------------------------ search
    def search(self, q: str = "", sort: str = "auto", offset: int = 0, limit: int = 200,
               strictness: float = 0.07, _depth: int = 0) -> dict[str, Any]:
        t0 = time.time()
        pq = parse_query(q)
        where, params = self._filter_sql(pq, _depth)
        rows = self.db.query(
            "SELECT p.id, p.filename, p.width, p.height, p.taken_at, p.favorite, p.rating, "
            f"p.kind, p.size FROM photos p WHERE {where}", params
        )
        scores: dict[int, float] = {}
        semantic = bool(pq.text) or pq.similar is not None
        if semantic and rows:
            self._refresh()
            if pq.similar is not None:
                qv = self.embedding(pq.similar)
                min_score = 0.0 if self._is_fake() else 0.70
                margin = 1.0
                lexical: set[int] = set()
            else:
                qv = self._text_vec(pq.text)
                min_score = 0.0 if self._is_fake() else 0.16
                margin = strictness
                lexical = self._lexical_hits(pq.text, where, params)
            if qv is not None and len(self._ids):
                cand = np.array([r["id"] for r in rows], dtype=np.int64)
                idx = [self._row[i] for i in cand.tolist() if i in self._row]
                sims = self._matrix[idx] @ qv if idx else np.zeros(0)
                cand_ids = [i for i in cand.tolist() if i in self._row]
                for pid, s in zip(cand_ids, sims.tolist()):
                    scores[pid] = s
                if pq.similar is not None:
                    scores.pop(pq.similar, None)
                semantic_only = [v for k, v in scores.items() if k not in lexical]
                if semantic_only:
                    floor = max(min_score, max(semantic_only) - margin)
                    scores = {k: v for k, v in scores.items() if v >= floor or k in lexical}
            else:  # no embeddings yet: fall back to keyword matching
                lexical = self._lexical_hits(pq.text, where, params) if pq.text else set()
                scores = {pid: 1.0 for pid in lexical}
            rows = [r for r in rows if r["id"] in scores]
            for r in rows:
                r["score"] = round(scores[r["id"]], 4)
                # Exact word matches in your tags / notes / file names rank first.
                r["match"] = r["id"] in lexical

        if sort == "auto":
            sort = "relevance" if semantic else "newest"
        keyfns = {
            "relevance": (lambda r: (not r.get("match", False), -r.get("score", 0.0))),
            "newest": (lambda r: r["taken_at"] or ""),
            "oldest": (lambda r: r["taken_at"] or "9999"),
            "name": (lambda r: r["filename"].lower()),
            "size": (lambda r: r["size"] or 0),
        }
        rows.sort(key=keyfns.get(sort, keyfns["newest"]), reverse=sort in ("newest", "size"))
        return {
            "query": q,
            "text": pq.text,
            "filters": [{"neg": n, "key": k, "value": v} for n, k, v in pq.filters],
            "sort": sort,
            "total": len(rows),
            "offset": offset,
            "items": rows[offset: offset + limit],
            "took_ms": round((time.time() - t0) * 1000, 1),
        }

    def _is_fake(self) -> bool:
        return getattr(self._embedder_factory(), "name", "") == "fake"

    # ------------------------------------------------------------ duplicates
    def duplicates(self, max_distance: int = 5, min_cosine: float = 0.92) -> list[list[int]]:
        """Groups of exact (same checksum) and near-duplicate (similar dHash) photos."""
        rows = self.db.query("SELECT id, checksum, dhash FROM photos WHERE dhash IS NOT NULL")
        parent = {r["id"]: r["id"] for r in rows}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        by_sum: dict[str, int] = {}
        for r in rows:
            if r["checksum"]:
                if r["checksum"] in by_sum:
                    union(by_sum[r["checksum"]], r["id"])
                else:
                    by_sum[r["checksum"]] = r["id"]

        if len(rows) > 1:
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            hashes = np.array([int(r["dhash"], 16) for r in rows], dtype=np.uint64)
            self._refresh()
            for start in range(0, len(rows), 2048):
                block = hashes[start:start + 2048]
                dist = np.bitwise_count(block[:, None] ^ hashes[None, :])
                ii, jj = np.nonzero(dist <= max_distance)
                for i, j in zip(ii.tolist(), jj.tolist()):
                    a, b = int(ids[start + i]), int(ids[j])
                    if a >= b:
                        continue
                    ea, eb = self.embedding(a), self.embedding(b)
                    if ea is not None and eb is not None and float(ea @ eb) < min_cosine:
                        continue  # same rough layout but different content
                    union(a, b)

        groups: dict[int, list[int]] = {}
        for pid in parent:
            groups.setdefault(find(pid), []).append(pid)
        return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: -len(g))
