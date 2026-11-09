"""FastAPI web server: JSON API + the single-page UI."""

from __future__ import annotations

import io
import mimetypes
import threading
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import metadata as md
from .config import Config
from .db import Database
from .embedder import get_embedder
from .indexer import Indexer
from .search import SearchEngine
from .sources import gdrive

STATIC = Path(__file__).parent / "static"
BROWSER_FORMATS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


class SourceIn(BaseModel):
    kind: str = "local"
    location: str
    label: str | None = None


class PhotoPatch(BaseModel):
    favorite: bool | None = None
    rating: int | None = None
    note: str | None = None


class BulkIn(BaseModel):
    ids: list[int]
    action: str  # favorite | unfavorite | rate | add_tag | remove_tag | add_to_album | remove_from_album
    value: Any = None


class AlbumIn(BaseModel):
    name: str
    query: str | None = None
    photo_ids: list[int] = []


class AlbumPatch(BaseModel):
    name: str | None = None
    query: str | None = None
    cover_id: int | None = None


class ExportIn(BaseModel):
    ids: list[int] = []
    query: str | None = None
    dest: str
    pattern: str = "{year}/{month}"


class IdsIn(BaseModel):
    ids: list[int]


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    db = Database(cfg.db_path)

    def embedder():
        return get_embedder(cfg.embedder, cfg.clip_model, cfg.clip_pretrained)

    indexer = Indexer(cfg, db, embedder)
    engine = SearchEngine(db, embedder, lambda: indexer.version)
    gdrive_auth: dict[str, Any] = {"running": False, "error": None}

    app = FastAPI(title="PhotoSift")
    app.state.cfg, app.state.db, app.state.indexer, app.state.engine = cfg, db, indexer, engine

    def bump() -> None:
        indexer.version += 1

    def photo_or_404(pid: int) -> dict[str, Any]:
        p = db.photo(pid)
        if not p:
            raise HTTPException(404, "Photo not found")
        return p

    # ------------------------------------------------------------------ status
    @app.get("/api/stats")
    def stats():
        return {
            **db.stats(),
            "model": cfg.clip_model if cfg.embedder != "fake" else "fake (testing only)",
            "jobs": indexer.active_jobs(),
        }

    @app.get("/api/jobs")
    def jobs():
        return indexer.active_jobs()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        job = indexer.jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        job.cancel = True
        return job.view()

    # ------------------------------------------------------------------ sources
    @app.get("/api/sources")
    def list_sources():
        return db.sources()

    @app.post("/api/sources")
    def add_source(s: SourceIn):
        if s.kind == "local":
            p = Path(s.location).expanduser()
            if not p.is_dir():
                raise HTTPException(400, f"Folder not found: {p}")
            location = str(p.resolve())
            label = s.label or p.resolve().name or location
        elif s.kind == "gdrive":
            if not gdrive.status(cfg)["connected"]:
                raise HTTPException(400, "Connect Google Drive first.")
            location = s.location.strip() or "root"
            if "folders/" in location:  # accept a pasted Drive folder URL
                location = location.split("folders/")[1].split("?")[0].split("/")[0]
            label = s.label or ("Google Drive" if location == "root" else f"Drive folder {location[:8]}")
        else:
            raise HTTPException(400, "kind must be 'local' or 'gdrive'")
        sid = db.add_source(s.kind, location, label)
        job = indexer.scan_source(sid)
        return {"id": sid, "job": job.view()}

    @app.delete("/api/sources/{sid}")
    def remove_source(sid: int):
        ids = [r["id"] for r in db.query("SELECT id FROM photos WHERE source_id=?", (sid,))]
        db.execute("DELETE FROM sources WHERE id=?", (sid,))
        for pid in ids:
            (cfg.thumbs_dir / f"{pid}.jpg").unlink(missing_ok=True)
            (cfg.previews_dir / f"{pid}.jpg").unlink(missing_ok=True)
        bump()
        return {"removed_photos": len(ids)}

    @app.post("/api/sources/{sid}/scan")
    def scan_source(sid: int, full: bool = False):
        try:
            return indexer.scan_source(sid, full).view()
        except KeyError:
            raise HTTPException(404)

    @app.post("/api/scan-all")
    def scan_all():
        return [indexer.scan_source(s["id"]).view() for s in db.sources()]

    # ------------------------------------------------------------------ google drive
    @app.get("/api/gdrive")
    def gdrive_status():
        return {**gdrive.status(cfg), "auth_running": gdrive_auth["running"],
                "auth_error": gdrive_auth["error"]}

    @app.post("/api/gdrive/connect")
    def gdrive_connect():
        st = gdrive.status(cfg)
        if not st["installed"]:
            raise HTTPException(400, "Install Drive support: pip install 'photosift[gdrive]'")
        if not st["client_secret"]:
            raise HTTPException(400, f"Save your OAuth client JSON as {st['client_secret_path']}")
        if gdrive_auth["running"]:
            return {"started": False}

        def run():
            gdrive_auth.update(running=True, error=None)
            try:
                gdrive.authorize(cfg)
            except Exception as e:
                gdrive_auth["error"] = str(e)
            finally:
                gdrive_auth["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @app.post("/api/gdrive/disconnect")
    def gdrive_disconnect():
        gdrive.disconnect(cfg)
        return gdrive.status(cfg)

    # ------------------------------------------------------------------ search & browse
    @app.get("/api/search")
    def search(q: str = "", sort: str = "auto", offset: int = 0, limit: int = 200,
               strictness: float = 0.07):
        return engine.search(q, sort, offset, min(limit, 1000), strictness)

    @app.get("/api/tags")
    def tags():
        return db.tag_counts()

    @app.get("/api/timeline")
    def timeline():
        return db.query(
            "SELECT substr(taken_at, 1, 7) AS month, COUNT(*) AS n FROM photos "
            "WHERE taken_at IS NOT NULL GROUP BY month ORDER BY month DESC"
        )

    @app.get("/api/folders")
    def folders():
        return db.query(
            "SELECT folder, kind, COUNT(*) AS n FROM photos GROUP BY folder, kind ORDER BY folder"
        )

    @app.get("/api/cameras")
    def cameras():
        return db.query(
            "SELECT camera, COUNT(*) AS n FROM photos WHERE camera IS NOT NULL "
            "GROUP BY camera ORDER BY n DESC"
        )

    @app.get("/api/places")
    def places():
        return db.query(
            "SELECT id, lat, lon, filename, taken_at FROM photos WHERE lat IS NOT NULL"
        )

    @app.get("/api/duplicates")
    def duplicates():
        groups = engine.duplicates()
        out = []
        for g in groups:
            rows = db.query(
                f"SELECT id, filename, path, size, width, height, taken_at, kind, checksum "
                f"FROM photos WHERE id IN ({','.join('?' * len(g))})", g
            )
            rows.sort(key=lambda r: (-(r["width"] or 0) * (r["height"] or 0), -(r["size"] or 0)))
            exact = len({r["checksum"] for r in rows}) == 1
            out.append({"exact": exact, "photos": rows})
        return out

    # ------------------------------------------------------------------ photos
    @app.get("/api/photos/{pid}")
    def get_photo(pid: int):
        return photo_or_404(pid)

    @app.patch("/api/photos/{pid}")
    def patch_photo(pid: int, body: PhotoPatch):
        photo_or_404(pid)
        if body.favorite is not None:
            db.execute("UPDATE photos SET favorite=? WHERE id=?", (int(body.favorite), pid))
        if body.rating is not None:
            db.execute("UPDATE photos SET rating=? WHERE id=?", (max(0, min(5, body.rating)), pid))
        if body.note is not None:
            db.execute("UPDATE photos SET note=? WHERE id=?", (body.note[:5000], pid))
        return db.photo(pid)

    @app.post("/api/photos/bulk")
    def bulk(body: BulkIn):
        ids, v = body.ids, body.value
        if not ids:
            return {"ok": True}
        marks = ",".join("?" * len(ids))
        a = body.action
        if a in ("favorite", "unfavorite"):
            db.execute(f"UPDATE photos SET favorite=? WHERE id IN ({marks})", [int(a == "favorite"), *ids])
        elif a == "rate":
            db.execute(f"UPDATE photos SET rating=? WHERE id IN ({marks})", [max(0, min(5, int(v))), *ids])
        elif a == "add_tag":
            tag = str(v or "").strip().lower()
            if not tag:
                raise HTTPException(400, "Tag is empty")
            db.add_tag(ids, tag)
        elif a == "remove_tag":
            db.remove_tag(ids, str(v).strip().lower())
        elif a == "add_to_album":
            db.add_to_album(int(v), ids)
        elif a == "remove_from_album":
            db.remove_from_album(int(v), ids)
        else:
            raise HTTPException(400, f"Unknown action {a}")
        return {"ok": True, "count": len(ids)}

    @app.get("/api/photos/{pid}/thumb")
    def thumb(pid: int):
        f = cfg.thumbs_dir / f"{pid}.jpg"
        if not f.exists():
            raise HTTPException(404)
        return FileResponse(f, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/photos/{pid}/full")
    def full(pid: int):
        p = photo_or_404(pid)
        if p["kind"] == "local":
            path = Path(p["key"])
            if not path.exists():
                raise HTTPException(410, "File no longer exists; rescan the source.")
            if path.suffix.lower() in BROWSER_FORMATS:
                return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])
            # HEIC/TIFF etc.: convert on the fly for the browser
            im = md.prepare(md.open_image(path))
            im.thumbnail((cfg.preview_size * 2, cfg.preview_size * 2))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            return Response(buf.getvalue(), media_type="image/jpeg")
        f = cfg.previews_dir / f"{pid}.jpg"
        if not f.exists():
            raise HTTPException(404)
        return FileResponse(f, media_type="image/jpeg")

    # ------------------------------------------------------------------ albums
    @app.get("/api/albums")
    def albums():
        out = db.albums()
        for a in out:
            if a["query"]:
                res = engine.search(a["query"], limit=1)
                a["photo_count"] = res["total"]
                a["cover"] = a["cover_id"] or (res["items"][0]["id"] if res["items"] else None)
        return out

    @app.post("/api/albums")
    def create_album(body: AlbumIn):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Album name is empty")
        if db.one("SELECT id FROM albums WHERE name=? COLLATE NOCASE", (name,)):
            raise HTTPException(409, "An album with that name already exists")
        aid = db.create_album(name, (body.query or "").strip() or None)
        if body.photo_ids:
            db.add_to_album(aid, body.photo_ids)
        return db.one("SELECT * FROM albums WHERE id=?", (aid,))

    @app.patch("/api/albums/{aid}")
    def patch_album(aid: int, body: AlbumPatch):
        if body.name:
            db.execute("UPDATE albums SET name=? WHERE id=?", (body.name.strip(), aid))
        if body.query is not None:
            db.execute("UPDATE albums SET query=? WHERE id=?", (body.query.strip() or None, aid))
        if body.cover_id is not None:
            db.execute("UPDATE albums SET cover_id=? WHERE id=?", (body.cover_id, aid))
        return db.one("SELECT * FROM albums WHERE id=?", (aid,))

    @app.delete("/api/albums/{aid}")
    def delete_album(aid: int):
        db.execute("DELETE FROM albums WHERE id=?", (aid,))
        return {"ok": True}

    # ------------------------------------------------------------------ export
    @app.post("/api/export")
    def export(body: ExportIn):
        ids = body.ids or [r["id"] for r in engine.search(body.query or "", limit=1_000_000)["items"]]
        if not ids:
            raise HTTPException(400, "Nothing to export")
        return indexer.export(ids, body.dest, body.pattern).view()

    @app.post("/api/download")
    def download(body: IdsIn):
        """Zip of the selected photos (originals for local files, previews for Drive)."""
        ids = body.ids[:2000]

        def gen():
            buf = io.BytesIO()
            names: set[str] = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                for pid in ids:
                    p = db.photo(pid)
                    if not p:
                        continue
                    name = p["filename"]
                    n = 1
                    while name in names:
                        stem, dot, ext = p["filename"].rpartition(".")
                        name = f"{stem} ({n}).{ext}" if dot else f"{p['filename']} ({n})"
                        n += 1
                    names.add(name)
                    src = Path(p["key"]) if p["kind"] == "local" else cfg.previews_dir / f"{pid}.jpg"
                    if src.exists():
                        z.write(src, name)
            yield buf.getvalue()

        return StreamingResponse(gen(), media_type="application/zip",
                                 headers={"Content-Disposition": 'attachment; filename="photos.zip"'})

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
