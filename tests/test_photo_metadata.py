"""Pins down what uploads read from the photo files (Lightroom color labels -> portrait poses,
capture times for the time filter) and how colour / name search match, before code moves."""
import json
from io import BytesIO

import boto3
import pytest
from PIL import Image

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"
XMP = b"http://ns.adobe.com/xap/1.0/\x00"


def _labeled(label=None, element=False) -> bytes:
    buf = BytesIO(); Image.new("RGB", (60, 40)).save(buf, "JPEG"); body = buf.getvalue()
    if label is None:
        return body
    xmp = (b"<xmp:Label>" + label.encode() + b"</xmp:Label>") if element else (b'xmp:Label="' + label.encode() + b'"')
    return body[:2] + XMP + b"<x:xmpmeta " + xmp + b"/>" + body[2:]


# ── Lightroom color labels -> poses ──────────────────────────────────────────

def test_reads_the_color_label_in_both_xmp_styles(main_module):
    assert main_module.read_xmp_color_label(_labeled("Red")) == "Red"
    assert main_module.read_xmp_color_label(_labeled("Green", element=True)) == "Green"
    assert main_module.read_xmp_color_label(_labeled()) == ""


@pytest.fixture()
def folder_meta(main_module):
    S3.put_object(Bucket=B, Key="meta/folder_meta.json", Body=b"{}")
    main_module._folder_meta_cache["data"] = None
    yield lambda: json.loads(S3.get_object(Bucket=B, Key="meta/folder_meta.json")["Body"].read())
    S3.delete_object(Bucket=B, Key="meta/folder_meta.json")
    main_module._folder_meta_cache["data"] = None


def test_poses_follow_the_color_labels_in_file_order(main_module, folder_meta):
    base = "images/2026-12-05/lone-peak-portraits/smith/"
    files = {"IMG_3.jpg": "Green", "IMG_1.jpg": "Red", "IMG_2.jpg": "Red", "IMG_4.jpg": None, "IMG_5.jpg": "Green"}
    for name, label in files.items():
        S3.put_object(Bucket=B, Key=base + name, Body=_labeled(label))
    main_module._update_folder_poses("2026-12-05", "Lone Peak Portraits", "smith", [base + n for n in files])
    entry = folder_meta()["2026-12-05|Lone Peak Portraits|smith"]
    assert entry["poses"] == [
        {"color": "Red", "label": "Pose 1", "files": ["IMG_1.jpg", "IMG_2.jpg"]},
        {"color": "Green", "label": "Pose 2", "files": ["IMG_3.jpg", "IMG_5.jpg"]},
        {"color": "", "label": "Unlabeled", "files": ["IMG_4.jpg"]},
    ]
    assert entry["poses_live"] is True


def test_no_color_labels_means_no_poses_saved(main_module, folder_meta):
    key = "images/2026-12-05/lone-peak-portraits/jones/IMG_1.jpg"
    S3.put_object(Bucket=B, Key=key, Body=_labeled())
    main_module._update_folder_poses("2026-12-05", "Lone Peak Portraits", "jones", [key])
    assert "2026-12-05|Lone Peak Portraits|jones" not in folder_meta()


# ── Capture times for the time filter ────────────────────────────────────────

def _with_time(when: str) -> bytes:
    exif = Image.Exif(); exif[0x8769] = {0x9003: when}          # DateTimeOriginal
    buf = BytesIO(); Image.new("RGB", (60, 40)).save(buf, "JPEG", exif=exif.tobytes()); return buf.getvalue()


@pytest.fixture()
def photos(main_module):
    saved = main_module.data
    yield lambda items: setattr(main_module, "data", items)
    main_module.data = saved
    main_module._folder_times_cache.clear()


def test_capture_times_read_from_the_photos_when_not_saved_at_upload(client, main_module, photos):
    base = "images/2026-12-06/el-dorado-action/"
    S3.put_object(Bucket=B, Key=base + "A1.JPG", Body=_with_time("2026:12:06 10:17:33"))
    S3.put_object(Bucket=B, Key=base + "A2.JPG", Body=_with_time("2026:12:06 14:05:00"))
    S3.put_object(Bucket=B, Key=base + "D1.JPG", Body=_with_time("2026:12:06 09:00:00"))
    photos([{"path": base + "A1.JPG", "date": "2026-12-06", "location": "El Dorado Action"},
            {"path": base + "A2.JPG", "date": "2026-12-06", "location": "El Dorado Action"},
            {"path": base + "D1.JPG", "date": "2026-12-06", "location": "El Dorado Action", "draft": True}])
    r = client.get("/api/folder-times", params={"date": "2026-12-06", "location": "El Dorado Action"}).json()
    assert r == {"times": {"A1.JPG": 617, "A2.JPG": 845}}              # minutes after midnight; drafts left out


def test_capture_times_saved_at_upload_are_used_first(client, main_module, photos):
    photos([])
    S3.put_object(Bucket=B, Key="meta/times/2026-12-07/el-dorado-action.json", Body=json.dumps({"X.JPG": 600}).encode())
    r = client.get("/api/folder-times", params={"date": "2026-12-07", "location": "El Dorado Action"}).json()
    assert r == {"times": {"X.JPG": 600}}


# ── Colour and name search ───────────────────────────────────────────────────

def _p(name, **kw):
    return {"path": f"images/2026-12-08/el-dorado-action/{name}.jpg", "date": "2026-12-08",
            "location": "El Dorado Action", "last_name": "", "group": "", **kw}


@pytest.fixture()
def search(client, main_module, photos, monkeypatch):
    monkeypatch.setattr(main_module, "_dims_for_paths", lambda paths: {})
    main_module._search_limiter._calls.clear()
    photos([_p("navy", jacket_colors=["navy"], pants_colors=["black"], sport="ski"),
            _p("blue_red", jacket_colors=["blue", "red"], pants_colors=["blue"], sport="snowboard"),
            _p("maroon", jacket_colors=["maroon"], pants_colors=["black"], sport="ski"),
            _p("draft_blue", jacket_colors=["blue"], pants_colors=["black"], draft=True),
            _p("unlabeled")])
    main_module._index_state["version"] += 1

    def run(**params):
        q = [("date", "2026-12-08"), ("location", "El Dorado Action")]
        for k, v in params.items():
            q += [(k, x) for x in v] if isinstance(v, list) else [(k, v)]
        return sorted(p["filename"] for p in client.get("/api/search", params=q).json()["photos"])
    return run


def test_colour_search_counts_similar_shades_and_skips_drafts(search):
    assert search(jacket_color="blue") == ["blue_red.jpg", "navy.jpg"]      # navy counts as blue
    assert search(jacket_color="red") == ["blue_red.jpg", "maroon.jpg"]     # maroon counts as red


def test_two_jacket_colours_must_both_match(search):
    assert search(jacket_color=["blue", "red"]) == ["blue_red.jpg"]


def test_pants_and_sport_filters(search):
    assert search(pants_color="black") == ["maroon.jpg", "navy.jpg"]
    assert search(jacket_color="blue", sport="snowboard") == ["blue_red.jpg"]


def test_last_name_search_is_forgiving_about_spelling(client, main_module, photos, monkeypatch):
    monkeypatch.setattr(main_module, "_dims_for_paths", lambda paths: {})
    main_module._search_limiter._calls.clear()
    photos([_p("a", last_name="Smythe"), _p("b", last_name="Jones"), _p("c", last_name="Smith")])
    names = lambda q: sorted(p["last_name"] for p in client.get("/api/search", params={"last_name": q}).json()["photos"])
    assert names("smyth") == ["Smith", "Smythe"]                          # close spellings both match
    assert names("jone") == ["Jones"]
    assert client.get("/api/search").status_code == 400                   # needs a name or a filter
