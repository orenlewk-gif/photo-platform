"""Shipping labels (Shippo) on orders that ship: settings, rates, buying a label, the customer's
tracking email, marking shipped by hand, voiding, and the Orders page showing it all. Shippo
itself is replaced by a fake."""
import json

import boto3
import pytest

from core import shippo_api

S3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "test-bucket"
BOX = {"name": "8x10 mailer", "length": 12, "width": 10, "height": 1, "weight": 0.6}
TO = {"name": "Finn Hart", "street1": "22 Elk Rd", "city": "Bozeman", "state": "mt", "zip": "59715"}


class FakeShippo:
    def __init__(self):
        self.calls = []

    def call(self, method, path, token, body=None, timeout=30):
        self.calls.append((method, path, body))
        if path == "shipments":
            return {"object_id": "shp_1", "messages": [], "rates": [
                {"object_id": "rate_pri", "amount": "9.85", "currency": "USD", "provider": "USPS",
                 "servicelevel": {"name": "Priority Mail", "token": "usps_priority"}, "estimated_days": 2},
                {"object_id": "rate_gnd", "amount": "5.10", "currency": "USD", "provider": "USPS",
                 "servicelevel": {"name": "Ground Advantage", "token": "usps_ground_advantage"}, "estimated_days": 4}]}
        if path == "transactions":
            return {"object_id": "tx_1", "status": "SUCCESS", "tracking_number": "9400100000000000000001",
                    "tracking_url_provider": "https://tools.usps.com/x", "label_url": "https://shippo-delivery.s3/label.pdf"}
        if path == "refunds":
            return {"object_id": "ref_1", "status": "QUEUED"}
        raise AssertionError((method, path))


@pytest.fixture()
def shippo(main_module, monkeypatch):
    for prefix in ("shipments/", "pos_orders/"):
        for o in S3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []):
            S3.delete_object(Bucket=BUCKET, Key=o["Key"])
    S3.delete_object(Bucket=BUCKET, Key="meta/shipping_settings.json")
    from routes import shipping
    shipping._settings_doc.cache["data"] = None
    shipping._cache.clear()
    monkeypatch.setattr(main_module, "SHIPPO_API_KEY", "shippo_test_x")
    fake = FakeShippo()
    monkeypatch.setattr(shippo_api, "call", fake.call)
    sent = []
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")

    class _Ok:
        ok, text = True, "{}"
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: sent.append(json) or _Ok())
    fake.emails = sent
    # a store sale that ships prints
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    sale = {"id": "pos-ship1", "date": now.strftime("%Y-%m-%d"), "created": now.isoformat(), "name": "Finn Hart",
            "email": "finn@example.com", "address": "22 Elk Rd, Bozeman MT", "location": "Base Area", "total": 38.0,
            "square_payment_id": "SQ1", "items": [{"category": "prints", "label": "8x10 Print", "subtotal": 30},
                                                  {"category": "_shipping", "label": "Shipping", "subtotal": 8}]}
    S3.put_object(Bucket=BUCKET, Key=f"pos_orders/{sale['date']}_pos-ship1.json", Body=json.dumps(sale).encode())
    yield fake
    S3.delete_object(Bucket=BUCKET, Key="meta/shipping_settings.json")
    shipping._settings_doc.cache["data"] = None


def _save_box(owner_client, **extra):
    s = owner_client.get("/api/admin/shipping/settings").json()
    r = owner_client.put("/api/admin/shipping/settings", json={"from": {**s["from"], **extra}, "packages": [BOX]})
    return r


def test_settings_start_from_the_po_box_and_check_what_is_saved(owner_client, admin_client, shippo):
    s = owner_client.get("/api/admin/shipping/settings").json()
    assert s["from"]["street1"] == "PO Box 162712" and s["from"]["zip"] == "59716" and s["ready"] and s["mode"] == "test"
    assert _save_box(owner_client).status_code == 200
    saved = owner_client.get("/api/admin/shipping/settings").json()["packages"]
    assert saved == [{"id": "8x10-mailer", **BOX}]
    bad = owner_client.put("/api/admin/shipping/settings", json={"from": s["from"], "packages": [{"name": "x", "length": "big"}]})
    assert bad.status_code == 400
    no_city = owner_client.put("/api/admin/shipping/settings", json={"from": {**s["from"], "city": ""}, "packages": []})
    assert no_city.status_code == 400


def test_buy_a_label_and_the_customer_gets_the_tracking_number(owner_client, admin_client, shippo):
    _save_box(owner_client)
    key = "pos:pos-ship1"
    info = admin_client.get(f"/api/admin/shipping/order?key={key}").json()
    assert info["shipment"] is None and info["to"]["name"] == "Finn Hart"
    r = admin_client.post("/api/admin/shipping/rates", json={"key": key, "to": TO, "package_id": "8x10-mailer"})
    assert r.status_code == 200, r.text
    rates = r.json()["rates"]
    assert [x["id"] for x in rates] == ["rate_gnd", "rate_pri"] and rates[0]["service"] == "Ground Advantage"
    sent_shipment = shippo.calls[0][2]
    assert sent_shipment["address_from"]["street1"] == "PO Box 162712" and sent_shipment["address_to"]["state"] == "MT"
    assert sent_shipment["parcels"][0] == {"length": "12.0", "width": "10.0", "height": "1.0", "distance_unit": "in",
                                           "weight": "0.6", "mass_unit": "lb"}
    assert admin_client.post("/api/admin/shipping/buy", json={"key": key, "rate_id": "rate_made_up"}).status_code == 400
    b = admin_client.post("/api/admin/shipping/buy", json={"key": key, "rate_id": "rate_gnd"})
    assert b.status_code == 200 and b.json()["emailed"]
    sh = b.json()["shipment"]
    assert sh["status"] == "label" and sh["tracking_number"].startswith("9400") and sh["label_cost"] == 5.10 and sh["test"]
    assert shippo.calls[-1][2] == {"rate": "rate_gnd", "label_file_type": "PDF_4x6", "async": False}
    mail = shippo.emails[-1]
    assert mail["to"] == ["finn@example.com"] and "9400100000000000000001" in mail["html"] and "Track Your Package" in mail["html"]
    # no second label while this one stands
    again = admin_client.post("/api/admin/shipping/rates", json={"key": key, "to": TO, "package_id": "8x10-mailer"})
    assert again.status_code == 409
    row = next(x for x in admin_client.get("/api/admin/sales?days=7").json()["rows"] if x["order_id"] == "pos-ship1")
    assert row["ships"] and row["shipment"]["status"] == "label"


def test_void_then_buy_again(owner_client, admin_client, shippo):
    _save_box(owner_client)
    key = "pos:pos-ship1"
    admin_client.post("/api/admin/shipping/rates", json={"key": key, "to": TO, "package_id": "8x10-mailer"})
    admin_client.post("/api/admin/shipping/buy", json={"key": key, "rate_id": "rate_pri", "email_customer": False})
    v = admin_client.post("/api/admin/shipping/void", json={"key": key})
    assert v.status_code == 200 and v.json()["shipment"]["status"] == "voided"
    assert shippo.calls[-1] == ("POST", "refunds", {"transaction": "tx_1", "async": False})
    assert admin_client.post("/api/admin/shipping/rates", json={"key": key, "to": TO, "package_id": "8x10-mailer"}).status_code == 200


def test_mark_shipped_by_hand(admin_client, shippo):
    key = "pos:pos-ship1"
    assert admin_client.post("/api/admin/shipping/manual", json={"key": key, "carrier": ""}).status_code == 400
    r = admin_client.post("/api/admin/shipping/manual", json={"key": key, "carrier": "USPS", "tracking_number": "9400 1111 2222"})
    assert r.status_code == 200 and r.json()["emailed"]
    sh = r.json()["shipment"]
    assert sh["status"] == "shipped" and sh["manual"] and sh["tracking_url"].endswith("tLabels=940011112222")
    assert admin_client.post("/api/admin/shipping/void", json={"key": key}).json()["shipment"]["status"] == "voided"


def test_shipping_needs_the_studio_and_a_real_order_key(client, admin_client, shippo):
    assert client.post("/api/admin/shipping/rates", json={"key": "pos:pos-ship1"}).status_code == 401
    assert client.get("/api/admin/shipping/settings").status_code == 401
    assert admin_client.post("/api/admin/shipping/rates", json={"key": "../etc"}).status_code == 400
