"""Background indexing: scan sources, extract metadata, embed and tag photos."""

from __future__ import annotations

import hashlib
import io
import logging
import queue
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import metadata as md
from .config import Config
from .db import Database
from .embedder import Embedder
from .sources import SourceItem
from .sources.gdrive import iter_gdrive
from .sources.local import iter_local
from .tagger import Tagger

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    title: str
    state: str = "queued"          # queued | running | done | failed | cancelled
    total: int = 0
    done: int = 0
    skipped: int = 0
    added: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    current: str = ""
    started: float | None = None
    finished: float | None = None
    cancel: bool = False

    def view(self) -> dict[str, Any]:
        d = asdict(self)
        d["errors"] = self.errors[-20:]
        d["error_count"] = len(self.errors)
        return d


class Indexer:
    def __init__(self, cfg: Config, db: Database, embedder_factory: Callable[[], Embedder]):
        self.cfg = cfg
        self.db = db
        self._embedder_factory = embedder_factory
        self._tagger: Tagger | None = None
        self.jobs: dict[int, Job] = {}
        self._queue: "queue.Queue[tuple[Job, Callable[[Job], None]]]" = queue.Queue()
        self._next_id = 1
        self._lock = threading.Lock()
        self.version = 0  # bumps whenever photos change, so search can refresh caches
        threading.Thread(target=self._worker, daemon=True, name="indexer").start()

    # ------------------------------------------------------------- job queue
    def submit(self, title: str, fn: Callable[[Job], None]) -> Job:
        with self._lock:
            job = Job(id=self._next_id, title=title)
            self._next_id += 1
            self.jobs[job.id] = job
        self._queue.put((job, fn))
        return job

    def _worker(self) -> None:
        while True:
            job, fn = self._queue.get()
            if job.cancel:
                job.state = "cancelled"
                continue
            job.state, job.started = "running", time.time()
            try:
                fn(job)
                job.state = "cancelled" if job.cancel else "done"
            except Exception as e:  # keep the worker alive
                log.exception("Job %s failed", job.title)
                job.errors.append(f"{type(e).__name__}: {e}")
                job.state = "failed"
            finally:
                job.finished, job.current = time.time(), ""
                self.version += 1

    def active_jobs(self) -> list[dict[str, Any]]:
        recent = sorted(self.jobs.values(), key=lambda j: -j.id)[:10]
        return [j.view() for j in recent]

    # ------------------------------------------------------------- scanning
    @property
    def tagger(self) -> Tagger:
        if self._tagger is None:
            self._tagger = Tagger(
                self._embedder_factory(), self.cfg.max_auto_tags, self.cfg.auto_tag_min_score
            )
        return self._tagger

    def scan_source(self, source_id: int, full: bool = False) -> Job:
        src = self.db.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not src:
            raise KeyError(source_id)
        return self.submit(f"Scan {src['label']}", lambda job: self._scan(job, src, full))

    def _iter(self, src: dict[str, Any]):
        if src["kind"] == "local":
            return iter_local(src["location"])
        if src["kind"] == "gdrive":
            return iter_gdrive(self.cfg, src["location"])
        raise ValueError(f"Unknown source kind {src['kind']}")

    def _scan(self, job: Job, src: dict[str, Any], full: bool) -> None:
        job.current = "Listing files..."
        existing = {
            r["key"]: r
            for r in self.db.query(
                "SELECT id, key, size, mtime, checksum, indexed_at, embedding IS NOT NULL AS has_emb "
                "FROM photos WHERE source_id=?",
                (src["id"],),
            )
        }
        items = list(self._iter(src))
        seen = {it.key for it in items}
        todo: list[SourceItem] = []
        for it in items:
            row = existing.get(it.key)
            if (
                not full and row and row["has_emb"]
                and _token_of(row, src["kind"]) == it.change_token
            ):
                job.skipped += 1
            else:
                todo.append(it)
        job.total = len(todo)

        # Photos that disappeared from the source are dropped from the library.
        gone = [r["id"] for k, r in existing.items() if k not in seen]
        if gone:
            with self.db.tx() as c:
                c.executemany("DELETE FROM photos WHERE id=?", [(i,) for i in gone])
            for pid in gone:
                (self.cfg.thumbs_dir / f"{pid}.jpg").unlink(missing_ok=True)
                (self.cfg.previews_dir / f"{pid}.jpg").unlink(missing_ok=True)
            job.removed = len(gone)
            self.version += 1

        existing_ids = {r["id"] for r in existing.values()}
        batch: list[tuple[int, Any]] = []
        for it in todo:
            if job.cancel:
                break
            job.current = it.path
            try:
                pid, im = self._ingest(src, it)
                batch.append((pid, im))
                if pid not in existing_ids:
                    job.added += 1
            except Exception as e:
                job.errors.append(f"{it.path}: {type(e).__name__}: {e}")
                log.debug(traceback.format_exc())
                job.done += 1
            if len(batch) >= self.cfg.batch_size:
                self._embed_batch(batch)
                job.done += len(batch)
                batch = []
                self.version += 1
        if batch:
            self._embed_batch(batch)
            job.done += len(batch)
        self.db.execute("UPDATE sources SET last_scan=? WHERE id=?", (time.time(), src["id"]))

    def _ingest(self, src: dict[str, Any], it: SourceItem):
        data = it.read()
        checksum = it.checksum or hashlib.md5(data).hexdigest()
        raw = md.open_image(io.BytesIO(data))
        exif = md.extract_exif(raw)
        im = md.prepare(raw)
        for k, v in it.extra.items():  # source-provided metadata fills gaps
            if v is not None and not exif.get(k):
                exif[k] = v
        taken = exif["taken_at"] or (
            datetime.fromtimestamp(it.mtime).isoformat() if it.mtime else None
        )
        rec = {
            "source_id": src["id"],
            "kind": src["kind"],
            "key": it.key,
            "path": it.path,
            "filename": it.filename,
            "folder": it.folder,
            "size": it.size if it.size is not None else len(data),
            "mtime": it.mtime,
            "checksum": checksum,
            "dhash": md.dhash(im),
            "width": im.width,
            "height": im.height,
            "taken_at": taken,
            "camera": exif["camera"],
            "lat": exif["lat"],
            "lon": exif["lon"],
            "web_url": it.web_url,
            "indexed_at": time.time(),
        }
        pid = self.db.upsert_photo(rec)
        md.save_resized(im, self.cfg.thumbs_dir / f"{pid}.jpg", self.cfg.thumb_size)
        if src["kind"] != "local":  # no local original to show full-size
            md.save_resized(im, self.cfg.previews_dir / f"{pid}.jpg", self.cfg.preview_size, 88)
        small = im.copy()
        small.thumbnail((448, 448))
        return pid, small

    def _embed_batch(self, batch: list[tuple[int, Any]]) -> None:
        emb = self.tagger.embedder.encode_images([im for _, im in batch])
        tags = self.tagger.tags_for(emb)
        with self.db.tx() as c:
            c.executemany(
                "UPDATE photos SET embedding=? WHERE id=?",
                [(emb[i].astype(np.float32).tobytes(), pid) for i, (pid, _) in enumerate(batch)],
            )
        for (pid, _), t in zip(batch, tags):
            self.db.set_auto_tags(pid, t)

    # ------------------------------------------------------------- export
    def export(self, photo_ids: list[int], dest: str, pattern: str) -> Job:
        return self.submit(
            f"Export {len(photo_ids)} photos to {dest}",
            lambda job: self._export(job, photo_ids, Path(dest).expanduser(), pattern),
        )

    def _export(self, job: Job, photo_ids: list[int], dest: Path, pattern: str) -> None:
        """Copy photos into dest/<pattern>/. Never moves or deletes originals."""
        import shutil
        from .sources.gdrive import _download

        dest.mkdir(parents=True, exist_ok=True)
        job.total = len(photo_ids)
        for pid in photo_ids:
            if job.cancel:
                break
            p = self.db.photo(pid)
            job.done += 1
            if not p:
                continue
            sub = _expand_pattern(pattern, p)
            target_dir = dest / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            target = _unique(target_dir / p["filename"])
            job.current = str(target)
            try:
                if p["kind"] == "local":
                    shutil.copy2(p["key"], target)
                else:
                    target.write_bytes(_download(self.cfg, p["key"]))
                job.added += 1
            except Exception as e:
                job.errors.append(f"{p['path']}: {e}")


def _token_of(row: dict[str, Any], kind: str) -> str:
    if kind == "gdrive":
        return row["checksum"] or ""
    return f"{row['size']}:{int(row['mtime'] or 0)}"


def _safe(s: str) -> str:
    s = "".join(ch if ch.isalnum() or ch in " -_.,()&" else "_" for ch in s).strip()
    return s or "untitled"


def _expand_pattern(pattern: str, p: dict[str, Any]) -> Path:
    taken = p.get("taken_at") or ""
    year, month, day = taken[:4] or "unknown", taken[5:7] or "00", taken[8:10] or "00"
    first_tag = next((t["tag"] for t in p.get("tags", [])), "untagged")
    first_album = next((a["name"] for a in p.get("albums", [])), "no album")
    values = {
        "year": year, "month": month, "day": day,
        "camera": p.get("camera") or "unknown camera",
        "tag": first_tag, "album": first_album,
    }
    parts = []
    for part in (pattern or "").split("/"):
        if not part:
            continue
        try:
            parts.append(_safe(part.format(**values)))
        except (KeyError, IndexError, ValueError):
            parts.append(_safe(part))
    return Path(*parts) if parts else Path(".")


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf, n = path.stem, path.suffix, 1
    while (cand := path.with_name(f"{stem} ({n}){suf}")).exists():
        n += 1
    return cand
