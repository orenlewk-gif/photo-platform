"""Unlimited Action Photo Packages: the POS Packages list, adding photos, following photo moves,
and closing a package with the commission split by photographer (steps 3-5)."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
import pytest

from test_square import CUSTOMER, PIN, PIN_HDR, FakeSquareAndResend, no_pos_orders, square  # noqa: F401

S3 = boto3.client("s3", region_name="us-east-1")
MT = ZoneInfo("America/Denver")
TODAY = datetime.now(MT).date()


def _day(n):
    return (TODAY + timedelta(days=n)).isoformat()


def _photo(name, date=None, location="Big Sky Ski School"):
    date = date or _day(0)
    return {"path": f"images/{date}/ski-school/{name}", "filename": name, "date": date,
            "location": location, "last_name": "", "group": ""}


PHOTOS = [_photo("DSC_0001.JPG"), _photo("DSC_0002.JPG"), _photo("DSC_0003.JPG"),
          _photo("AMS_0001.JPG"), _photo("XYZ_0001.JPG", date=_day(-3)),   # earlier day, unknown code
          _photo("DSC_0100.JPG", location="Lone Peak Portraits"),          # a portrait
          _photo("DSC_0200.JPG", date=_day(9))]                             # after the last day


@pytest.fixture(autouse=True)
def setup(main_module, live_photos, square):
    square(FakeSquareAndResend())
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json", Body=json.dumps([
        {"id": "p1", "name": "Test Photog", "pin": PIN},
        {"id": "p2", "name": "Dana Shooter", "pin": "2222", "file_prefix": "dsc"},
        {"id": "p3", "name": "Alex Shooter", "pin": "3333", "file_prefix": "ams"},
        {"id": "p0", "name": "Oren Lewkowski", "pin": "4321"}]).encode())
    S3.delete_object(Bucket="test-bucket", Key="meta/unlimited_packages.json")
    main_module._unlimited_doc.cache["data"] = None
    for o in S3.list_objects_v2(Bucket="test-bucket", Prefix="unlimited_sales/").get("Contents", []):
        S3.delete_object(Bucket="test-bucket", Key=o["Key"])
    main_module._unlimited_records_cache["records"] = None
    live_photos(PHOTOS)
    yield
    main_module._unlimited_records_cache["records"] = None


def _sell(client, name="Ann Smith", phone="406-555-0101", tier="unl-3", days=3, price=205.0):
    item = {"category": "unlimited_package", "label": "3-Day", "qty": 1, "price": price, "subtotal": price,
            "package_tier_id": tier, "package_people": 4, "package_days": days,
            "package_custom_price": price != 205.0}
    r = client.post("/api/pos-checkout", json={
        "pin": PIN, "email": "ann@example.com", "name": name, "phone": phone, "items": [item],
        "total": price, "square_payment_id": "COMP", "customer": CUSTOMER})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _add(client, token, paths=(), remove=()):
    return client.post(f"/api/pos/unlimited/{token}/photos", json={"add": list(paths), "remove": list(remove)},
                       headers=PIN_HDR)


def _paths(*names):
    return [p["path"] for p in PHOTOS if p["filename"] in names]


def test_packages_list_needs_the_pin_and_finds_by_name_or_phone(client, no_pos_orders):
    _sell(client)
    _sell(client, name="Bo Jones", phone="(406) 555-7788")
    assert client.get("/api/pos/unlimited").status_code == 401
    names = lambda q: [p["name"] for p in client.get(f"/api/pos/unlimited?q={q}", headers=PIN_HDR).json()["packages"]]
    assert sorted(names("")) == ["Ann Smith", "Bo Jones"]
    assert names("jones") == ["Bo Jones"]
    assert names("5557788") == ["Bo Jones"]          # phone digits match however it was typed
    p = client.get("/api/pos/unlimited?q=ann", headers=PIN_HDR).json()["packages"][0]
    assert p["open"] and p["day_num"] == 1 and p["photo_count"] == 0 and p["adding_until"] == _day(2)


def test_adding_photos_shows_them_to_the_customer_right_away(client, no_pos_orders):
    token = _sell(client)
    r = _add(client, token, _paths("DSC_0002.JPG", "DSC_0001.JPG", "XYZ_0001.JPG"))
    assert r.status_code == 200 and r.json()["added"] == 3
    again = _add(client, token, _paths("DSC_0001.JPG", "AMS_0001.JPG")).json()   # no duplicates
    assert again["added"] == 1 and again["photo_count"] == 4
    detail = client.get(f"/api/pos/unlimited/{token}", headers=PIN_HDR).json()
    assert [p["filename"] for p in detail["photos"]][0] == "XYZ_0001.JPG"     # in day order
    page = client.get(f"/package/{token}").text
    assert "4 photos" in page and "No photos yet" not in page
    assert client.get(f"/package/{token}/thumb/0").status_code != 404


@pytest.mark.parametrize("name,msg", [("DSC_0100.JPG", "portrait"), ("DSC_0200.JPG", "after the package's last day")])
def test_photos_that_dont_belong_are_refused(client, no_pos_orders, name, msg):
    token = _sell(client)
    r = _add(client, token, _paths(name))
    assert r.status_code == 400 and msg in r.json()["error"]
    r = _add(client, token, ["images/made/up.jpg"])
    assert r.status_code == 400 and "published" in r.json()["error"]


def test_staff_can_remove_a_photo_added_by_mistake(client, no_pos_orders):
    token = _sell(client)
    _add(client, token, _paths("DSC_0001.JPG", "DSC_0002.JPG"))
    r = _add(client, token, remove=_paths("DSC_0002.JPG")).json()
    assert r["removed"] == 1 and r["photo_count"] == 1


def test_adding_needs_the_pin(client, no_pos_orders):
    token = _sell(client)
    assert client.post(f"/api/pos/unlimited/{token}/photos", json={"add": _paths("DSC_0001.JPG")}).status_code == 401


def test_adding_ends_with_the_last_day_then_closed(client, no_pos_orders, main_module, monkeypatch):
    token = _sell(client)                                      # 3 days: today .. today+2
    monkeypatch.setattr(main_module, "_mountain_today", lambda: _day(2))   # the last day
    assert _add(client, token, _paths("DSC_0001.JPG")).status_code == 200
    monkeypatch.setattr(main_module, "_mountain_today", lambda: _day(3))
    r = _add(client, token, _paths("DSC_0002.JPG"))
    assert r.status_code == 400 and "closed" in r.json()["error"]
    [p] = client.get("/api/pos/unlimited", headers=PIN_HDR).json()["packages"]
    assert not p["open"]


def test_moved_photos_stay_in_the_package(client, admin_client, no_pos_orders, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "_remap_unlimited_paths_soon", main_module._remap_unlimited_paths)
    token = _sell(client)
    for p in PHOTOS[:2]:
        S3.put_object(Bucket="test-bucket", Key=p["path"], Body=b"jpeg")
    _add(client, token, _paths("DSC_0001.JPG", "DSC_0002.JPG"))
    r = admin_client.post("/api/admin/folder/move", json={"date": _day(0), "location": "Big Sky Ski School",
                                                          "folder": "", "new_date": _day(0),
                                                          "new_location": "Madison Base"})
    assert r.status_code == 200 and r.json()["moved"] >= 2
    paths = json.loads(S3.get_object(Bucket="test-bucket", Key=f"packages/{token}/meta.json")["Body"].read())["paths"]
    assert all("/madison-base/" in p for p in paths) and len(paths) == 2


def test_closing_splits_the_sale_by_each_photographers_share(client, owner_client, admin_client,
                                                             no_pos_orders, main_module, monkeypatch):
    token = _sell(client, price=200.0, tier="unl-3")
    def no_woocommerce(*a, **k):
        raise ConnectionError("no WooCommerce in tests")
    monkeypatch.setattr(main_module.http_requests, "get", no_woocommerce)   # Orders page: in-person sales only
    # 2 Dana (dsc), 1 Alex (ams), 1 unknown code -> the owner
    _add(client, token, _paths("DSC_0001.JPG", "DSC_0002.JPG", "AMS_0001.JPG", "XYZ_0001.JPG"))

    comm = lambda: {c["name"]: c["revenue"] for c in owner_client.get(
        f"/api/photographers/commission?date_from={_day(-1)}&date_to={_day(1)}").json()["commission"]}
    d = owner_client.get(f"/api/photographers/commission?date_from={_day(-1)}&date_to={_day(1)}").json()
    assert d["unlimited_pending"] == 200.0 and d["unlimited_pending_count"] == 1
    assert comm()["Dana Shooter"] == 0                         # nothing credited while open
    assert "split when it closes" in admin_client.get("/admin/orders?days=7").text

    monkeypatch.setattr(main_module, "_mountain_today", lambda: _day(3))
    by_name = comm()
    assert (by_name["Dana Shooter"], by_name["Alex Shooter"], by_name["Oren Lewkowski"]) == (100.0, 50.0, 50.0)
    assert by_name["Test Photog"] == 0                         # the seller isn't credited
    record = json.loads(S3.get_object(Bucket="test-bucket", Key=f"unlimited_sales/{token}.json")["Body"].read())
    assert record["status"] == "closed" and record["split"] == {"dsc": 2, "ams": 1, "xyz": 1}
    html = admin_client.get("/admin/orders?days=7").text
    assert "Dana Shooter 50% &amp; Alex Shooter 25% &amp; Oren Lewkowski 25%" in html
    csv_line = next(l for l in admin_client.get("/admin/export?days=7").text.splitlines() if "Ann" in l)
    assert "Dana Shooter 50%" in csv_line


def test_a_package_closed_with_no_photos_goes_to_the_owner(main_module):
    photogs = [{"id": "p0", "name": "Oren Lewkowski"}, {"id": "p2", "name": "Dana", "file_prefix": "dsc"}]
    assert main_module._unlimited_shares({"status": "closed", "split": {}}, photogs) == [("Oren Lewkowski", "p0", 1.0)]
    assert main_module._unlimited_shares({"status": "open"}, photogs) is None
