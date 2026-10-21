"""Partner programs (Eagle Mount): marked photos stay out of the public gallery but on the POS,
and participants download them free on a password-protected page; downloads are logged."""
import json
from io import BytesIO

import boto3
import pytest
from fastapi.testclient import TestClient
from PIL import Image

S3 = boto3.client("s3", region_name="us-east-1")
DAY1, DAY2 = "2026-12-05", "2026-12-06"
PW = "eagle-test"


def _jpeg(color=(30, 120, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1200, 800), color).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _photo(name, date=DAY1, partner=None):
    item = {"path": f"images/{date}/big-sky-ski-school/{name}", "date": date, "location": "Big Sky Ski School",
            "last_name": "", "group": ""}
    if partner:
        item["partner"] = partner
    return item


PHOTOS = [_photo("DSC_0001.JPG"), _photo("DSC_0002.JPG", partner="eagle-mount"),
          _photo("DSC_0003.JPG", partner="eagle-mount"), _photo("DSC_0010.JPG", date=DAY2, partner="eagle-mount"),
          _photo("DSC_0020.JPG", date="2026-12-07", partner="eagle-mount")]   # a day with only Eagle Mount


def _partners(password=PW, **extra):
    S3.put_object(Bucket="test-bucket", Key="meta/partners.json", Body=json.dumps([
        {"id": "eagle-mount", "name": "Eagle Mount", "label": "Orange", "slug": "eaglemount",
         "password": password, "welcome": "Welcome!", **extra}]).encode())


@pytest.fixture(autouse=True)
def setup(main_module, live_photos):
    for p in PHOTOS:
        S3.put_object(Bucket="test-bucket", Key=p["path"], Body=_jpeg())
    S3.delete_object(Bucket="test-bucket", Key="meta/partner_downloads/eagle-mount.json")
    main_module._partner_logs.clear()
    main_module._pin_fail_limiter._calls.clear()
    _partners()
    main_module._partners_doc.cache["data"] = None
    live_photos(PHOTOS)
    yield
    S3.delete_object(Bucket="test-bucket", Key="meta/partners.json")
    main_module._partners_doc.cache["data"] = None


@pytest.fixture()
def member(main_module):
    c = TestClient(main_module.app)
    r = c.post("/partner/eaglemount/login", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303
    return c


def _names(photos):
    return sorted(p["path"].rsplit("/", 1)[-1] for p in photos)


# ── Hidden from the public, still on the POS ──────────────────────────────────

def test_public_gallery_doesnt_show_eagle_mount_photos(client):
    browse = client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert _names(browse["photos"]) == ["DSC_0001.JPG"]
    days = [d["date"] for d in client.get("/api/days").json()["days"]]
    assert DAY1 in days and DAY2 not in days and "2026-12-07" not in days
    assert "2026-12-07" not in client.get("/api/dates").json()["dates"]
    assert client.get("/api/locations", params={"date": DAY2}).json()["locations"] == []
    assert client.get("/api/folder-dates", params={"location": "Big Sky Ski School"}).json()["dates"] == [DAY1]


def test_the_pos_still_sees_them(admin_client, main_module):
    browse = admin_client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert _names(browse["photos"]) == ["DSC_0001.JPG", "DSC_0002.JPG", "DSC_0003.JPG"]
    pos_days = [d["date"] for d in admin_client.get("/api/pos-dates").json()["dates"]]
    assert DAY2 in pos_days and "2026-12-07" in pos_days


def test_they_cant_be_bought_online(main_module):
    assert PHOTOS[1]["path"] not in main_module._folder_index()["by_path"]
    assert PHOTOS[1]["path"] in main_module._folder_index()["all_by_path"]


# ── The password page ─────────────────────────────────────────────────────────

def test_page_needs_the_password(client, main_module):
    page = client.get("/partner/eaglemount")
    assert page.status_code == 200 and 'type="password"' in page.text and "noindex" in page.text
    assert client.get("/api/partner/eaglemount/photos").status_code == 401
    bad = client.post("/partner/eaglemount/login", data={"password": "nope"})
    assert bad.status_code == 401 and "isn't right" in bad.text
    assert client.get("/eaglemount", follow_redirects=False).headers["location"] == "/partner/eaglemount"


def test_page_is_closed_until_a_password_is_set(client, main_module):
    _partners(password="")
    main_module._partners_doc.cache["data"] = None
    assert client.get("/partner/eaglemount").status_code == 404
    assert client.post("/partner/eaglemount/login", data={"password": ""}).status_code == 404


def test_signed_in_member_sees_their_photos_by_day(member):
    page = member.get("/partner/eaglemount")
    assert page.status_code == 200 and "Welcome!" in page.text and "Download all from this day" in page.text
    d = member.get("/api/partner/eaglemount/photos").json()
    assert [day["date"] for day in d["days"]] == ["2026-12-07", DAY2, DAY1]          # newest first
    assert [p["filename"] for p in d["days"][2]["photos"]] == ["DSC_0002.JPG", "DSC_0003.JPG"]


def test_changing_the_password_signs_everyone_out(member, main_module):
    _partners(password="new-season")
    main_module._partners_doc.cache["data"] = None
    assert member.get("/api/partner/eaglemount/photos").status_code == 401


def test_too_many_wrong_passwords_lock_out(client, main_module):
    for _ in range(main_module._pin_fail_limiter.max_calls):
        client.post("/partner/eaglemount/login", data={"password": "nope"})
    assert client.post("/partner/eaglemount/login", data={"password": PW}).status_code == 429


# ── Downloads and the donation log ────────────────────────────────────────────

def _log():
    return json.loads(S3.get_object(Bucket="test-bucket", Key="meta/partner_downloads/eagle-mount.json")["Body"].read())


def test_single_download_is_the_original_and_is_logged(member):
    r = member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    assert r.status_code == 307 and "DSC_0002.JPG" in r.headers["location"]
    member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    rec = _log()["photos"][PHOTOS[1]["path"]]
    assert rec["count"] == 2 and rec["photo_date"] == DAY1          # counted once for the report, twice downloaded


def test_only_their_own_photos_can_be_downloaded(member, client):
    assert member.get("/partner/eaglemount/file", params={"path": PHOTOS[0]["path"]}).status_code == 404
    assert client.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}).status_code == 404
    assert member.get("/partner/eaglemount/thumb", params={"path": PHOTOS[0]["path"]}).status_code == 404


def test_download_a_whole_day_as_a_zip(member):
    r = member.post("/partner/eaglemount/zip", data={"date": DAY1})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    import zipfile
    names = sorted(zipfile.ZipFile(BytesIO(r.content)).namelist())
    assert names == ["DSC_0002.JPG", "DSC_0003.JPG"]
    assert set(_log()["photos"]) == {PHOTOS[1]["path"], PHOTOS[2]["path"]}


def test_download_a_selection_skips_anything_not_theirs(member):
    import zipfile
    wanted = [PHOTOS[3]["path"], PHOTOS[0]["path"], "images/../meta/pricing.json"]
    r = member.post("/partner/eaglemount/zip", data={"paths": json.dumps(wanted)})
    assert zipfile.ZipFile(BytesIO(r.content)).namelist() == ["DSC_0010.JPG"]


def test_member_thumbnails_and_enlarged_view_are_unwatermarked(member, main_module, monkeypatch):
    marked = []
    monkeypatch.setattr(main_module, "apply_watermark", lambda img, s="medium": marked.append(s) or img)
    assert member.get("/partner/eaglemount/thumb", params={"path": PHOTOS[1]["path"]}).status_code == 200
    v = member.get("/partner/eaglemount/view", params={"path": PHOTOS[1]["path"]})
    assert v.status_code == 200 and v.headers["content-type"] == "image/jpeg" and marked == []
