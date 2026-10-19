"""Private data and admin actions require the right credentials."""
import json

import boto3
import pytest

PIN = "4821"
CUSTOMERS = [
    {"first": "Ann",  "last": "Smith",  "email": "ann@example.com",  "phone": "555-0101"},
    {"first": "Bob",  "last": "Jones",  "email": "bob@example.com",  "phone": "555-0102"},
    {"first": "Cara", "last": "Smythe", "email": "cara@example.com", "phone": "555-0103"},
]


def _put_json(key, payload):
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket="test-bucket", Key=key, Body=json.dumps(payload).encode())


@pytest.fixture(autouse=True)
def seed(main_module):
    _put_json("meta/photographers.json", [{"id": "p1", "name": "Test Photog", "pin": PIN}])
    _put_json("pos_data/customers.json", CUSTOMERS)
    _put_json("meta/clock_records.json", [{"id": "rec1", "photographer_id": "p1",
                                            "photographer_name": "Test Photog",
                                            "clock_in": "2026-10-01T09:00:00+00:00",
                                            "clock_out": None, "date": "2026-10-01"}])
    _put_json("meta/pending_meta.json", [])
    main_module._customers_cache["data"] = None
    getattr(main_module, "_pin_fail_limiter", None) and main_module._pin_fail_limiter._calls.clear()
    yield
    getattr(main_module, "_pin_fail_limiter", None) and main_module._pin_fail_limiter._calls.clear()


PIN_HDR = {"X-POS-PIN": PIN}


# ── Customer directory ───────────────────────────────────────────────────────

def test_customers_blocked_without_credentials(client):
    assert client.get("/api/pos-customers").status_code == 401
    assert client.get("/api/pos-customers?q=smith").status_code == 401


def test_customers_wrong_pin_blocked(client):
    assert client.get("/api/pos-customers?q=smith", headers={"X-POS-PIN": "0000"}).status_code == 401


def test_pos_pin_can_search_by_name(client):
    r = client.get("/api/pos-customers?q=sm", headers=PIN_HDR)
    assert r.status_code == 200
    assert {c["last"] for c in r.json()["customers"]} == {"Smith", "Smythe"}


def test_pos_pin_cannot_browse_whole_directory(client):
    r = client.get("/api/pos-customers", headers=PIN_HDR)
    assert r.status_code == 200
    assert r.json()["customers"] == [] and r.json()["search_required"] is True
    assert client.get("/api/pos-customers?q=s", headers=PIN_HDR).json()["customers"] == []


def test_admin_can_browse_whole_directory(admin_client):
    r = admin_client.get("/api/pos-customers")
    assert r.status_code == 200
    assert r.json()["total"] == 3


def test_replace_whole_directory_endpoint_is_gone(client, admin_client):
    """Nothing can overwrite all customers in one call any more."""
    new = [{"first": "X", "last": "Y"}]
    assert client.post("/api/pos-customers", json=new, headers=PIN_HDR).status_code == 405
    assert admin_client.post("/api/pos-customers", json=new).status_code == 405


def test_recent_sales_requires_pin_or_admin(client, admin_client):
    assert client.get("/api/pos-recent-sales").status_code == 401
    assert client.get("/api/pos-recent-sales", headers=PIN_HDR).status_code == 200
    assert admin_client.get("/api/pos-recent-sales").status_code == 200


# ── PIN brute-force lockout ──────────────────────────────────────────────────

def test_ten_wrong_pins_locks_out_even_correct_pin(client):
    for _ in range(10):
        client.post("/api/photographer/auth", json={"pin": "0000"})
    r = client.post("/api/photographer/auth", json={"pin": PIN})
    assert r.status_code == 429
    assert client.get("/api/pos-customers?q=sm", headers=PIN_HDR).status_code == 401


def test_correct_pin_still_logs_in(client):
    r = client.post("/api/photographer/auth", json={"pin": PIN})
    assert r.status_code == 200
    assert r.json()["photographer"]["name"] == "Test Photog"
    assert "pin" not in r.json()["photographer"]


# ── Timecards ────────────────────────────────────────────────────────────────

def test_timecard_edit_delete_require_admin_and_the_owner_pin(client, admin_client, owner_client):
    assert client.put("/api/clock/record/rec1", json={"location": "x"}).status_code == 401
    assert client.delete("/api/clock/record/rec1").status_code == 401
    assert admin_client.put("/api/clock/record/rec1", json={"location": "x"}).status_code == 403
    assert owner_client.put("/api/clock/record/rec1", json={"location": "Base"}).status_code == 200
    assert owner_client.delete("/api/clock/record/rec1").status_code == 200


def test_clock_in_out_still_work_with_pin(client):
    assert client.post("/api/clock/in", json={"pin": PIN}).status_code == 400  # already clocked in
    assert client.post("/api/clock/out", json={"pin": PIN}).status_code == 200


# ── Removed pages stay removed; the pages we kept still load ─────────────────

@pytest.mark.parametrize("method,url", [
    ("get", "/upload"), ("post", "/api/upload/presign"), ("post", "/api/upload/confirm"),
    ("get", "/admin/cull"), ("get", "/api/pending"), ("get", "/api/pending/dates"),
    ("post", "/api/cull/organize"), ("post", "/api/cull/rename-folder"),
    ("post", "/api/cull/reject"), ("post", "/api/cull/golive"),
    ("get", "/admin/downloads"), ("get", "/api/uploads"), ("get", "/api/uploads/zip-all"),
    ("delete", "/api/uploads/batch/b1"), ("get", "/admin/timecards"),
    ("get", "/api/zip-compare"), ("get", "/api/families"), ("post", "/api/admin/recolor-all"),
])
def test_removed_pages_are_gone(admin_client, method, url):
    assert getattr(admin_client, method)(url).status_code in (404, 405)


@pytest.mark.parametrize("url", ["/clockin", "/admin/trash", "/admin/photographers", "/admin/indexing",
                                 "/admin/dashboard", "/admin/pricing", "/pos"])
def test_kept_pages_still_load(admin_client, url):
    assert admin_client.get(url).status_code == 200


def test_old_zip_pricing_links_still_lead_to_pricing(admin_client):
    for url in ("/admin/zip-pricing", "/admin/zip-folders", "/admin/zip-compare", "/admin/zip-preview/2026-06-12/Smith"):
        r = admin_client.get(url, follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/admin/pricing#zip"


def test_dashboard_upload_locations_still_served(client):
    assert client.get("/api/upload/locations").status_code == 200


# ── Login page: no outside text, no sending you to another site ──────────────

@pytest.fixture()
def no_codes(main_module, monkeypatch):
    """Sign-in without the emailed code (as from an approved device)."""
    monkeypatch.setattr(main_module, "ADMIN_LOGIN_CODES_ON", False)
    main_module._admin_pw_fail_limiter._calls.clear()


@pytest.mark.parametrize("next_url", [
    "https://evil.example/phish", "//evil.example", "/\\evil.example", "http:/evil.example",
    "javascript:alert(1)", "/admin\r\nSet-Cookie: x=1", "",
])
def test_sign_in_only_returns_to_pages_on_this_site(client, no_codes, next_url):
    r = client.post("/admin/login", data={"password": "test-admin-password", "next": next_url},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/dashboard"


def test_sign_in_returns_to_the_page_you_were_on(client, no_codes):
    r = client.post("/admin/login", data={"password": "test-admin-password", "next": "/admin/orders?days=7"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/orders?days=7"


def test_login_page_never_shows_text_from_the_link(client):
    evil = '/admin/x"><script>alert(1)</script>'
    page = client.get("/admin", params={"next": evil}).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    wrong = client.post("/admin/login", data={"password": "nope", "next": evil}).text
    assert "<script>alert(1)</script>" not in wrong


def test_links_page_only_shows_a_real_package_id(admin_client):
    page = admin_client.get("/admin/links", params={"new": "x';alert(1);//"}).text
    assert "alert(1)" not in page
    page = admin_client.get("/admin/links", params={"new": "a1b2c3d4e5f6a7b8"}).text
    assert "/package/a1b2c3d4e5f6a7b8" in page


# ── Owner PIN: only Oren's PIN, checked by the server ────────────────────────

LOCKED = [
    ("post", "/api/admin/print-pricing", {}),
    ("post", "/api/admin/portrait-pricing", {}), ("post", "/api/admin/portrait-locations", {"locations": []}),
    ("post", "/api/admin/private-session-pricing", {}), ("post", "/api/admin/pos-products", {}),
    ("post", "/api/admin/items-bundles", {}), ("post", "/api/admin/frames/prices", []),
    ("post", "/api/zip-pricing", {}),
    ("get", "/api/photographers", None), ("post", "/api/photographers", {"name": "X", "pin": "9999"}),
    ("put", "/api/photographers/p1", {"name": "X"}), ("delete", "/api/photographers/p1", None),
    ("get", "/api/photographers/commission", None), ("get", "/api/clock/records", None),
    ("post", "/api/clock/record", {}), ("put", "/api/clock/record/r1", {}), ("delete", "/api/clock/record/r1", None),
]


@pytest.fixture()
def owner_on_file(main_module):
    S3 = __import__("boto3").client("s3", region_name="us-east-1")
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json", Body=__import__("json").dumps([
        {"id": "p1", "name": "Oren Lewkowski", "pin": "7777"},
        {"id": "p2", "name": "Dana Shooter", "pin": "2222"}]).encode())
    main_module._pin_fail_limiter._calls.clear()


@pytest.mark.parametrize("method,url,body", LOCKED)
def test_prices_and_photographers_need_the_owner_pin(admin_client, owner_on_file, method, url, body):
    kwargs = {"json": body} if body is not None else {}
    r = getattr(admin_client, method)(url, **kwargs)
    assert r.status_code == 403 and r.json()["pin_required"] is True


def test_only_the_owners_pin_unlocks_and_then_it_works(admin_client, client, owner_on_file):
    assert client.post("/api/admin/owner-unlock", json={"pin": "7777"}).status_code == 401      # not signed in
    assert admin_client.post("/api/admin/owner-unlock", json={"pin": "2222"}).status_code == 401  # a photographer's PIN
    assert admin_client.get("/api/admin/owner-status").json() == {"unlocked": False}
    r = admin_client.post("/api/admin/owner-unlock", json={"pin": "7777"})
    assert r.status_code == 200
    assert admin_client.get("/api/admin/owner-status").json() == {"unlocked": True}
    assert admin_client.get("/api/photographers").status_code == 200                          # now allowed


def test_owner_unlock_runs_out(admin_client, owner_on_file, main_module):
    import time
    admin_client.cookies.set(main_module.OWNER_COOKIE, main_module._owner_token(int(time.time()) - 1))
    assert admin_client.get("/api/photographers").status_code == 403


def test_a_made_up_unlock_cookie_does_nothing(admin_client, owner_on_file, main_module):
    import time
    admin_client.cookies.set(main_module.OWNER_COOKIE, f"{int(time.time()) + 600}.forged")
    assert admin_client.get("/api/photographers").status_code == 403


def test_wrong_owner_pins_lock_out(admin_client, owner_on_file):
    for _ in range(10):
        admin_client.post("/api/admin/owner-unlock", json={"pin": "0000"})
    assert admin_client.post("/api/admin/owner-unlock", json={"pin": "7777"}).status_code == 429


def test_public_price_lists_stay_open(client):
    for url in ("/api/pricing?location=Winter%20Action", "/api/print-pricing", "/api/pos-products",
                "/api/portrait-pricing", "/api/private-session-pricing"):
        assert client.get(url).status_code == 200, url


def test_signing_out_also_locks_the_owner_pages(owner_client, main_module):
    r = owner_client.get("/admin/logout", follow_redirects=False)
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith(main_module.OWNER_COOKIE + "=")]
    assert cleared and ("Max-Age=0" in cleared[0] or "expires=" in cleared[0].lower())



def test_activities_page_saves_need_only_the_login(client, admin_client):
    # The Activities page has no PIN screen (Oren's choice), so its two saves stay login-only
    assert client.post("/api/admin/pricing", json={}).status_code == 401
    pricing = admin_client.get("/api/admin/pricing").json()
    assert admin_client.post("/api/admin/pricing", json=pricing).status_code == 200
    assert admin_client.post("/api/admin/activity-groups", json={}).status_code == 200



# ── Studio sign-in: sessions per device, emailed code on new devices ─────────

class _Mail:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, **kw):
        assert url == "https://api.resend.com/emails", url
        self.sent.append(json)

        class R:
            ok, status_code, text = True, 200, "{}"
        return R()


@pytest.fixture()
def mail(main_module, monkeypatch):
    m = _Mail()
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(main_module.http_requests, "post", m.post)
    monkeypatch.setattr(main_module, "ADMIN_LOGIN_CODES_ON", True)
    main_module._admin_pw_fail_limiter._calls.clear()
    main_module._login_challenges.clear()
    return m


def _code_from(mail):
    import re
    return re.search(r"code: (\d{6})", mail.sent[-1]["subject"]).group(1)


def _challenge(page):
    import re
    return re.search(r'name="challenge" value="([^"]+)"', page).group(1)


def _sign_in(client, next_url="/admin/orders"):
    return client.post("/admin/login", data={"password": "test-admin-password", "next": next_url},
                       follow_redirects=False)


def test_new_device_needs_the_emailed_code_then_only_the_password(client, mail, main_module):
    r = _sign_in(client)
    assert r.status_code == 200 and "emailed a 6-digit code" in r.text
    assert mail.sent[-1]["to"] == ["orenlewk@gmail.com"] and "new device" in mail.sent[-1]["html"]
    assert client.get("/api/admin/devices").status_code == 401            # not signed in yet

    r = client.post("/admin/verify", data={"challenge": _challenge(r.text), "code": _code_from(mail)},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/orders"
    devices = client.get("/api/admin/devices").json()["devices"]
    assert len(devices) == 1 and devices[0]["this_device"]

    client.get("/admin/logout")
    assert client.get("/api/admin/devices").status_code == 401            # signed out
    n = len(mail.sent)
    r = _sign_in(client)                                                   # same device: just the password
    assert r.status_code == 303 and len(mail.sent) == n


def test_wrong_codes_and_expired_codes(client, mail):
    page = _sign_in(client).text
    ch = _challenge(page)
    for _ in range(4):
        r = client.post("/admin/verify", data={"challenge": ch, "code": "000000"})
        assert r.status_code == 401 and "Incorrect code" in r.text
    r = client.post("/admin/verify", data={"challenge": ch, "code": "000000"})
    assert "Too many wrong codes" in r.text
    r = client.post("/admin/verify", data={"challenge": ch, "code": _code_from(mail)})
    assert "expired" in r.text                                             # that code is gone


def test_sign_out_everywhere_ends_every_session_and_forgets_devices(client, mail, main_module):
    from fastapi.testclient import TestClient
    page = _sign_in(client).text
    client.post("/admin/verify", data={"challenge": _challenge(page), "code": _code_from(mail)})
    other = TestClient(main_module.app)
    other.cookies.set(main_module.ADMIN_COOKIE, main_module._create_admin_session())
    assert other.get("/api/admin/devices").status_code == 200

    assert client.post("/api/admin/sign-out-everywhere").status_code == 200
    assert other.get("/api/admin/devices").status_code == 401              # the other device is out too
    r = _sign_in(client)
    assert "emailed a 6-digit code" in r.text                              # and needs a code again


def test_too_many_wrong_passwords_lock_out(client, mail):
    for _ in range(10):
        client.post("/admin/login", data={"password": "wrong"})
    r = client.post("/admin/login", data={"password": "test-admin-password"})
    assert r.status_code == 429


def test_the_old_fixed_cookie_no_longer_signs_in(client, main_module):
    import hashlib, hmac
    old = hmac.new(b"test-admin-password", b"test-admin-password:admin-session", hashlib.sha256).hexdigest()
    client.cookies.set(main_module.ADMIN_COOKIE, old)
    assert client.get("/api/admin/devices").status_code == 401


def test_if_the_code_email_fails_you_are_told(client, mail, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "")
    r = _sign_in(client)
    assert r.status_code == 503 and "Couldn't email the sign-in code" in r.text
