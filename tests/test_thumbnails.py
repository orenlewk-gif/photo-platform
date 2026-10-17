"""Thumbnails: made once per photo in all three versions, in the background and on demand."""
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


def test_one_download_makes_all_three_versions(main_module):
    key = "images/2026-10-01/mountain-biking/am/A.jpg"
    _put(key, _jpeg())
    out = main_module._make_thumbs(key, list(main_module._THUMB_VARIANTS))
    assert _size(out["public"]) == (700, 525) and _size(out["pos"]) == (700, 525)
    assert _size(out["admin"]) == (450, 338)
    assert out["public"] != out["pos"]                       # gallery copy is watermarked
    assert _get("thumbs/v2/2026-10-01/mountain-biking/am/A.jpg")
    assert _get("thumbs/nowm/2026-10-01/mountain-biking/am/A.jpg")
    assert _get("admin_thumbs/2026-10-01/mountain-biking/am/A.jpg")


def test_rotated_photo_comes_out_upright(main_module):
    key = "images/2026-10-01/mountain-biking/am/ROT.jpg"
    _put(key, _jpeg(4000, 3000, orientation=6))               # camera held sideways
    out = main_module._make_thumbs(key, ["pos"])
    assert _size(out["pos"]) == (525, 700)


def test_pos_thumbnail_on_demand_then_cached_and_others_made(client, main_module):
    key = "images/2026-10-02/mountain-biking/am/B.jpg"
    _put(key, _jpeg())
    r = client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1})
    assert r.status_code == 200 and _size(r.content) == (700, 525)
    assert _get("thumbs/nowm/2026-10-02/mountain-biking/am/B.jpg")
    # the gallery and studio versions follow in the background from the same download
    assert _wait_for("thumbs/v2/2026-10-02/mountain-biking/am/B.jpg")
    assert _wait_for("admin_thumbs/2026-10-02/mountain-biking/am/B.jpg")


def test_cached_thumbnail_is_served_without_touching_the_original(client, main_module):
    key = "images/2026-10-03/mountain-biking/am/C.jpg"
    _put("thumbs/nowm/2026-10-03/mountain-biking/am/C.jpg", _jpeg(700, 525, (200, 10, 10)))
    r = client.get("/api/photo", params={"path": key, "size": "thumb", "nowm": 1})   # no original uploaded at all
    assert r.status_code == 200 and _size(r.content) == (700, 525)


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
        assert _get("thumbs/nowm/" + tail) and _get("thumbs/v2/" + tail) and _get("admin_thumbs/" + tail)


def test_backfill_makes_only_missing_thumbnails_and_is_safe_to_rerun(admin_client, main_module):
    saved = main_module.data
    try:
        main_module.data = [
            {"path": "images/2026-10-05/x/D.jpg", "date": "2026-10-05", "location": "X"},
            {"path": "images/2026-10-06/x/E.jpg", "date": "2026-10-06", "location": "X"},
        ]
        _put("images/2026-10-05/x/D.jpg", _jpeg(2000, 1500))
        _put("images/2026-10-06/x/E.jpg", _jpeg(2000, 1500))
        main_module._make_thumbs("images/2026-10-06/x/E.jpg", list(main_module._THUMB_VARIANTS))   # E already done
        r = admin_client.post("/api/admin/thumbs/backfill").json()
        assert r["queued"] == 1
        _wait_job(admin_client, r["job_id"])
        assert _get("thumbs/nowm/2026-10-05/x/D.jpg")
        assert admin_client.post("/api/admin/thumbs/backfill").json()["job_id"] is None   # nothing left
    finally:
        main_module.data = saved


def test_backfill_requires_admin(client):
    assert client.post("/api/admin/thumbs/backfill").status_code == 401


def test_missing_original_counts_as_failed_not_crash(admin_client, main_module):
    job = main_module._start_thumb_job([("images/2026-10-07/x/NOPE.jpg", ["pos"])])
    d = _wait_job(admin_client, job)
    assert d["done"] == 1 and d["failed"] == 1
