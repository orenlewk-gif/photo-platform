"""Nobody gets somewhere they shouldn't: every address refuses visitors without a sign-in unless
it's on the public list below, staff PINs and discount codes can't be guessed, and visitors only
see published photos (Eagle Mount photos only on Eagle Mount's page), with a cap on enlarged ones."""
import json
import re
from io import BytesIO

import boto3
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from PIL import Image

S3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "test-bucket"

# Everything a visitor may reach without signing in. A new address that answers visitors fails
# test_every_other_address_refuses_visitors until it's added here on purpose.
PUBLIC = {
    # Pages anyone can open (the studio, POS and clock-in pages are sign-in screens; their data needs a sign-in)
    ("GET", "/"), ("GET", "/checkout"), ("GET", "/admin"), ("GET", "/pos"), ("GET", "/pos/manifest.json"),
    ("GET", "/clockin"), ("GET", "/privacy"), ("GET", "/terms"), ("GET", "/refunds"), ("GET", "/photo-removal"),
    ("GET", "/healthz"), ("GET", "/healthz/storage"),
    # The public gallery: published photos only (Eagle Mount and unpublished folders are left out)
    ("GET", "/api/days"), ("GET", "/api/dates"), ("GET", "/api/locations"), ("GET", "/api/subfolders"),
    ("GET", "/api/browse"), ("GET", "/api/search"), ("GET", "/api/last-name-search"), ("GET", "/api/photo"),
    ("GET", "/api/folder-colors"), ("GET", "/api/folder-times"), ("GET", "/api/folder-dates"),
    ("GET", "/api/folder/info"), ("GET", "/api/folder/poses"),
    # Prices and shop settings the gallery, checkout and POS show
    ("GET", "/api/pricing"), ("GET", "/api/print-pricing"), ("GET", "/api/portrait-pricing"),
    ("GET", "/api/shop-prices"), ("GET", "/api/frames"), ("GET", "/api/frame-photos"), ("GET", "/api/seasons"),
    ("GET", "/api/private-session-pricing"), ("GET", "/api/items-bundles"), ("GET", "/api/unlimited-packages"),
    ("GET", "/api/pos-products"), ("GET", "/api/pos-theme"), ("GET", "/api/site-info"),
    ("GET", "/api/upload/locations"),
    # Checkout: prices are worked out on the server; codes are limited per visitor
    ("POST", "/api/checkout"), ("POST", "/api/checkout/quote"), ("GET", "/api/validate-coupon"),
    # Visitor forms (each checks its input and is limited per visitor)
    ("POST", "/api/saved-cart"), ("POST", "/api/saved-cart/load"), ("POST", "/api/photo-removal"),
    # The POS access-code screen (it only shows the PIN pad; everything after needs a PIN)
    ("POST", "/api/pos-unlock"),
    # Signed or secret-link addresses: the link, payment or WooCommerce signature is the key
    ("POST", "/webhook"), ("GET", "/api/saved-cart/link"), ("GET", "/api/pos/download-qr"),
    ("POST", "/api/pos/square-complete"), ("POST", "/api/pos/square-pending"), ("POST", "/api/pos/device-verify"),
    ("GET", "/download/{token}"), ("GET", "/download/{token}/all"), ("GET", "/download/{token}/{idx}"),
    ("GET", "/package/{token}"), ("GET", "/package/{token}/all"), ("GET", "/package/{token}/links"),
    ("GET", "/package/{token}/file/{idx}"), ("GET", "/package/{token}/medium/{idx}"),
    ("GET", "/package/{token}/thumb/{idx}"), ("GET", "/unsubscribe/{token}"), ("POST", "/unsubscribe/{token}"),
    # Partner (Eagle Mount) page and its sign-in; its photos need the partner's password (tested below)
    ("GET", "/{slug}"), ("POST", "/{slug}"),
    ("GET", "/partner/{slug}/thumb"), ("GET", "/partner/{slug}/view"), ("GET", "/partner/{slug}/file"),
}
# Every query value an address might need, so none stops at "missing field" before its sign-in check
_QUERY = {"date": "2026-06-07", "location": "x", "path": "images/2026-06-07/x/A.jpg", "job_id": "x",
          "pin": "0000", "q": "ab", "token": "x", "partner": "x", "email": "a@b.co", "code": "x"}


def _routes(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _routes(inner.routes)
        elif isinstance(r, APIRoute):
            yield r


def _refused(resp) -> bool:
    if resp.status_code in (401, 403):
        return True
    return resp.status_code in (302, 303, 307) and resp.headers.get("location", "").startswith("/admin")


def test_every_other_address_refuses_visitors(main_module):
    c = TestClient(main_module.app, raise_server_exceptions=False)
    seen, open_ = set(), []
    for route in _routes(main_module.app.routes):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            seen.add((method, route.path))
            if (method, route.path) in PUBLIC:
                continue
            url = re.sub(r"\{[^}]+:path\}", "images/2026-06-07/x/A.jpg", route.path)
            url = re.sub(r"\{[^}]+\}", "1", url)
            resp = c.request(method, url, params=_QUERY, json={}, follow_redirects=False)
            if not _refused(resp):
                open_.append(f"{method} {route.path} -> {resp.status_code}")
    assert not open_, "These answer visitors without a sign-in:\n" + "\n".join(open_)
    assert len(seen) > 200                                     # the walk really found the site's addresses
    assert not PUBLIC - seen, f"Public list names addresses that no longer exist: {PUBLIC - seen}"


# ── Staff PINs ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def photographers(main_module):
    S3.put_object(Bucket=BUCKET, Key="meta/photographers.json", Body=json.dumps([
        {"id": "p1", "name": "Test Photog", "pin": "4821"},
        {"id": "p2", "name": "No Pin Yet", "pin": ""}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    yield
    main_module._pin_fail_limiter._calls.clear()


PIN_CHECKS = [
    ("post", "/api/clock/in", lambda pin: {"json": {"pin": pin}}),
    ("post", "/api/clock/out", lambda pin: {"json": {"pin": pin}}),
    ("get", "/api/clock/status", lambda pin: {"params": {"pin": pin}}),
    ("post", "/api/photographer/auth", lambda pin: {"json": {"pin": pin}}),
    ("post", "/api/pos-checkout", lambda pin: {"json": {"pin": pin, "items": []}}),
    ("post", "/api/pos-resend", lambda pin: {"json": {"pin": pin}}),
]


@pytest.mark.parametrize("method,url,args", PIN_CHECKS, ids=[u for _, u, _ in PIN_CHECKS])
def test_wrong_pins_lock_out_every_pin_check(client, photographers, method, url, args):
    for i in range(10):
        r = getattr(client, method)(url, **args(f"{1000 + i}"))
        assert r.status_code == (429 if i == 9 else 401)       # the 10th wrong PIN says it's locked
    # Locked out: even the right PIN is refused until the 10 minutes pass
    r = getattr(client, method)(url, **args("4821"))
    assert r.status_code == 429


@pytest.mark.parametrize("method,url,args", PIN_CHECKS, ids=[u for _, u, _ in PIN_CHECKS])
def test_an_empty_pin_never_matches_a_photographer_without_one(client, photographers, method, url, args):
    assert getattr(client, method)(url, **args("")).status_code == 401


def test_the_right_pin_still_works(client, photographers):
    assert client.get("/api/clock/status", params={"pin": "4821"}).status_code == 200
    assert client.post("/api/photographer/auth", json={"pin": "4821"}).status_code == 200


# ── POS photo lists (they include Eagle Mount folders) ──────────────────────────

def test_pos_day_and_family_lists_need_a_signed_in_pos(main_module, client, admin_client, photographers):
    params = {"date": "2026-06-07", "location": "x"}
    assert client.get("/api/pos-dates").status_code == 401
    assert client.get("/api/pos-families", params=params).status_code == 401
    assert client.get("/api/pos-dates", headers={"X-POS-PIN": "4821"}).status_code == 200
    assert client.get("/api/pos-families", params=params, headers={"X-POS-PIN": "4821"}).status_code == 200
    assert admin_client.get("/api/pos-dates").status_code == 200
    pos = TestClient(main_module.app)
    pos.post("/api/photographer/auth", json={"pin": "4821"})    # the POS cookie, as on the iPad
    assert pos.get("/api/pos-dates").status_code == 200


# ── Discount codes ─────────────────────────────────────────────────────────────

@pytest.fixture()
def codes(main_module):
    main_module._coupon_fail_limiter._calls.clear()
    S3.put_object(Bucket=BUCKET, Key="meta/discount_codes.json", Body=json.dumps(
        [{"code": "REALCODE", "type": "percent", "amount": 10, "active": True}]).encode())
    yield
    S3.delete_object(Bucket=BUCKET, Key="meta/discount_codes.json")
    main_module._coupon_fail_limiter._calls.clear()


def test_guessing_discount_codes_is_stopped(client, codes, main_module):
    if not main_module._check_coupon("REALCODE")[0]:
        pytest.skip("discount code store isn't at meta/discount_codes.json")
    assert client.get("/api/validate-coupon", params={"code": "REALCODE"}).status_code == 200
    for i in range(15):
        assert client.get("/api/validate-coupon", params={"code": f"GUESS{i}"}).status_code == 404
    assert client.get("/api/validate-coupon", params={"code": "REALCODE"}).status_code == 429
    assert client.post("/api/checkout/quote", json={"coupon_code": "REALCODE"}).status_code == 429
    assert client.post("/api/checkout", json={"coupon_code": "REALCODE"}).status_code == 429
    # A cart without a code still works
    assert client.post("/api/checkout/quote", json={}).status_code == 200


def test_wrong_codes_at_checkout_count_too(client, codes):
    for i in range(15):
        client.post("/api/checkout/quote", json={"coupon_code": f"GUESS{i}"})
    assert client.get("/api/validate-coupon", params={"code": "REALCODE"}).status_code == 429


# ── Which photos visitors can see ──────────────────────────────────────────────

def _jpeg():
    buf = BytesIO()
    Image.new("RGB", (1600, 1200), (40, 90, 160)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


PUB = "images/2026-12-05/big-sky-ski-school/PUB_0001.JPG"
DRAFT = "images/2026-12-05/big-sky-ski-school/DRAFT_0001.JPG"
EM = "images/2026-12-05/big-sky-ski-school/EM_0001.JPG"


@pytest.fixture()
def gallery(main_module, live_photos):
    base = {"date": "2026-12-05", "location": "Big Sky Ski School", "group": ""}
    live_photos([{**base, "path": PUB, "last_name": "Jones"},
                 {**base, "path": DRAFT, "last_name": "Pulledback", "draft": True},
                 {**base, "path": EM, "last_name": "", "partner": "eagle-mount"}])
    for key in (PUB, DRAFT, EM):
        S3.put_object(Bucket=BUCKET, Key=key, Body=_jpeg())
    S3.put_object(Bucket=BUCKET, Key="meta/partners.json", Body=json.dumps([
        {"id": "eagle-mount", "name": "Eagle Mount", "slug": "eaglemount", "password": "eagle-test"}]).encode())
    main_module._partners_doc.cache["data"] = None
    main_module._preview_device_limiter._calls.clear()
    main_module._preview_ip_limiter._calls.clear()
    main_module._lastname_limiter._calls.clear()
    yield
    S3.delete_object(Bucket=BUCKET, Key="meta/partners.json")
    main_module._partners_doc.cache["data"] = None
    main_module._preview_device_limiter._calls.clear()
    main_module._preview_ip_limiter._calls.clear()


def test_visitors_see_published_photos_only(client, gallery):
    assert client.get("/api/photo", params={"path": PUB, "size": "medium"}).status_code == 200
    for path in (DRAFT, EM, "images/2026-12-05/big-sky-ski-school/NOT_IN_LIST.JPG"):
        for size in ("thumb", "medium", "full"):
            assert client.get("/api/photo", params={"path": path, "size": size},
                              follow_redirects=False).status_code == 404, (path, size)


def test_eagle_mount_photos_show_for_their_signed_in_visitors_never_cached_publicly(main_module, gallery):
    c = TestClient(main_module.app)
    assert c.post("/eaglemount", data={"password": "eagle-test"}, follow_redirects=False).status_code in (302, 303)
    r = c.get("/api/photo", params={"path": EM, "size": "medium"})
    assert r.status_code == 200 and r.headers["cache-control"].startswith("private")


def test_staff_see_every_photo(admin_client, gallery):
    for path in (DRAFT, EM):
        assert admin_client.get("/api/photo", params={"path": path, "size": "medium"}).status_code == 200


def test_visitors_get_at_most_lightbox_size(client, gallery, main_module, monkeypatch):
    sizes = []
    real = main_module.apply_watermark
    monkeypatch.setattr(main_module, "apply_watermark", lambda img, s="medium": sizes.append(s) or real(img, s))
    r = client.get("/api/photo", params={"path": PUB, "size": "full"})
    assert r.status_code == 200 and sizes == ["medium"] and max(Image.open(BytesIO(r.content)).size) <= 1800


def test_enlarged_previews_are_capped_per_visitor(client, gallery, main_module, monkeypatch):
    monkeypatch.setattr(main_module._preview_device_limiter, "max_calls", 3)
    for _ in range(3):
        assert client.get("/api/photo", params={"path": PUB, "size": "medium"}).status_code == 200
    assert client.get("/api/photo", params={"path": PUB, "size": "medium"}).status_code == 429
    # Grid thumbnails aren't capped
    assert client.get("/api/photo", params={"path": PUB, "size": "thumb"}, follow_redirects=False).status_code in (200, 302)
    # Another device on the same Wi-Fi has its own allowance
    other = client.get("/api/photo", params={"path": PUB, "size": "medium"}, headers={"User-Agent": "other phone"})
    assert other.status_code == 200


def test_the_shared_wifi_cap_still_stops_one_address(client, gallery, main_module, monkeypatch):
    monkeypatch.setattr(main_module._preview_ip_limiter, "max_calls", 4)
    codes = [client.get("/api/photo", params={"path": PUB, "size": "medium"},
                        headers={"User-Agent": f"browser {i}"}).status_code for i in range(5)]
    assert codes == [200, 200, 200, 200, 429]


def test_staff_previews_arent_capped(admin_client, gallery, main_module, monkeypatch):
    monkeypatch.setattr(main_module._preview_device_limiter, "max_calls", 1)
    for _ in range(3):
        assert admin_client.get("/api/photo", params={"path": PUB, "size": "medium"}).status_code == 200


def test_unpublished_folders_dont_show_up_in_name_search(client, gallery):
    assert client.get("/api/last-name-search", params={"q": "pulled"}).json()["results"] == []
    assert client.get("/api/last-name-search", params={"q": "jon"}).json()["results"]
    found = client.get("/api/search", params={"last_name": "Pulledback"}).json()
    assert found.get("photos", []) == []
    assert "2026-12-05" in client.get("/api/dates").json()["dates"]


def test_unpublished_days_dont_show_in_the_day_list(client, live_photos):
    live_photos([{"date": "2026-12-09", "location": "Big Sky Ski School", "group": "", "last_name": "X",
                  "path": "images/2026-12-09/big-sky-ski-school/D.JPG", "draft": True}])
    assert "2026-12-09" not in client.get("/api/dates").json()["dates"]


# ── Partner (Eagle Mount) photo addresses ──────────────────────────────────────

@pytest.mark.parametrize("method,what", [("get", "thumb"), ("get", "view"), ("get", "file"),
                                         ("post", "zip"), ("post", "links")])
def test_partner_photo_addresses_need_the_partner_password(client, gallery, method, what):
    r = client.request(method.upper(), f"/partner/eaglemount/{what}", params={"path": EM, "date": "2026-12-05"},
                       json={"paths": [EM]}, follow_redirects=False)
    assert r.status_code in (401, 403, 404)
    assert not r.headers.get("content-type", "").startswith(("image/", "application/zip"))
