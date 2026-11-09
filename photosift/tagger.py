"""Zero-shot auto-tagging: score each photo against a vocabulary of labels.

Labels are split into facets (scene, subject, event, ...). Within each facet we
softmax over the labels, so every facet contributes its best few matches. That
works far better with CLIP than a single softmax over every label, which tends
to put all the probability on one tag.
"""

from __future__ import annotations

import numpy as np

from .embedder import Embedder

# facet -> (max tags kept from this facet, labels). A label of None marks the
# "nothing in particular" class: if it wins, the facet adds no tag.
VOCABULARY: dict[str, tuple[int, list[str | None]]] = {
    "kind": (1, [
        None, "screenshot", "document", "receipt", "whiteboard", "drawing", "meme",
        "map", "diagram", "handwritten note", "x-ray",
    ]),
    "scene": (2, [
        "beach", "ocean", "lake", "river", "waterfall", "mountain", "forest", "desert",
        "snow", "field", "garden", "park", "farm", "city street", "skyline", "bridge",
        "road", "countryside", "island", "cave", "kitchen", "living room", "bedroom",
        "bathroom", "office", "classroom", "restaurant", "cafe", "bar", "shop",
        "supermarket", "museum", "church", "temple", "castle", "stadium", "gym",
        "swimming pool", "playground", "airport", "train station", "hospital",
        "concert hall", "theater", "library", "backyard", "car interior", "night sky",
    ]),
    "subject": (3, [
        "person", "group of people", "selfie", "baby", "child", "elderly person",
        "dog", "cat", "bird", "horse", "cow", "fish", "insect", "butterfly", "wildlife",
        "flower", "tree", "plant", "food", "dessert", "cake", "drink", "coffee",
        "fruit", "car", "bicycle", "motorcycle", "boat", "airplane", "train", "bus",
        "building", "house", "architecture", "monument", "statue", "painting",
        "toy", "computer", "phone", "book", "furniture", "clothing", "shoes",
        "jewelry", "sky", "clouds", "sunset", "fireworks", "christmas tree",
        "sign", "text", "artwork", "vehicle",
    ]),
    "event": (1, [
        None, "birthday party", "wedding", "graduation", "christmas", "halloween",
        "holiday trip", "sports", "hiking", "camping", "swimming", "skiing",
        "concert", "meal with friends", "picnic", "family gathering", "baby shower",
        "parade", "festival", "conference", "road trip",
    ]),
    "time": (1, [None, "night", "sunset", "sunrise", "golden hour", "foggy", "rainy"]),
}

_TEMPLATES = ["a photo of {}.", "a picture of a {}.", "an image showing {}."]
_NONE_PROMPTS = ["a photo.", "an ordinary everyday photo.", "a picture."]


class Tagger:
    def __init__(self, embedder: Embedder, max_tags: int = 6, min_score: float = 0.08):
        self.embedder = embedder
        self.max_tags = max_tags
        self.min_score = min_score
        self._facets: list[tuple[int, list[str | None], np.ndarray]] = []
        for _, (keep, labels) in VOCABULARY.items():
            vecs = []
            for label in labels:
                prompts = _NONE_PROMPTS if label is None else [t.format(label) for t in _TEMPLATES]
                e = embedder.encode_texts(prompts).mean(axis=0)
                vecs.append(e / (np.linalg.norm(e) or 1.0))
            self._facets.append((keep, labels, np.stack(vecs).astype(np.float32)))

    def tags_for(self, image_embeddings: np.ndarray) -> list[list[tuple[str, float]]]:
        """Return [(tag, score), ...] for each embedding row."""
        results: list[list[tuple[str, float]]] = [[] for _ in range(len(image_embeddings))]
        for keep, labels, text in self._facets:
            logits = 100.0 * image_embeddings @ text.T  # CLIP's usual temperature
            logits -= logits.max(axis=1, keepdims=True)
            probs = np.exp(logits)
            probs /= probs.sum(axis=1, keepdims=True)
            for i, row in enumerate(probs):
                for j in np.argsort(-row)[:keep]:
                    label = labels[j]
                    if label is None:
                        break  # "nothing special" outranks the rest of this facet
                    if row[j] >= self.min_score:
                        results[i].append((label, float(row[j])))
        return [sorted(r, key=lambda x: -x[1])[: self.max_tags] for r in results]
