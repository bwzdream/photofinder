"""Local folders (internal disks, external/USB drives, mounted network shares)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from ..config import IMAGE_EXTENSIONS
from . import SourceItem

_SKIP_DIRS = {"node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"}


def _reader(p: Path):
    return lambda: p.read_bytes()


def iter_local(root: str) -> Iterator[SourceItem]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"Folder not found: {base}")
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        # Skip hidden folders (.git, .thumbnails, ...) and system junk.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < 1024:  # icons / broken files
                continue
            yield SourceItem(
                key=str(p),
                path=str(p),
                filename=name,
                folder=str(p.parent),
                size=st.st_size,
                mtime=st.st_mtime,
                change_token=f"{st.st_size}:{int(st.st_mtime)}",
                read=_reader(p),
            )
