# PhotoSift

[![tests](https://github.com/bwzdream/photofinder/actions/workflows/tests.yml/badge.svg)](https://github.com/bwzdream/photofinder/actions/workflows/tests.yml)

A private photo organizer that runs on your own computer. It reads the photos on
your drives and in Google Drive. A local AI model (CLIP) works out what is in each
one, so you can find photos by describing them, like *"kids playing in the snow"*.
You can also organize them with albums, tags, favorites and ratings.

The AI runs offline on your machine. Photos are never uploaded anywhere.

## Features

| | |
|---|---|
| **Sources** | Local folders, external/USB drives, mounted network shares, and Google Drive (read-only) |
| **AI understanding** | Search by plain-language description, automatic tags (scene, subject, event, time of day, screenshots/documents), "find similar" |
| **Metadata** | Date taken, camera, GPS location, size (from EXIF; falls back to file time) |
| **Search** | Free text plus filters: `tag:` `album:` `after:` `before:` `year:` `date:` `camera:` `folder:` `file:` `source:` `is:favorite` `rating:` `has:gps` `has:note` `is:duplicate` `similar:` and `-` to exclude |
| **Organize** | Albums, smart albums (saved searches that update themselves), your own tags, favorites, 1–5 star ratings, notes |
| **Browse** | Timeline by month/year, Places map, Folders, Duplicates (exact and look-alike) |
| **Export** | Copy a selection, album or search into a folder, sorted into `Year/Month`, camera, tag or album subfolders. Or download a zip |
| **Safe** | Never moves, edits or deletes your original files |

Formats: JPEG, PNG, WebP, GIF, BMP, TIFF, HEIC/HEIF (iPhone).

## Install

Requires Python 3.10+.

Download the code with the green **Code → Download ZIP** button on GitHub and unzip it,
or clone it:

```bash
git clone https://github.com/bwzdream/photofinder.git
cd photofinder
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[gdrive]"
```

On a machine without an NVIDIA GPU you can install the smaller CPU-only PyTorch
first:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

## Run

```bash
photosift serve
```

This opens http://127.0.0.1:8765. Click **+** next to *Sources* and enter a folder
path, such as `~/Pictures`, `D:\Photos` or `/Volumes/MyDrive/Photos`. Indexing runs
in the background and photos appear as they are processed. On first use the CLIP
model (~600 MB) is downloaded once and cached.

Rough speed: 5–15 photos per second on a modern CPU, and much faster on a GPU or
Apple Silicon (used automatically). Rescans only process new or changed files.

From the terminal:

```bash
photosift add ~/Pictures          # index a folder now
photosift search "birthday cake"  # search without the UI
```

## Google Drive

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and
   enable the **Google Drive API**.
2. Under *APIs & Services → Credentials*, create an **OAuth client ID** of type
   **Desktop app**, then download the JSON.
   While the consent screen is in "Testing" mode, add your Google account as a test user.
3. Save the JSON as `~/.photosift/client_secret.json`.
4. In the app, choose **+ → Google Drive → Connect** and approve read-only access.
   Then index all of Drive (`root`) or paste a folder link.

PhotoSift downloads each Drive photo once to make a thumbnail, a 1600px preview and
the AI index. It does not keep the original. "Copy to folder" downloads full-quality
originals.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `PHOTOSIFT_HOME` | `~/.photosift` | Library database, thumbnails, Drive token |
| `PHOTOSIFT_CLIP_MODEL` | `ViT-B-32` | Any [open_clip](https://github.com/mlfoundations/open_clip) model, e.g. `ViT-L-14` for better accuracy (slower) |
| `PHOTOSIFT_CLIP_PRETRAINED` | `laion2b_s34b_b79k` | Weights for that model |
| `PHOTOSIFT_EMBEDDER` | `clip` | `fake` = no model, for development/tests only |
| `PHOTOSIFT_GDRIVE_CLIENT_SECRET` | `$PHOTOSIFT_HOME/client_secret.json` | |

If you change the model, use **Re-index everything** on each source (double-click
the source in the sidebar).

## How it works

```
sources/local.py, sources/gdrive.py   list image files + read bytes
            │
indexer.py  ── metadata.py: EXIF, rotation, dHash, thumbnails
            ── embedder.py: CLIP image vector (stored in SQLite)
            ── tagger.py:   zero-shot tags from a label vocabulary, per facet
            │
search.py   query language → SQL filters → cosine similarity ranking
            (+ exact word matches in your tags/notes/file names rank first)
server.py   FastAPI JSON API + static single-page UI (static/)
```

Your edits are kept across rescans. Manual tags, removed AI tags, albums, favorites,
ratings and notes all survive re-indexing.

## Development

```bash
pip install -e ".[gdrive,dev]"
pytest
```

The tests use the `fake` embedder, so they don't need the model download.

## Ideas for next steps

- Face detection and grouping ("People"), e.g. with `insightface`
- Text-in-image search (OCR) for screenshots and documents
- Reverse-geocoding GPS into place names ("Paris")
- Watch folders for new files automatically
- Video support (index keyframes)
