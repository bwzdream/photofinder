import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photosift.config import Config
from photosift.search import parse_query
from photosift.server import create_app


def make_photo(path: Path, color, size=(640, 480), date=None, camera=None, gps=None):
    im = Image.new("RGB", size, color)
    # add some texture so hashes differ between colours
    for x in range(0, size[0], 40):
        for y in range(0, size[1], 40):
            im.putpixel((x, y), (255 - color[0], 255 - color[1], 255 - color[2]))
    exif = Image.Exif()
    if camera:
        exif[0x010F], exif[0x0110] = camera
    if date:
        exif.get_ifd(0x8769)[0x9003] = date
    if gps:
        lat, lon = gps
        g = exif.get_ifd(0x8825)
        g[1], g[2] = ("N" if lat >= 0 else "S"), (abs(int(lat)), 0, 0)
        g[3], g[4] = ("E" if lon >= 0 else "W"), (abs(int(lon)), 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", exif=exif, quality=95)


@pytest.fixture()
def lib(tmp_path):
    photos = tmp_path / "photos"
    make_photo(photos / "trip" / "red.jpg", (220, 30, 30), date="2023:07:04 12:00:00",
               camera=("Canon", "Canon EOS R6"), gps=(48, 2))
    make_photo(photos / "trip" / "blue.jpg", (30, 30, 220), date="2024:01:15 09:30:00")
    make_photo(photos / "home" / "green.png", (30, 200, 30))
    # exact duplicate and a resized near-duplicate
    (photos / "home" / "red_copy.jpg").write_bytes((photos / "trip" / "red.jpg").read_bytes())
    make_photo(photos / "home" / "red_small.jpg", (220, 30, 30), size=(320, 240))
    (photos / "notes.txt").write_text("not an image")
    (photos / ".hidden").mkdir()
    make_photo(photos / ".hidden" / "skip.jpg", (0, 0, 0))

    cfg = Config(data_dir=tmp_path / "lib", embedder="fake")
    client = TestClient(create_app(cfg))
    return client, photos


def wait_jobs(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        jobs = client.get("/api/jobs").json()
        if all(j["state"] not in ("queued", "running") for j in jobs):
            return jobs
        time.sleep(0.1)
    raise TimeoutError(jobs)


def add_folder(client, photos):
    r = client.post("/api/sources", json={"kind": "local", "location": str(photos)})
    assert r.status_code == 200, r.text
    return wait_jobs(client)


def test_parse_query():
    pq = parse_query('dog beach tag:pet -tag:"golden hour" after:2023 album:"My Trip" rating>=3')
    assert pq.text == "dog beach"
    assert (False, "tag", "pet") in pq.filters
    assert (True, "tag", "golden hour") in pq.filters
    assert (False, "album", "My Trip") in pq.filters
    assert (False, "rating", "3") in pq.filters
    assert parse_query("similar:12").similar == 12
    assert parse_query("weird:thing").text == "weird:thing"


def test_index_and_metadata(lib):
    client, photos = lib
    jobs = add_folder(client, photos)
    assert jobs[0]["state"] == "done", jobs
    assert jobs[0]["errors"] == []
    stats = client.get("/api/stats").json()
    assert stats["photos"] == 5 and stats["embedded"] == 5

    items = client.get("/api/search").json()["items"]
    names = [i["filename"] for i in items]
    assert "skip.jpg" not in names
    assert names.index("blue.jpg") < names.index("red.jpg")  # newest first by EXIF date

    red = next(i for i in items if i["filename"] == "red.jpg")
    p = client.get(f"/api/photos/{red['id']}").json()
    assert p["camera"] == "Canon EOS R6"
    assert p["taken_at"] == "2023-07-04T12:00:00"
    assert p["lat"] == 48 and p["lon"] == 2
    assert p["width"] == 640
    assert client.get(f"/api/photos/{red['id']}/thumb").status_code == 200
    assert client.get(f"/api/photos/{red['id']}/full").status_code == 200
    assert red["id"] in [x["id"] for x in client.get("/api/places").json()]


def test_incremental_rescan(lib):
    client, photos = lib
    add_folder(client, photos)
    sid = client.get("/api/sources").json()[0]["id"]
    client.post(f"/api/sources/{sid}/scan")
    job = wait_jobs(client)[0]
    assert job["skipped"] == 5 and job["total"] == 0

    (photos / "home" / "green.png").unlink()
    make_photo(photos / "new.jpg", (200, 200, 0))
    client.post(f"/api/sources/{sid}/scan")
    job = wait_jobs(client)[0]
    assert job["removed"] == 1 and job["added"] == 1
    assert client.get("/api/stats").json()["photos"] == 5


def test_filters_tags_albums(lib):
    client, photos = lib
    add_folder(client, photos)
    s = lambda q: client.get("/api/search", params={"q": q}).json()

    assert s("camera:canon")["total"] >= 1
    assert s("year:2024")["total"] == 1
    assert s("after:2023-01 before:2024")["total"] >= 1
    assert s("folder:trip")["total"] == 2
    assert s("has:gps")["total"] >= 1
    assert s("is:duplicate")["total"] == 2

    ids = [i["id"] for i in s("folder:trip")["items"]]
    client.post("/api/photos/bulk", json={"ids": ids, "action": "add_tag", "value": "Vacation"})
    assert s("tag:vacation")["total"] == 2
    assert s("-tag:vacation")["total"] == 3
    assert s("vacation")["items"][0]["id"] in ids  # keyword boost for tags

    client.post("/api/photos/bulk", json={"ids": ids[:1], "action": "remove_tag", "value": "vacation"})
    assert s("tag:vacation")["total"] == 1

    client.post("/api/photos/bulk", json={"ids": ids, "action": "favorite"})
    assert s("is:favorite")["total"] == 2

    album = client.post("/api/albums", json={"name": "Trip 2023", "photo_ids": ids}).json()
    assert s('album:"Trip 2023"')["total"] == 2
    smart = client.post("/api/albums", json={"name": "Faves", "query": "is:favorite"}).json()
    albums = {a["name"]: a for a in client.get("/api/albums").json()}
    assert albums["Faves"]["photo_count"] == 2 and albums["Trip 2023"]["photo_count"] == 2
    assert s("album:Faves")["total"] == 2
    client.post("/api/photos/bulk", json={"ids": ids[:1], "action": "remove_from_album", "value": album["id"]})
    assert s('album:"Trip 2023"')["total"] == 1
    assert client.delete(f"/api/albums/{smart['id']}").status_code == 200

    pid = ids[0]
    client.patch(f"/api/photos/{pid}", json={"rating": 4, "note": "Eiffel tower picnic"})
    assert s("rating:4")["total"] == 1
    assert s("has:note")["total"] == 1
    assert s("eiffel")["items"][0]["id"] == pid


def test_semantic_and_similar(lib):
    client, photos = lib
    add_folder(client, photos)
    items = client.get("/api/search").json()["items"]
    red = next(i for i in items if i["filename"] == "red.jpg")
    res = client.get("/api/search", params={"q": f"similar:{red['id']}"}).json()
    assert red["id"] not in [i["id"] for i in res["items"]]
    assert res["items"][0]["filename"] in ("red_copy.jpg", "red_small.jpg")
    res = client.get("/api/search", params={"q": "anything at all"}).json()
    assert res["sort"] == "relevance" and all("score" in i for i in res["items"])


def test_duplicates(lib):
    client, photos = lib
    add_folder(client, photos)
    groups = client.get("/api/duplicates").json()
    assert len(groups) == 1
    names = sorted(p["filename"] for p in groups[0]["photos"])
    assert names == ["red.jpg", "red_copy.jpg", "red_small.jpg"]
    assert groups[0]["photos"][0]["width"] == 640  # best copy first


def test_export_and_download(lib, tmp_path):
    client, photos = lib
    add_folder(client, photos)
    dest = tmp_path / "organized"
    r = client.post("/api/export", json={"query": "folder:trip", "dest": str(dest), "pattern": "{year}/{month}"})
    assert r.status_code == 200
    wait_jobs(client)
    assert (dest / "2023" / "07" / "red.jpg").exists()
    assert (dest / "2024" / "01" / "blue.jpg").exists()
    assert (photos / "trip" / "red.jpg").exists()  # originals untouched

    ids = [i["id"] for i in client.get("/api/search").json()["items"]]
    z = client.post("/api/download", json={"ids": ids})
    assert z.status_code == 200 and z.content[:2] == b"PK"


def test_remove_source_and_bad_folder(lib):
    client, photos = lib
    assert client.post("/api/sources", json={"kind": "local", "location": "/nope/missing"}).status_code == 400
    add_folder(client, photos)
    sid = client.get("/api/sources").json()[0]["id"]
    assert client.delete(f"/api/sources/{sid}").json()["removed_photos"] == 5
    assert client.get("/api/stats").json()["photos"] == 0
    assert client.get("/").status_code == 200  # UI served
