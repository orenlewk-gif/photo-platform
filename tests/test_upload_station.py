"""Upload Station (/u/<random>): the store desktops' upload page. Its address is random (any other
/u/ address is a plain "not found"); a computer is approved once with a code emailed to the owner.
A station can do the upload steps and nothing else of the studio, and can move, trash and bring
back only the photos uploaded today (from "Today's uploads")."""
from io import BytesIO

import boto3
import pytest
from fastapi.testclient import TestClient
from PIL import Image

S3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "test-bucket"
DATE = "2026-12-05"
ACTION = "Big Sky Ski School"
PORTRAIT = "Lone Peak Portraits"


def _jpeg():
    buf = BytesIO()
    Image.new("RGB", (900, 600), (40, 90, 160)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture()
def emails(main_module, monkeypatch):
    """Codes emailed to the owner: (computer name, code)."""
    sent = []
    monkeypatch.setattr(main_module, "_send_upload_station_email", lambda info, name, code: sent.append((name, code)) or True)
    main_module._station_challenges.clear()
    main_module._station_request_limiter._calls.clear()
    auth = main_module._load_admin_auth()
    auth["upload_stations"] = {}
    auth.pop("upload_station_path", None)
    _clear_log(main_module)
    yield sent
    auth["upload_stations"] = {}
    _clear_log(main_module)


def _clear_log(main_module):
    """Today's upload log starts empty (other tests upload too)."""
    with main_module._upload_log_lock:
        main_module._upload_logs.clear()
        main_module._upload_logs[main_module._mountain_today()] = []
        main_module._upload_log_dirty.clear()
    S3.delete_object(Bucket=BUCKET, Key=main_module._upload_log_key(main_module._mountain_today()))


@pytest.fixture(autouse=True)
def fresh_list(live_photos):
    live_photos([])


def _path(main_module):
    return main_module._device_path("station")


def _approve(c, main_module, emails, name="Office iMac"):
    r = c.post("/api/station/request", json={"name": name, "path": _path(main_module)})
    assert r.status_code == 200 and r.json()["challenge"], r.text
    assert emails[-1][0] == name and len(emails[-1][1]) == 6
    r = c.post("/api/station/verify", json={"challenge": r.json()["challenge"], "code": emails[-1][1]})
    assert r.status_code == 200, r.text
    return c


def _upload(c, location, names, folder=""):
    """Upload photos the way the page does: presign, put the files, add them to the site."""
    r = c.post("/api/admin/upload/presign", json={"date": DATE, "location": location, "folder": folder,
                                                  "files": [{"name": n, "type": "image/jpeg"} for n in names]})
    assert r.status_code == 200, r.text
    keys = [u["key"] for u in r.json()["urls"]]
    for k in keys:
        S3.put_object(Bucket=BUCKET, Key=k, Body=_jpeg())
    r = c.post("/api/admin/upload/index", json={"date": DATE, "location": location, "folder": folder, "keys": keys})
    assert r.status_code == 200, r.text
    return keys


def test_only_the_random_address_answers(main_module, emails, client):
    assert client.get("/u/guess").status_code == 404
    assert client.get("/u/guess").json() == client.get("/no-such-page/at-all").json()
    assert client.get("/u/guess/manifest.json").status_code == 404
    r = client.get(f"/u/{_path(main_module)}")
    assert r.status_code == 403 and "isn't approved to upload photos yet" in r.text
    assert "upload.js" not in r.text and r.headers["x-robots-tag"].startswith("noindex")
    m = client.get(f"/u/{_path(main_module)}/manifest.json").json()
    assert m["start_url"] == f"/u/{_path(main_module)}" and m["display"] == "standalone"


def test_asking_for_a_code_needs_the_address_and_a_name(main_module, emails, client):
    assert client.post("/api/station/request", json={"name": "Office iMac", "path": "guess"}).status_code == 404
    assert client.post("/api/station/request", json={"name": " ", "path": _path(main_module)}).status_code == 400
    assert emails == []


def test_the_emailed_code_approves_the_computer(owner_client, main_module, emails, admin_client):
    c = _approve(TestClient(main_module.app), main_module, emails)
    page = c.get(f"/u/{_path(main_module)}")
    assert page.status_code == 200 and "Office iMac" in page.text
    assert '<div class="upload-card">' in page.text and "/static/upload.js" in page.text
    assert "<!--upload-form-->" not in page.text
    listed = owner_client.get("/api/admin/upload-stations").json()
    assert [s["name"] for s in listed["stations"]] == ["Office iMac"]
    links = {l["kind"]: l["link"] for l in owner_client.get("/api/admin/device-links").json()["links"]}
    assert links["station"].endswith(f"/u/{_path(main_module)}")


def test_wrong_codes_run_out(main_module, emails):
    c = TestClient(main_module.app)
    challenge = c.post("/api/station/request", json={"name": "Kiosk", "path": _path(main_module)}).json()["challenge"]
    wrong = "000000" if emails[-1][1] != "000000" else "111111"
    for _ in range(main_module.LOGIN_CODE_TRIES):
        r = c.post("/api/station/verify", json={"challenge": challenge, "code": wrong})
    assert r.status_code == 401 and r.json()["restart"]
    assert c.post("/api/station/verify", json={"challenge": challenge, "code": emails[-1][1]}).status_code == 401
    assert main_module.UPLOAD_STATION_COOKIE not in c.cookies


def test_a_station_uploads_and_its_name_is_in_todays_list(main_module, emails):
    c = _approve(TestClient(main_module.app), main_module, emails)
    keys = _upload(c, ACTION, ["DSC_0001.JPG", "DSC_0002.JPG"])
    assert {i["path"] for i in main_module.data} == set(keys)
    assert c.post("/api/admin/push-live", json={"date": DATE, "location": ACTION, "folder": ""}).status_code in (200, 400)
    fam = _upload(c, PORTRAIT, ["WWB_0101.jpg"], folder="Smith")
    today = c.get("/api/station/today").json()
    groups = {(g["location"], g["folder"]): g for g in today["groups"]}
    assert groups[(ACTION, "")]["count"] == 2 and groups[(ACTION, "")]["by"] == ["Office iMac"]
    assert groups[(PORTRAIT, "Smith")]["photos"][0]["path"] == fam[0]
    assert c.get("/api/station/photo", params={"path": keys[0]}).status_code == 200


def test_a_station_cant_use_the_rest_of_the_studio(main_module, emails):
    c = _approve(TestClient(main_module.app), main_module, emails)
    keys = _upload(c, ACTION, ["DSC_0003.JPG"])
    assert c.get("/api/admin/folders").status_code == 401
    assert c.get("/api/admin/photo", params={"path": keys[0]}).status_code == 401
    assert c.post("/api/admin/photos/move", json={"paths": keys, "new_date": DATE, "new_location": PORTRAIT,
                                                  "new_folder": "X"}).status_code == 401
    assert c.post("/api/admin/photo/trash", json={"paths": keys}).status_code == 401
    assert c.post("/api/admin/delete-folder", json={"date": DATE, "location": ACTION, "folder": ""}).status_code == 401
    assert c.get("/api/admin/upload-stations").status_code == 401
    assert c.get("/admin/dashboard", follow_redirects=False).status_code in (302, 303, 307)


def test_nobody_else_can_upload_or_see_today(main_module, emails, client):
    assert client.post("/api/admin/upload/presign", json={"date": DATE, "location": ACTION,
                                                          "files": [{"name": "A.JPG", "type": "image/jpeg"}]}).status_code == 401
    assert client.get("/api/station/today").status_code == 403
    # A store screen (customers use those) is a different list: it can't upload
    store = TestClient(main_module.app)
    store.cookies.set(main_module.STORE_SCREEN_COOKIE, main_module._approve_store_screen("Front", {}))
    assert store.post("/api/admin/upload/presign", json={"date": DATE, "location": ACTION,
                                                         "files": [{"name": "A.JPG", "type": "image/jpeg"}]}).status_code == 401
    main_module._load_admin_auth()["store_screens"] = {}


def test_todays_photos_can_be_moved_and_stay_in_the_list(main_module, emails):
    c = _approve(TestClient(main_module.app), main_module, emails)
    keys = _upload(c, ACTION, ["DSC_0010.JPG", "DSC_0011.JPG"])
    r = c.post("/api/station/move", json={"paths": [keys[0]], "new_date": DATE, "new_location": PORTRAIT,
                                          "new_folder": "Garcia"})
    assert r.status_code == 200 and r.json()["moved"] == 1, r.text
    moved = f"images/{DATE}/lone-peak-portraits/garcia/DSC_0010.JPG"
    assert any(i["path"] == moved and i["last_name"] == "Garcia" for i in main_module.data)
    groups = {(g["location"], g["folder"]): g["count"] for g in c.get("/api/station/today").json()["groups"]}
    assert groups == {(ACTION, ""): 1, (PORTRAIT, "Garcia"): 1}
    # Moved again from its new place (the log follows it)
    r = c.post("/api/station/move", json={"paths": [moved], "new_date": DATE, "new_location": ACTION})
    assert r.status_code == 200 and r.json()["moved"] == 1


def test_only_todays_photos_can_be_moved_or_trashed(main_module, emails, live_photos):
    old = f"images/2026-11-20/{ACTION.lower().replace(' ', '-')}/OLD_0001.JPG"
    live_photos([{"path": old, "date": "2026-11-20", "location": ACTION, "group": "", "last_name": ""}])
    S3.put_object(Bucket=BUCKET, Key=old, Body=_jpeg())
    c = _approve(TestClient(main_module.app), main_module, emails)
    assert c.post("/api/station/move", json={"paths": [old], "new_date": DATE, "new_location": ACTION}).status_code == 400
    assert c.post("/api/station/trash", json={"paths": [old]}).status_code == 400
    assert c.get("/api/station/photo", params={"path": old}).status_code == 404
    assert any(i["path"] == old for i in main_module.data)


def test_todays_photos_go_to_the_trash_and_come_back(main_module, emails):
    c = _approve(TestClient(main_module.app), main_module, emails)
    keys = _upload(c, ACTION, ["DSC_0020.JPG", "DSC_0021.JPG"])
    r = c.post("/api/station/trash", json={"paths": [keys[1]]})
    assert r.status_code == 200 and r.json()["trashed"] == 1
    assert keys[1] not in {i["path"] for i in main_module.data}
    today = c.get("/api/station/today").json()
    assert [t["path"] for t in today["trash"]] == [keys[1]] and today["groups"][0]["count"] == 1
    r = c.post("/api/station/restore", json={"path": keys[1]})
    assert r.status_code == 200, r.text
    assert keys[1] in {i["path"] for i in main_module.data}
    assert c.get("/api/station/today").json()["trash"] == []
    main_module._save_trash_meta([])


def test_a_new_link_retires_the_old_one(owner_client, main_module, emails, admin_client):
    c = _approve(TestClient(main_module.app), main_module, emails)
    old = _path(main_module)
    new = owner_client.post("/api/admin/device-links/station/new").json()["link"]
    assert c.get(f"/u/{old}").status_code == 404
    assert new.endswith(f"/u/{_path(main_module)}") and c.get(f"/u/{_path(main_module)}").status_code == 200


def test_renaming_and_removing_a_station(owner_client, main_module, emails, admin_client):
    c = _approve(TestClient(main_module.app), main_module, emails)
    sid = owner_client.get("/api/admin/upload-stations").json()["stations"][0]["id"]
    assert owner_client.put(f"/api/admin/upload-stations/{sid}", json={"name": "Back office"}).status_code == 200
    assert "Back office" in c.get(f"/u/{_path(main_module)}").text
    assert owner_client.delete(f"/api/admin/upload-stations/{sid}").status_code == 200
    assert c.get(f"/u/{_path(main_module)}").status_code == 403
    assert c.post("/api/admin/upload/presign", json={"date": DATE, "location": ACTION,
                                                     "files": [{"name": "A.JPG", "type": "image/jpeg"}]}).status_code == 401


def test_the_studio_still_uploads_and_shows_in_todays_list(main_module, emails, admin_client):
    keys = _upload(admin_client, ACTION, ["DSC_0030.JPG"])
    g = admin_client.get("/api/station/today").json()["groups"][0]
    assert g["by"] == ["Studio"] and g["photos"][0]["path"] == keys[0]
    page = admin_client.get("/admin/dashboard")
    assert page.status_code == 200 and '<div class="upload-card">' in page.text and "<!--upload-form-->" not in page.text
