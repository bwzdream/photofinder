"""Google Drive source (read-only).

Setup (one time):
  1. In Google Cloud Console create a project, enable the "Google Drive API".
  2. Create an OAuth client ID of type "Desktop app" and download the JSON.
  3. Save it as ~/.photosift/client_secret.json (or set PHOTOSIFT_GDRIVE_CLIENT_SECRET).
  4. Click "Connect Google Drive" in the app (or run `photosift gdrive-auth`).

Only the drive.readonly scope is requested: PhotoSift never modifies your Drive.
Photos are downloaded one at a time to build a thumbnail, a preview and the AI
index; the original is not kept on disk.
"""

from __future__ import annotations

import io
import logging
import threading
from datetime import datetime
from typing import Any, Iterator

from ..config import Config
from . import SourceItem

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS = (
    "nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum, parents, "
    "webViewLink, imageMediaMetadata(time, cameraMake, cameraModel, location, width, height))"
)

_local = threading.local()


class GDriveNotConfigured(RuntimeError):
    pass


def _imports():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as e:  # pragma: no cover
        raise GDriveNotConfigured(
            "Google Drive support is not installed. Run: pip install 'photosift[gdrive]'"
        ) from e
    return Request, Credentials, InstalledAppFlow, build, MediaIoBaseDownload


def status(cfg: Config) -> dict[str, Any]:
    try:
        _imports()
        installed = True
    except GDriveNotConfigured:
        installed = False
    return {
        "installed": installed,
        "client_secret": cfg.gdrive_client_secret.exists(),
        "client_secret_path": str(cfg.gdrive_client_secret),
        "connected": cfg.gdrive_token.exists(),
    }


def load_credentials(cfg: Config):
    Request, Credentials, *_ = _imports()
    if not cfg.gdrive_token.exists():
        raise GDriveNotConfigured("Google Drive is not connected yet.")
    creds = Credentials.from_authorized_user_file(str(cfg.gdrive_token), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        cfg.gdrive_token.write_text(creds.to_json())
    return creds


def authorize(cfg: Config, open_browser: bool = True) -> None:
    """Run the OAuth consent flow in the user's browser and store the token."""
    _, _, InstalledAppFlow, *_ = _imports()
    if not cfg.gdrive_client_secret.exists():
        raise GDriveNotConfigured(
            f"Missing OAuth client file. Download a 'Desktop app' OAuth client JSON from "
            f"Google Cloud Console and save it as {cfg.gdrive_client_secret}"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.gdrive_client_secret), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.gdrive_token.write_text(creds.to_json())


def disconnect(cfg: Config) -> None:
    cfg.gdrive_token.unlink(missing_ok=True)


def _service(cfg: Config):
    # googleapiclient service objects are not thread-safe: one per thread.
    svc = getattr(_local, "svc", None)
    if svc is None:
        *_, build, _ = _imports()
        svc = build("drive", "v3", credentials=load_credentials(cfg), cache_discovery=False)
        _local.svc = svc
    return svc


def _list(svc, q: str) -> Iterator[dict[str, Any]]:
    token = None
    while True:
        resp = svc.files().list(
            q=q, fields=_FIELDS, pageSize=1000, pageToken=token,
            spaces="drive", includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute()
        yield from resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            break


def _download(cfg: Config, file_id: str) -> bytes:
    *_, MediaIoBaseDownload = _imports()
    svc = _service(cfg)
    buf = io.BytesIO()
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req, chunksize=8 << 20)
    done = False
    while not done:
        _, done = dl.next_chunk(num_retries=3)
    return buf.getvalue()


def _parse_time(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).isoformat()
        except ValueError:
            pass
    return None


def iter_gdrive(cfg: Config, folder_id: str = "root") -> Iterator[SourceItem]:
    svc = _service(cfg)
    # Build a folder-id -> (name, parent) map so we can show readable paths.
    folders: dict[str, tuple[str, str | None]] = {}
    for f in _list(svc, f"mimeType='{FOLDER_MIME}' and trashed=false"):
        folders[f["id"]] = (f["name"], (f.get("parents") or [None])[0])

    def folder_path(fid: str | None) -> str:
        parts, seen = [], set()
        while fid and fid in folders and fid not in seen:
            seen.add(fid)
            name, fid = folders[fid]
            parts.append(name)
        return "/".join(["My Drive", *reversed(parts)])

    if folder_id == "root":
        allowed = None
    else:  # restrict to this folder and everything below it
        children: dict[str, list[str]] = {}
        for fid, (_, parent) in folders.items():
            children.setdefault(parent or "", []).append(fid)
        allowed, stack = set(), [folder_id]
        while stack:
            fid = stack.pop()
            allowed.add(fid)
            stack.extend(children.get(fid, []))

    for f in _list(svc, "mimeType contains 'image/' and trashed=false"):
        parent = (f.get("parents") or [None])[0]
        if allowed is not None and parent not in allowed:
            continue
        meta = f.get("imageMediaMetadata") or {}
        loc = meta.get("location") or {}
        camera = " ".join(x for x in (meta.get("cameraMake"), meta.get("cameraModel")) if x) or None
        fpath = folder_path(parent)
        mtime = None
        try:
            mtime = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        fid = f["id"]
        yield SourceItem(
            key=fid,
            path=f"{fpath}/{f['name']}",
            filename=f["name"],
            folder=fpath,
            size=int(f["size"]) if f.get("size") else None,
            mtime=mtime,
            change_token=f.get("md5Checksum") or f.get("modifiedTime", ""),
            read=lambda fid=fid: _download(cfg, fid),
            checksum=f.get("md5Checksum"),
            web_url=f.get("webViewLink"),
            extra={
                "taken_at": _parse_time(meta.get("time")),
                "camera": camera,
                "lat": loc.get("latitude"),
                "lon": loc.get("longitude"),
            },
        )
