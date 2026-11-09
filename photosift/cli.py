"""Command line: `photosift serve`, `photosift add <folder>`, `photosift search "..."`."""

from __future__ import annotations

import argparse
import logging
import time
import webbrowser

from .config import Config


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="photosift", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    a = sub.add_parser("add", help="add a local folder and index it now")
    a.add_argument("folder")

    q = sub.add_parser("search", help="search from the terminal")
    q.add_argument("query")
    q.add_argument("-n", type=int, default=20)

    sub.add_parser("gdrive-auth", help="connect your Google Drive account")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = Config()

    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        url = f"http://{args.host}:{args.port}"
        if not args.no_browser:
            import threading
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        print(f"PhotoSift running at {url}  (library: {cfg.data_dir})")
        uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning")

    elif args.cmd == "add":
        from .server import create_app

        app = create_app(cfg)
        from pathlib import Path

        folder = str(Path(args.folder).expanduser().resolve())
        sid = app.state.db.add_source("local", folder, Path(folder).name)
        job = app.state.indexer.scan_source(sid)
        while job.state in ("queued", "running"):
            print(f"\r{job.done}/{job.total} indexed, {job.skipped} unchanged", end="", flush=True)
            time.sleep(0.5)
        print(f"\n{job.state}: {job.added} new, {job.removed} removed, {len(job.errors)} errors")
        for e in job.errors[:10]:
            print("  !", e)

    elif args.cmd == "search":
        from .server import create_app

        app = create_app(cfg)
        res = app.state.engine.search(args.query, limit=args.n)
        print(f"{res['total']} matches ({res['took_ms']} ms)")
        for r in res["items"]:
            p = app.state.db.photo(r["id"])
            score = f"{r['score']:.3f}" if "score" in r else "     "
            print(f"{score}  {p['taken_at'] or '':19}  {p['path']}")

    elif args.cmd == "gdrive-auth":
        from .sources import gdrive

        gdrive.authorize(cfg)
        print("Google Drive connected.")


if __name__ == "__main__":
    main()
