"""Reading images: EXIF metadata, perceptual hashes, thumbnails."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from PIL import ExifTags, Image, ImageOps

try:  # HEIC/HEIF support (iPhone photos)
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

Image.MAX_IMAGE_PIXELS = 300_000_000  # allow big panoramas, still guard bombs

_GPS_TAG = 0x8825
_EXIF_IFD = 0x8769


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_image(src: Path | BinaryIO | bytes) -> Image.Image:
    if isinstance(src, bytes):
        src = io.BytesIO(src)
    im = Image.open(src)
    im.load()
    return im


def dhash(im: Image.Image, size: int = 8) -> str:
    """Difference hash: robust to resizing/re-encoding; used for near-duplicates."""
    g = im.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _to_degrees(value: Any) -> float:
    d, m, s = (float(x) for x in value)
    return d + m / 60.0 + s / 3600.0


def _parse_dt(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt).isoformat()
        except ValueError:
            continue
    return None


def extract_exif(im: Image.Image) -> dict[str, Any]:
    out: dict[str, Any] = {"taken_at": None, "camera": None, "lat": None, "lon": None}
    try:
        exif = im.getexif()
    except Exception:
        return out
    if not exif:
        return out
    base = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
    try:
        sub = {ExifTags.TAGS.get(k, k): v for k, v in exif.get_ifd(_EXIF_IFD).items()}
    except Exception:
        sub = {}
    out["taken_at"] = (
        _parse_dt(sub.get("DateTimeOriginal"))
        or _parse_dt(sub.get("DateTimeDigitized"))
        or _parse_dt(base.get("DateTime"))
    )
    make = str(base.get("Make") or "").strip().strip("\x00")
    model = str(base.get("Model") or "").strip().strip("\x00")
    if model and make and model.lower().startswith(make.lower()):
        make = ""
    cam = f"{make} {model}".strip()
    out["camera"] = cam or None
    try:
        gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in exif.get_ifd(_GPS_TAG).items()}
        if "GPSLatitude" in gps and "GPSLongitude" in gps:
            lat = _to_degrees(gps["GPSLatitude"])
            lon = _to_degrees(gps["GPSLongitude"])
            if str(gps.get("GPSLatitudeRef", "N")).upper().startswith("S"):
                lat = -lat
            if str(gps.get("GPSLongitudeRef", "E")).upper().startswith("W"):
                lon = -lon
            if abs(lat) <= 90 and abs(lon) <= 180 and (lat or lon):
                out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
    except Exception:
        pass
    return out


def prepare(im: Image.Image) -> Image.Image:
    """Apply EXIF rotation and convert to RGB (first frame for GIFs)."""
    try:
        im.seek(0)
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    return im


def save_resized(im: Image.Image, dest: Path, max_side: int, quality: int = 82) -> None:
    copy = im.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    copy.save(tmp, "JPEG", quality=quality, optimize=True)
    tmp.replace(dest)
