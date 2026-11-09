"""Runtime configuration, driven by environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif",
}


@dataclass
class Config:
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PHOTOSIFT_HOME", Path.home() / ".photosift")
        ).expanduser()
    )
    # "clip" (real model) or "fake" (deterministic stand-in, for tests/dev without a model)
    embedder: str = field(default_factory=lambda: os.environ.get("PHOTOSIFT_EMBEDDER", "clip"))
    clip_model: str = field(default_factory=lambda: os.environ.get("PHOTOSIFT_CLIP_MODEL", "ViT-B-32"))
    clip_pretrained: str = field(
        default_factory=lambda: os.environ.get("PHOTOSIFT_CLIP_PRETRAINED", "laion2b_s34b_b79k")
    )
    thumb_size: int = 400
    preview_size: int = 1600
    batch_size: int = 16
    # How many auto-tags to keep per photo, and the minimum score to keep one.
    max_auto_tags: int = 6
    auto_tag_min_score: float = 0.08

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def gdrive_token(self) -> Path:
        return self.data_dir / "gdrive_token.json"

    @property
    def gdrive_client_secret(self) -> Path:
        return Path(
            os.environ.get("PHOTOSIFT_GDRIVE_CLIENT_SECRET", self.data_dir / "client_secret.json")
        ).expanduser()

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.thumbs_dir, self.previews_dir):
            d.mkdir(parents=True, exist_ok=True)
