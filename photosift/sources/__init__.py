"""Photo sources: where images come from (local folders, Google Drive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SourceItem:
    key: str                  # unique within its kind: absolute path or Drive file id
    path: str                 # human-readable path
    filename: str
    folder: str
    size: int | None
    mtime: float | None
    change_token: str         # changes whenever the file content changes
    read: Callable[[], bytes]
    checksum: str | None = None   # md5 if the source already knows it
    web_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # metadata the source provides
