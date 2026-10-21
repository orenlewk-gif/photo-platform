"""Unlimited Action Photo Packages: selling one on the POS (step 2)."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import pytest

from test_square import (CUSTOMER, PIN, PIN_HDR, STATE, FakeSquareAndResend, _orders, _paid_order,  # noqa: F401
                         no_pending, no_pos_orders, square)

S3 = boto3.client("s3", region_name="us-east-1")
MT = ZoneInfo("America/Denver")


def _pkg_item(tier="unl-5", people=4, price=275.0, **extra):
    return {"category": "unlimited_package", "label": "5-Day Unlimited Action Photos", "qty": 1,
            "price": price, "subtotal": price, "package_tier_id": tier, "package_people": people,
            "package_days": 5, **extra}


def _sale(items, total, square_id="COMP"):
    return {"pin": PIN, "email": "ann@example.com", "name": "Ann Smith", "phone": "406-555-0101",
            "items": items, "total": total, "square_payment_id": square_id, "customer": CUSTOMER}


def _json(key):
    return json.loads(S3.get_object(Bucket="test-bucket", Key=key)["Body"].read())


@pytest.fixture(autouse=True)
def default_prices(main_module):
    S3.delete_object(Bucket="test-bucket", Key="meta/unlimited_packages.json")
    main_module._unlimited_doc.cache["data"] = None
    for o in S3.list_objects_v2(Bucket="test-bucket", Prefix="unlimited_sales/").get("Contents", []):
        S3.delete_object(Bucket="test-bucket", Key=o["Key"])
    yield
    S3.delete_object(Bucket="test-bucket", Key="meta/unlimited_packages.json")
    main_module._unlimited_doc.cache["data"] = None


def test_selling_a_package_creates_an_empty_growing_link(client, square, no_pos_orders):
    square(FakeSquareAndResend())
    r = client.post("/api/pos-checkout", json=_sale([_pkg_item(people=6, price=335.0)], 335.0))
    assert r.status_code == 200, r.text
    d = r.json()
    token = d["token"]
    assert d["download_url"].endswith(f"/package/{token}")

    today = datetime.now(MT).date()
    last_day = today + timedelta(days=4)                      # 5 days: today + the next 4
    pkg = _json(f"packages/{token}/meta.json")
    assert pkg["paths"] == [] and pkg["emails"] == ["ann@example.com"]
    assert pkg["unlimited"]["start"] == today.isoformat() and pkg["unlimited"]["last_day"] == last_day.isoformat()
    expires = datetime.fromisoformat(pkg["expires"]).astimezone(MT)
    assert expires.date() == last_day + timedelta(days=30) and (expires.hour, expires.minute) == (23, 59)

    record = _json(f"unlimited_sales/{token}.json")
    assert record["people"] == 6 and record["tier_id"] == "unl-5" and record["days"] == 5
    assert record["name"] == "Ann Smith" and record["phone"] == "406-555-0101" and record["status"] == "open"
    assert record["order_id"] == d["order_id"]

    [order] = _orders()
    assert order["package_token"] == token and order["unlimited"]["status"] == "open"
    assert order["filenames"] == ""                            # no photographer credited yet


@pytest.mark.parametrize("items,msg", [
    ([_pkg_item(price=199.0)], "prices changed"),
    ([_pkg_item(people=5, price=275.0)], "prices changed"),    # 5th person is $30
    ([_pkg_item(tier="unl-99")], "no longer offered"),
    ([_pkg_item(people=0)], "Number of people"),
    ([_pkg_item(), _pkg_item()], "one Unlimited package"),
    ([_pkg_item(), {"category": "action_photos", "price": 25, "qty": 1,
                    "photo_paths": ["images/2026-06-12/base/IMG_1.jpg"]}], "on its own"),
])
def test_a_wrong_package_is_refused_before_square_charges(client, square, no_pending, items, msg):
    square(FakeSquareAndResend())
    sale = {k: v for k, v in _sale(items, 300.0).items() if k not in ("pin", "square_payment_id")}
    r = client.post("/api/pos/square-pending", json={"state": STATE, "sale": sale}, headers=PIN_HDR)
    assert r.status_code == 400 and msg in r.json()["error"]


def test_staff_can_set_a_custom_price(client, square, no_pending, no_pos_orders):
    square(FakeSquareAndResend())
    item = _pkg_item(price=240.0, package_custom_price=True)
    sale = {k: v for k, v in _sale([item], 240.0).items() if k not in ("pin", "square_payment_id")}
    assert client.post("/api/pos/square-pending", json={"state": STATE, "sale": sale},
                       headers=PIN_HDR).status_code == 200
    token = client.post("/api/pos-checkout", json=_sale([item], 240.0)).json()["token"]
    record = _json(f"unlimited_sales/{token}.json")
    assert record["price"] == 240.0 and record["custom_price"] is True
    assert client.post("/api/pos-checkout", json=_sale(
        [_pkg_item(price=-5.0, package_custom_price=True)], 0)).status_code == 400


def test_a_comp_package_is_checked_too(client, square, no_pos_orders):
    square(FakeSquareAndResend())
    r = client.post("/api/pos-checkout", json=_sale([_pkg_item(price=1.0)], 1.0))
    assert r.status_code == 400 and _orders() == []


def test_a_paid_package_goes_through_even_if_the_price_was_edited_meanwhile(client, square, no_pos_orders,
                                                                             no_pending, owner_client):
    fake = square(FakeSquareAndResend())
    fake.orders = {"ORDER1": _paid_order(cents=28600)}        # $275 + 4% tax
    sale = {k: v for k, v in _sale([_pkg_item()], 275.0).items() if k not in ("pin", "square_payment_id")}
    assert client.post("/api/pos/square-pending", json={"state": STATE, "sale": sale},
                       headers=PIN_HDR).status_code == 200
    settings = client.get("/api/unlimited-packages").json()
    settings["tiers"][1]["price"] = 300                       # owner reprices while Square is open
    assert owner_client.post("/api/admin/unlimited-packages", json=settings).status_code == 200

    r = client.post("/api/pos/square-complete", json={"state": STATE, "transaction_id": "ORDER1"})
    assert r.status_code == 200, r.text
    [order] = _orders()
    assert order["square_verified"] and order["unlimited"]["tier_id"] == "unl-5"


def test_receipt_explains_the_package_link(client, square, no_pos_orders, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    fake = square(FakeSquareAndResend())
    token = client.post("/api/pos-checkout", json=_sale([_pkg_item()], 275.0)).json()["token"]
    [email] = fake.emails
    assert f"/package/{token}" in email["html"] and "Open Your Photo Page" in email["html"]
    assert "add your action photos to this page during your trip" in email["html"]


def test_package_page_says_photos_are_coming(client, square, no_pos_orders):
    square(FakeSquareAndResend())
    token = client.post("/api/pos-checkout", json=_sale([_pkg_item()], 275.0)).json()["token"]
    page = client.get(f"/package/{token}")
    assert page.status_code == 200
    assert "during your trip" in page.text and "No photos yet" in page.text
    assert '<div class="acts" style="display:none">' in page.text   # nothing to download yet


def test_ordinary_package_page_is_unchanged(client, square, no_pos_orders):
    square(FakeSquareAndResend())
    r = client.post("/api/pos-checkout", json=_sale(
        [{"category": "action_photos", "label": "Action Photos", "qty": 1, "price": 25.0, "subtotal": 25.0,
          "photo_paths": ["images/2026-06-12/base/IMG_1.jpg"], "filenames": ["IMG_1.jpg"]}], 25.0))
    page = client.get(f"/package/{r.json()['token']}").text
    assert '<div class="acts">' in page and "No photos yet" not in page and 'class="note"' not in page


def test_resend_keeps_the_package_wording(client, square, no_pos_orders, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    fake = square(FakeSquareAndResend())
    d = client.post("/api/pos-checkout", json=_sale([_pkg_item()], 275.0)).json()
    [order] = _orders()
    r = client.post("/api/pos-resend", json={"pin": PIN, "order_id": d["order_id"], "date": order["date"]})
    assert r.status_code == 200
    assert "Open Your Photo Page" in fake.emails[-1]["html"]


def test_dates_follow_big_sky_time(main_module):
    # 11pm in Big Sky on Dec 1 is already Dec 2 in UTC: the package still starts Dec 1
    now = datetime(2026, 12, 2, 6, 0, tzinfo=timezone.utc)
    d = main_module._unlimited_dates(3, 30, now)
    assert d["start"] == "2026-12-01" and d["last_day"] == "2026-12-03"
    assert datetime.fromisoformat(d["expires"]).astimezone(MT).date().isoformat() == "2027-01-02"
