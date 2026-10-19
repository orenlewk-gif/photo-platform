"""Thumbnails: made once per photo in every version, in the background and on demand, and
served by redirecting to the stored file. No-watermark versions are for signed-in staff only."""
import json
import time
from io import BytesIO

import boto3
import pytest
from PIL import Image

S3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "test-bucket"


def _jpeg(w=4000, h=3000, color=(30, 120, 200), orientation=None) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", quality=90, exif=exif.tobytes())
    else:
        img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _put(key, body):
    S3.put_object(Bucket=BUCKET, Key=key, Body=body)


def _get(key):
    try:
        return S3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except S3.exceptions.NoSuchKey:
        return None


def _size(jpeg: bytes):
    return Image.open(BytesIO(jpeg)).size


def _stored_key(location: str) -> str:
    """The storage key a thumbnail redirect points at."""
    return location.split("/test-bucket/", 1)[1].split("?", 1)[0]


@pytest.fixture()
def pos_client(main_module):
    """A POS that has entered a photographer PIN (which sets the staff-thumbnail cookie)."""
    from fastapi.testclient import TestClient
    _put("meta/photographers.json", json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    c = TestClient(main_module.app)
    assert c.post("/api/photographer/auth", json={"pin": "4821"}).status_code == 200
    return c


def _wait_job(admin_client, job_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        d = admin_client.get(f"/api/admin/embed-status?job_id={job_id}").json()
        if d["finished"]:
            return d
        time.sleep(0.1)
    raise AssertionError("thumbnail job didn't finish")


def _wait_for(key, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if _get(key):
            return True
        time.sleep(0.1)
    return False


def test_one_download_makes_every_version(main_module):
    key = "images/2026-10-01/mountain-biking/am/A.jpg"
    _put(key, _jpeg())
    out = main_module._make_thumbs(key, list(main_module._THUMB_VARIANTS))
    assert _size(out["grid"]) == (700, 525) and _size(out["pos"]) == (700, 525)
    assert _size(out["public"]) == (700, 525) and _size(out["admin"]) == (450, 338)
    assert Image.open(BytesIO(out["grid"])).format == "WEBP"
    # the gallery copy is watermarked, the POS copy isn't
    grid, pos = Image.open(BytesIO(out["grid"])).convert("RGB"), Image.open(BytesIO(out["pos"])).convert("RGB")
    assert grid.getpixel((350, 262)) != pos.getpixel((350, 262))
    assert _get("thumbs/grid/2026-10-01/mountain-biking/am/A.jpg.webp")
    assert S3.head_object(Bucket=BUCKET, Key="thumbs/grid/2026-10-01/mountain-biking/am/A.jpg.webp")["ContentType"] == "image/webp"
    assert _get("thumbs/v2/2026-10-01/mountain-biking/am/A.jpg")
    assert _get("thumbs/nowm/2026-10-01/mountain-biking/am/A.jpg")
    assert _get("admin_thumbs/2026-10-01/mountain-biking/am/A.jpg")


def test_rotated_photo_comes_out_upright(main_module):
    key = "images/2026-10-01/mountain-biking/am/ROT.jpg"
    _put(key, _jpeg(4000, 3000, orientation=6))               # camera held sideways
    out = main_module._make_thumbs(key, ["pos"])
    assert _size(out["pos"]) == (525, 700)


def test_pos_thumbnail_on_demand_then_from_storage_and_others_made(pos_client, main_module):
    key = "images/2026-10-02/mountain-biking/am/B.jpg"
    _put(key, _jpeg())
    r = pos_client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert r.status_code == 200 and _size(r.content) == (700, 525)       # made now and sent
    assert _get("thumbs/nowm/2026-10-02/mountain-biking/am/B.jpg")
    # the gallery and studio versions follow in the background from the same download
    assert _wait_for("thumbs/grid/2026-10-02/mountain-biking/am/B.jpg.webp")
    assert _wait_for("admin_thumbs/2026-10-02/mountain-biking/am/B.jpg")
    assert not _get("thumbs/v2/2026-10-02/mountain-biking/am/B.jpg")     # the old JPEG version isn't made any more
    r = pos_client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert r.status_code == 302 and _stored_key(r.headers["location"]) == "thumbs/nowm/2026-10-02/mountain-biking/am/B.jpg"


def test_stored_thumbnail_is_a_redirect_without_touching_the_original(client, main_module):
    key = "images/2026-10-03/mountain-biking/am/C.jpg"                   # no original uploaded at all
    _put("thumbs/grid/2026-10-03/mountain-biking/am/C.jpg.webp", b"webp")
    r = client.get("/api/photo", params={"path": key, "size": "thumb"}, follow_redirects=False)
    assert r.status_code == 302
    assert _stored_key(r.headers["location"]) == "thumbs/grid/2026-10-03/mountain-biking/am/C.jpg.webp"
    assert "X-Amz-Signature" in r.headers["location"]                    # a signed, expiring link
    assert "max-age=86400" in r.headers["cache-control"]
    # the same link is handed out again, so browsers reuse their saved copy
    again = client.get("/api/photo", params={"path": key, "size": "thumb"}, follow_redirects=False)
    assert again.headers["location"] == r.headers["location"]


def test_older_photo_keeps_its_jpeg_gallery_thumbnail(client, main_module):
    key = "images/2026-10-03/mountain-biking/am/OLD.jpg"
    _put("thumbs/v2/2026-10-03/mountain-biking/am/OLD.jpg", _jpeg(700, 525))
    r = client.get("/api/photo", params={"path": key, "size": "thumb"}, follow_redirects=False)
    assert r.status_code == 302 and _stored_key(r.headers["location"]) == "thumbs/v2/2026-10-03/mountain-biking/am/OLD.jpg"
    assert not _get("thumbs/grid/2026-10-03/mountain-biking/am/OLD.jpg.webp")   # nothing new made


def test_gallery_thumbnail_made_on_demand_is_webp(client, main_module):
    key = "images/2026-10-03/mountain-biking/am/NEW.jpg"
    _put(key, _jpeg())
    r = client.get("/api/photo", params={"path": key, "size": "thumb"}, follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp" and _size(r.content) == (700, 525)
    assert _get("thumbs/grid/2026-10-03/mountain-biking/am/NEW.jpg.webp")


def test_no_watermark_thumbnail_needs_a_signed_in_pos_or_studio(client, admin_client, main_module):
    key = "images/2026-10-03/mountain-biking/am/NW.jpg"
    _put("thumbs/nowm/2026-10-03/mountain-biking/am/NW.jpg", _jpeg(700, 525))
    _put("thumbs/grid/2026-10-03/mountain-biking/am/NW.jpg.webp", b"webp")
    r = client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert _stored_key(r.headers["location"]) == "thumbs/grid/2026-10-03/mountain-biking/am/NW.jpg.webp"   # watermarked
    client.cookies.set(main_module.POS_COOKIE, "9999999999.forged")
    r = client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert _stored_key(r.headers["location"]) == "thumbs/grid/2026-10-03/mountain-biking/am/NW.jpg.webp"
    r = admin_client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert _stored_key(r.headers["location"]) == "thumbs/nowm/2026-10-03/mountain-biking/am/NW.jpg"


def test_pos_pin_sets_the_staff_thumbnail_cookie(pos_client, main_module):
    key = "images/2026-10-03/mountain-biking/am/NW2.jpg"
    _put("thumbs/nowm/2026-10-03/mountain-biking/am/NW2.jpg", _jpeg(700, 525))
    r = pos_client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert _stored_key(r.headers["location"]) == "thumbs/nowm/2026-10-03/mountain-biking/am/NW2.jpg"


def test_expired_pos_cookie_gets_watermarked_thumbnails(client, main_module):
    import time as _t
    key = "images/2026-10-03/mountain-biking/am/NW3.jpg"
    _put("thumbs/nowm/2026-10-03/mountain-biking/am/NW3.jpg", _jpeg(700, 525))
    _put("thumbs/grid/2026-10-03/mountain-biking/am/NW3.jpg.webp", b"webp")
    client.cookies.set(main_module.POS_COOKIE, main_module._pos_token(int(_t.time()) - 5))
    r = client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1}, follow_redirects=False)
    assert _stored_key(r.headers["location"]).startswith("thumbs/grid/")


@pytest.mark.parametrize("size", ["medium", "full"])
def test_large_photo_without_watermark_needs_staff(client, admin_client, main_module, monkeypatch, size):
    key = f"images/2026-10-03/mountain-biking/am/BIG-{size}.jpg"
    _put(key, _jpeg(3000, 2000))
    marked = []
    real = main_module.apply_watermark
    monkeypatch.setattr(main_module, "apply_watermark", lambda img, s="medium": marked.append(s) or real(img, s))
    assert client.get("/api/photo", params={"path": key, "size": size, "nowm": 1}).status_code == 200
    assert marked == [size]                                  # a visitor asking for no watermark still gets one
    marked.clear()
    assert admin_client.get("/api/photo", params={"path": key, "size": size, "nowm": 1}).status_code == 200
    assert marked == []


def test_uploading_photos_queues_their_thumbnails(admin_client, main_module):
    keys = [f"images/2026-10-04/lone-peak-portraits/smith/P{i}.jpg" for i in range(3)]
    for k in keys:
        _put(k, _jpeg(3000, 2000))
    r = admin_client.post("/api/admin/upload/index", json={
        "date": "2026-10-04", "location": "Lone Peak Portraits", "folder": "smith", "keys": keys})
    assert r.status_code == 200 and r.json()["thumbs_job_id"]
    d = _wait_job(admin_client, r.json()["thumbs_job_id"])
    assert d["done"] == 3 and d["failed"] == 0
    for k in keys:
        tail = k[len("images/"):]
        assert _get("thumbs/nowm/" + tail) and _get("thumbs/grid/" + tail + ".webp") and _get("admin_thumbs/" + tail)
        assert not _get("thumbs/v2/" + tail)


def test_making_thumbnails_records_the_photo_size(main_module):
    key = "images/2026-10-05/x/D.jpg"
    _put(key, _jpeg(2000, 1500))
    main_module._make_thumbs(key, ["pos"])
    assert main_module._dim_cache[key] == [2000, 1500]


def test_pos_photo_list_skips_measuring_sizes(client, main_module, monkeypatch):
    saved = main_module.data
    calls = []
    monkeypatch.setattr(main_module, "_dims_for_paths", lambda paths: calls.append(paths) or {})
    try:
        main_module.data = [{"path": "images/2026-10-08/mountain-biking/IMG_1.jpg", "date": "2026-10-08",
                             "location": "Mountain Biking", "last_name": "", "group": ""}]
        r = client.get("/api/browse", params={"date": "2026-10-08", "location": "Mountain Biking", "dims": 0})
        assert r.status_code == 200 and r.json()["count"] == 1 and calls == []
        client.get("/api/browse", params={"date": "2026-10-08", "location": "Mountain Biking"})
        assert len(calls) == 1                                   # the gallery still gets sizes
    finally:
        main_module.data = saved


def test_missing_original_counts_as_failed_not_crash(admin_client, main_module):
    job = main_module._start_thumb_job([("images/2026-10-07/x/NOPE.jpg", ["pos"])])
    d = _wait_job(admin_client, job)
    assert d["done"] == 1 and d["failed"] == 1


def test_pos_thumbnails_only_for_chosen_dates_and_safe_to_rerun(admin_client, main_module):
    saved = main_module.data
    try:
        main_module.data = [
            {"path": "images/2026-06-10/ambush-action/W1.jpg", "date": "2026-06-10", "location": "Ambush Action"},
            {"path": "images/2026-06-12/ambush-action/W2.jpg", "date": "2026-06-12", "location": "Ambush Action"},
            {"path": "images/2026-08-01/mountain-biking/S1.jpg", "date": "2026-08-01", "location": "Mountain Biking"},
        ]
        for it in main_module.data:
            _put(it["path"], _jpeg(2000, 1500))
        r = admin_client.post("/api/admin/thumbs/pos", json={"date_to": "2026-06-12"}).json()
        assert r["in_range"] == 2 and r["queued"] == 2
        _wait_job(admin_client, r["job_id"])
        assert _get("thumbs/nowm/2026-06-10/ambush-action/W1.jpg") and _get("thumbs/nowm/2026-06-12/ambush-action/W2.jpg")
        assert not _get("thumbs/nowm/2026-08-01/mountain-biking/S1.jpg")          # summer photo left alone
        assert not _get("thumbs/grid/2026-06-10/ambush-action/W1.jpg.webp")        # POS version only
        again = admin_client.post("/api/admin/thumbs/pos", json={"date_to": "2026-06-12"}).json()
        assert again["job_id"] is None and again["in_range"] == 2
    finally:
        main_module.data = saved


def test_pos_thumbnails_requires_admin_and_a_date(client, admin_client):
    assert client.post("/api/admin/thumbs/pos", json={"date_to": "2026-06-12"}).status_code == 401
    assert admin_client.post("/api/admin/thumbs/pos", json={}).status_code == 400


def _camera_like_jpeg(w=6000, h=4000) -> bytes:
    """A JPEG with ~120 KB of extra information before the size, like Oren's camera files."""
    buf = BytesIO()
    Image.new("RGB", (w, h), (90, 140, 200)).save(buf, "JPEG", quality=60, icc_profile=bytes(120_000))
    return buf.getvalue()


def test_photo_size_is_measured_and_remembered_for_camera_files(main_module, monkeypatch):
    key = "images/2026-10-09/ambush-action/CAM1.JPG"
    _put(key, _camera_like_jpeg())
    assert main_module._dims_for_paths([key]) == {key: (6000, 4000)}
    calls = []
    monkeypatch.setattr(main_module, "_fetch_dims", lambda p: calls.append(p) or (0, 0))
    assert main_module._dims_for_paths([key]) == {key: (6000, 4000)}
    assert calls == []                                      # second visit: nothing measured again


def test_slow_measuring_never_holds_a_folder_past_the_limit(main_module, monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(main_module, "_DIMS_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(main_module, "_fetch_dims", lambda p: release.wait(5) and (4000, 3000))
    key = "images/2026-10-09/ambush-action/SLOW.JPG"
    start = time.time()
    assert main_module._dims_for_paths([key]) == {key: (0, 0)}
    assert time.time() - start < 2
    release.set()                                           # the straggler finishes in the background...
    end = time.time() + 3
    while key not in main_module._dim_cache and time.time() < end:
        time.sleep(0.05)
    assert main_module._dim_cache[key] == [4000, 3000]      # ...and is remembered for the next visit
