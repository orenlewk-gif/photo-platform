"""Store mode (/store): only approved store screens get in. A new screen asks for a code, which is
emailed to the owner and typed on the screen once; any other device is blocked and the owner is
told. An approved screen sees published photos without watermarks, but can't use the POS."""
import json
from io import BytesIO

import boto3
import pytest
from fastapi.testclient import TestClient
from PIL import Image

S3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "test-bucket"
PUB = "images/2026-12-06/big-sky-ski-school/PUB_0001.JPG"
DRAFT = "images/2026-12-06/big-sky-ski-school/DRAFT_0001.JPG"
EM = "images/2026-12-06/big-sky-ski-school/EM_0001.JPG"


def _jpeg():
    buf = BytesIO()
    Image.new("RGB", (1600, 1200), (40, 90, 160)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture()
def emails(main_module, monkeypatch):
    """Emails to the owner: (name, code) for a code, ("", "") for a blocked visit."""
    sent = []
    monkeypatch.setattr(main_module, "_send_store_screen_email",
                        lambda info, name="", code="": sent.append((name, code)) or True)
    main_module._store_challenges.clear()
    for lim in (main_module._store_request_limiter, main_module._store_notice_limiter,
                main_module._store_notice_daily):
        lim._calls.clear()
    main_module._load_admin_auth()["store_screens"] = {}
    yield sent
    main_module._load_admin_auth()["store_screens"] = {}


@pytest.fixture()
def photos(main_module, live_photos):
    base = {"date": "2026-12-06", "location": "Big Sky Ski School", "group": ""}
    live_photos([{**base, "path": PUB, "last_name": "Jones"},
                 {**base, "path": DRAFT, "last_name": "Pulledback", "draft": True},
                 {**base, "path": EM, "last_name": "", "partner": "eagle-mount"}])
    for key in (PUB, DRAFT, EM):
        S3.put_object(Bucket=BUCKET, Key=key, Body=_jpeg())
    main_module._preview_device_limiter._calls.clear()
    main_module._preview_ip_limiter._calls.clear()
    yield
    main_module._preview_device_limiter._calls.clear()
    main_module._preview_ip_limiter._calls.clear()


def _approve(c, emails, name="Front counter iMac"):
    r = c.post("/api/store/request", json={"name": name})
    assert r.status_code == 200 and r.json()["challenge"], r.text
    assert emails[-1][0] == name and len(emails[-1][1]) == 6
    return c.post("/api/store/verify", json={"challenge": r.json()["challenge"], "code": emails[-1][1]})


def test_a_device_that_isnt_approved_sees_the_locked_screen_and_the_owner_is_told(main_module, emails):
    c = TestClient(main_module.app)
    r = c.get("/store")
    assert r.status_code == 403 and "isn't approved for store mode" in r.text
    assert "STORE_MODE" not in r.text and r.headers["x-robots-tag"].startswith("noindex")
    assert emails == [("", "")]
    c.get("/store")                                      # one email per address an hour
    assert emails == [("", "")]


def test_asking_for_access_needs_a_name(main_module, emails):
    c = TestClient(main_module.app)
    assert c.post("/api/store/request", json={"name": "  "}).status_code == 400
    assert c.post("/api/store/request", json={"name": "x" * 41}).status_code == 400
    assert emails == []


def test_the_emailed_code_approves_the_screen_under_its_name(main_module, emails, admin_client):
    c = TestClient(main_module.app)
    r = _approve(c, emails)
    assert r.status_code == 200 and r.json()["name"] == "Front counter iMac"
    assert main_module.STORE_SCREEN_COOKIE in c.cookies
    page = c.get("/store")
    assert page.status_code == 200 and "window.STORE_MODE = true" in page.text and "/static/store-mode.js" in page.text
    screens = admin_client.get("/api/admin/store-screens").json()["screens"]
    assert [s["name"] for s in screens] == ["Front counter iMac"] and screens[0]["last_used"]


def test_wrong_codes_run_out(main_module, emails):
    c = TestClient(main_module.app)
    challenge = c.post("/api/store/request", json={"name": "Kiosk"}).json()["challenge"]
    wrong = "000000" if emails[-1][1] != "000000" else "111111"
    for _ in range(main_module.LOGIN_CODE_TRIES - 1):
        assert c.post("/api/store/verify", json={"challenge": challenge, "code": wrong}).json()["error"] == "Incorrect code"
    last = c.post("/api/store/verify", json={"challenge": challenge, "code": wrong})
    assert last.status_code == 401 and last.json()["restart"]
    # Even the right code is no good now
    assert c.post("/api/store/verify", json={"challenge": challenge, "code": emails[-1][1]}).status_code == 401
    assert main_module.STORE_SCREEN_COOKIE not in c.cookies


def test_asking_for_codes_is_limited(main_module, emails):
    c = TestClient(main_module.app)
    codes = [c.post("/api/store/request", json={"name": "Kiosk"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429] and len(emails) == 3


def test_the_owner_signed_in_to_the_studio_can_look_at_store_mode(admin_client, emails):
    r = admin_client.get("/store")
    assert r.status_code == 200 and "window.STORE_MODE = true" in r.text
    assert emails == []


def test_the_website_itself_isnt_store_mode(client):
    r = client.get("/")
    assert "window.STORE_MODE = true" not in r.text and "/static/store-mode.js" not in r.text
    assert "<!--store-mode-->" in r.text


def test_renaming_and_removing_a_screen(main_module, emails, admin_client):
    c = TestClient(main_module.app)
    _approve(c, emails)
    sid = admin_client.get("/api/admin/store-screens").json()["screens"][0]["id"]
    assert admin_client.put(f"/api/admin/store-screens/{sid}", json={"name": "Back wall"}).status_code == 200
    assert admin_client.get("/api/admin/store-screens").json()["screens"][0]["name"] == "Back wall"
    assert admin_client.delete(f"/api/admin/store-screens/{sid}").status_code == 200
    assert c.get("/store").status_code == 403                # locked out again
    assert TestClient(main_module.app).get("/api/admin/store-screens").status_code == 401


def test_a_store_screen_sees_published_photos_without_watermarks(main_module, emails, photos, monkeypatch):
    c = TestClient(main_module.app)
    _approve(c, emails)
    marked = []
    real = main_module.apply_watermark
    monkeypatch.setattr(main_module, "apply_watermark", lambda img, s="medium": marked.append(s) or real(img, s))
    r = c.get("/api/photo", params={"path": PUB, "size": "medium", "nowm": 1})
    assert r.status_code == 200 and marked == [] and r.headers["cache-control"].startswith("private")
    assert max(Image.open(BytesIO(r.content)).size) <= 1800          # never the full-size file
    assert c.get("/api/photo", params={"path": PUB, "size": "full", "nowm": 1}).status_code == 200 and marked == []
    # Still only what the website shows: no unpublished or Eagle Mount photos
    for path in (DRAFT, EM):
        assert c.get("/api/photo", params={"path": path, "size": "medium", "nowm": 1}).status_code == 404


def test_visitors_asking_for_no_watermark_still_get_one(main_module, client, photos, monkeypatch):
    marked = []
    real = main_module.apply_watermark
    monkeypatch.setattr(main_module, "apply_watermark", lambda img, s="medium": marked.append(s) or real(img, s))
    assert client.get("/api/photo", params={"path": PUB, "size": "medium", "nowm": 1}).status_code == 200
    assert marked == ["medium"]


def test_store_screens_flip_through_photos_without_the_visitor_cap(main_module, emails, photos, monkeypatch):
    c = TestClient(main_module.app)
    _approve(c, emails)
    monkeypatch.setattr(main_module._preview_device_limiter, "max_calls", 1)
    for _ in range(3):
        assert c.get("/api/photo", params={"path": PUB, "size": "medium", "nowm": 1}).status_code == 200


def test_a_store_screen_cant_use_the_pos(main_module, emails, monkeypatch):
    monkeypatch.setattr(main_module, "POS_DEVICE_CODES_ON", True)
    c = TestClient(main_module.app)
    _approve(c, emails)
    assert not main_module._pos_device_approved(_request_with(c, main_module))
    assert c.get("/api/pos-dates").status_code in (401, 403)
    assert c.get("/api/admin/store-screens").status_code == 401


def _request_with(c, main_module):
    from starlette.requests import Request
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    return Request({"type": "http", "headers": [(b"cookie", cookie.encode())], "client": ("1.2.3.4", 1)})


# ── Send to sale ──────────────────────────────────────────────────────────────

def _favorites(n=2):
    return {"ts": 1, "frameCart": [], "currentFolderKey": "2026-12-06||Big Sky Ski School", "digitalLogoPaths": [],
            "folderCarts": {"2026-12-06||Big Sky Ski School": {
                "selectedPaths": [f"images/2026-12-06/big-sky-ski-school/DSC_{i}.JPG" for i in range(n)],
                "selectedPhotos": [], "printCart": [], "label": "Big Sky Ski School", "location": "Big Sky Ski School"}}}


@pytest.fixture()
def sends(main_module):
    def clear():
        for o in S3.list_objects_v2(Bucket=BUCKET, Prefix=main_module.STORE_SENDS_PREFIX).get("Contents", []):
            S3.delete_object(Bucket=BUCKET, Key=o["Key"])
        main_module._store_sends.clear()
        main_module._store_send_limiter._calls.clear()
    clear()
    S3.put_object(Bucket=BUCKET, Key="meta/photographers.json",
                  Body=json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    yield
    clear()


PIN = {"X-POS-PIN": "4821"}


def test_only_a_store_screen_can_send_favorites(client, sends):
    r = client.post("/api/store/send", json={"cart": _favorites()})
    assert r.status_code == 403


def test_sending_gives_a_four_letter_code_that_new_hearts_follow(main_module, emails, sends, client):
    c = TestClient(main_module.app)
    _approve(c, emails)
    assert c.post("/api/store/send", json={"cart": _favorites(0)}).status_code == 400     # nothing hearted yet
    r = c.post("/api/store/send", json={"cart": _favorites(2)})
    code = r.json()["code"]
    assert r.status_code == 200 and len(code) == 4 and code.isalpha() and code.isupper()
    assert not set(code) & {"I", "O"} and r.json()["status"] == "waiting"
    # More hearts: the same code, now with three photos
    assert c.post("/api/store/send", json={"cart": _favorites(3), "code": code}).json()["code"] == code
    d = client.get("/api/pos/store-code", params={"code": code.lower()}, headers=PIN).json()
    assert d["store_code"] == code and d["screen_name"] == "Front counter iMac" and d["email"] == ""
    f = d["folders"][0]
    assert (f["date"], f["location"]) == ("2026-12-06", "Big Sky Ski School") and len(f["photos"]) == 3
    # The screen sees the counter opened it
    assert c.get(f"/api/store/send/{code}").json()["status"] == "opened"


def test_the_pos_needs_its_pin_and_a_real_code(main_module, emails, sends, client):
    c = TestClient(main_module.app)
    _approve(c, emails)
    code = c.post("/api/store/send", json={"cart": _favorites()}).json()["code"]
    assert client.get("/api/pos/store-code", params={"code": code}).status_code == 401
    other = "ABCD" if code != "ABCD" else "ABCE"
    assert client.get("/api/pos/store-code", params={"code": other}, headers=PIN).status_code == 404
    # The store screen itself can't open it at the POS
    assert c.get("/api/pos/store-code", params={"code": code}).status_code == 401


def test_another_screen_cant_follow_or_change_the_code(main_module, emails, sends):
    a, b = TestClient(main_module.app), TestClient(main_module.app)
    _approve(a, emails, "Screen A")
    _approve(b, emails, "Screen B")
    code = a.post("/api/store/send", json={"cart": _favorites(2)}).json()["code"]
    assert b.get(f"/api/store/send/{code}").status_code == 404
    other = b.post("/api/store/send", json={"cart": _favorites(1), "code": code}).json()["code"]
    assert other != code
    assert len(main_module._load_store_send(code)["summary"]["paths"]) == 2


def test_the_sale_marks_the_code_sold_and_the_screen_starts_over(main_module, emails, sends, client):
    c = TestClient(main_module.app)
    _approve(c, emails)
    code = c.post("/api/store/send", json={"cart": _favorites()}).json()["code"]
    r = client.post("/api/pos-checkout", json={"pin": "4821", "email": "kim@example.com", "name": "Kim Lee",
                                               "items": [], "total": 0, "square_payment_id": "COMP",
                                               "store_code": code})
    assert r.status_code == 200
    assert c.get(f"/api/store/send/{code}").json()["status"] == "sold"
    # A heart a moment after the sale doesn't start a new code: the screen thanks them instead
    assert c.post("/api/store/send", json={"cart": _favorites(3), "code": code}).json() == {"code": code, "status": "sold"}
    rec = main_module._load_store_send(code)
    assert rec["order_id"].startswith("pos-") and len(rec["summary"]["paths"]) == 2


def test_clearing_the_favorites_leaves_the_code_with_nothing_to_sell(main_module, emails, sends, client):
    c = TestClient(main_module.app)
    _approve(c, emails)
    code = c.post("/api/store/send", json={"cart": _favorites()}).json()["code"]
    assert c.post("/api/store/send", json={"cart": _favorites(0), "code": code}).json()["status"] == "cleared"
    assert client.get("/api/pos/store-code", params={"code": code}, headers=PIN).json()["summary"]["items"] == 0


def test_codes_last_until_the_end_of_the_day(main_module, emails, sends, client):
    c = TestClient(main_module.app)
    _approve(c, emails)
    code = c.post("/api/store/send", json={"cart": _favorites()}).json()["code"]
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    end = datetime.fromisoformat(main_module._load_store_send(code)["expires"]).astimezone(ZoneInfo("America/Denver"))
    now = datetime.now(ZoneInfo("America/Denver"))
    assert (end.hour, end.minute) == (0, 0) and end.date() == now.date() + timedelta(days=1)
    # Tomorrow it's gone (also after a restart, from storage)
    rec = main_module._load_store_send(code)
    main_module._save_store_send({**rec, "expires": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    main_module._store_sends.clear()
    assert client.get("/api/pos/store-code", params={"code": code}, headers=PIN).status_code == 404
    assert c.get(f"/api/store/send/{code}").status_code == 404


def test_the_promo_number_is_the_one_checkout_uses(admin_client, main_module, monkeypatch):
    # The page says what the sale does: change the setting and the promo follows
    assert 'window.STORE_PROMO = {"print_off": 50}' in admin_client.get("/store").text
    monkeypatch.setattr(main_module, "PRINT_WITH_DIGITAL_PCT", 40)
    assert 'window.STORE_PROMO = {"print_off": 40}' in admin_client.get("/store").text
