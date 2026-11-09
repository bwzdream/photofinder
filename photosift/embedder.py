"""Image/text embedding with a local CLIP model (open_clip).

CLIP maps images and text into the same vector space, which gives us:
  * natural-language search ("kids playing in the snow")
  * zero-shot auto-tagging (compare a photo against a tag vocabulary)
  * "find similar photos" (image-to-image nearest neighbours)
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Protocol

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int
    name: str

    def encode_images(self, images: list[Image.Image]) -> np.ndarray: ...
    def encode_texts(self, texts: list[str]) -> np.ndarray: ...


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return (v / n).astype(np.float32)


class ClipEmbedder:
    def __init__(self, model_name: str, pretrained: str):
        import open_clip
        import torch

        self._torch = torch
        if torch.cuda.is_available():
            self.device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"
        log.info("Loading CLIP %s (%s) on %s ...", model_name, pretrained, self.device)
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained or None, device=self.device
        )
        model.eval()
        self.model = model
        self.preprocess = preprocess
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.name = f"{model_name}/{pretrained}"
        with torch.no_grad():
            self.dim = int(self.encode_texts(["probe"]).shape[1])
        self._lock = threading.Lock()  # one forward pass at a time

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        torch = self._torch
        batch = torch.stack([self.preprocess(im.convert("RGB")) for im in images]).to(self.device)
        with torch.no_grad():
            feats = self.model.encode_image(batch)
        return _normalize(feats.float().cpu().numpy())

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        torch = self._torch
        tokens = self.tokenizer(texts).to(self.device)
        with torch.no_grad():
            feats = self.model.encode_text(tokens)
        return _normalize(feats.float().cpu().numpy())


class FakeEmbedder:
    """Deterministic, model-free stand-in used by tests and for trying the UI
    without downloading a model. Images embed by colour layout; texts by word hashes.
    Search results are NOT meaningful with this embedder."""

    dim = 64
    name = "fake"

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        out = []
        for im in images:
            small = np.asarray(im.convert("RGB").resize((4, 4)), dtype=np.float32) / 255.0
            v = np.concatenate([small.reshape(-1), np.zeros(self.dim - 48, np.float32)])
            out.append(v - 0.5)
        return _normalize(np.stack(out))

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            v = np.zeros(self.dim, np.float32)
            for w in t.lower().split():
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
            out.append(v)
        return _normalize(np.stack(out))


_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def get_embedder(kind: str, model_name: str, pretrained: str) -> Embedder:
    """Lazily create a process-wide embedder (model loading is slow)."""
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = FakeEmbedder() if kind == "fake" else ClipEmbedder(model_name, pretrained)
        return _embedder
