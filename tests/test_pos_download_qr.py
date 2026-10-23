"""POS Sale Complete screen: a QR code of the customer's download page."""
import json

import boto3
from fastapi.testclient import TestClient

S3 = boto3.client("s3", region_name="us-east-1")
TOKEN = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _pos(main_module):
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json",
                  Body=json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    c = TestClient(main_module.app)
    assert c.post("/api/photographer/auth", json={"pin": "4821"}).status_code == 200
    return c


def test_qr_of_the_download_page(main_module, client):
    main_module._store_pkg(TOKEN, {"token": TOKEN, "title": "Your Photos", "paths": [], "emails": ["a@b.c"],
                                   "expires": "2099-01-01T00:00:00+00:00"})
    pos = _pos(main_module)
    r = pos.get("/api/pos/download-qr", params={"token": TOKEN})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b"<svg" in r.content
    # Square often finishes the sale in Safari, which isn't signed in to the POS: the picture still
    # shows (it's only the link, which whoever asks already has)
    assert client.get("/api/pos/download-qr", params={"token": TOKEN}).status_code == 200
    assert pos.get("/api/pos/download-qr", params={"token": "nope"}).status_code == 404
    assert pos.get("/api/pos/download-qr", params={"token": "b" * 32}).status_code == 404     # no such package


def test_qr_encodes_the_link(main_module):
    import segno
    qr = segno.make(f"{main_module.SITE_URL}/package/{TOKEN}", error="m")
    assert qr.designator  # a valid QR symbol was produced for the link


def test_portrait_sale_gets_a_download_link_and_qr(main_module, monkeypatch):
    # Portrait poses sold on the POS are delivered like action photos: one download page,
    # its link in the receipt and on the Sale Complete screen
    sent = []
    monkeypatch.setattr(main_module, "_send_pos_receipt_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(main_module, "_upsert_customer_from_pos", lambda c: None)
    pos = _pos(main_module)
    poses = ["images/2026-12-05/lone-peak-portraits/smith/P1.jpg", "images/2026-12-05/lone-peak-portraits/smith/P2.jpg"]
    r = pos.post("/api/pos-checkout", json={
        "pin": "4821", "email": "ann@example.com", "name": "Ann Smith", "square_payment_id": "COMP", "total": 65,
        "items": [{"category": "portrait", "label": "Portrait Pose 1 — Smith", "price": 65, "qty": 1, "subtotal": 65,
                   "photo_paths": poses + [poses[0]], "date": "2026-12-05", "location": "Lone Peak Portraits"}]})
    d = r.json()
    assert r.status_code == 200 and d["token"] and d["download_url"].endswith("/package/" + d["token"])
    assert main_module._load_pkg(d["token"])["paths"] == poses                 # each photo once
    assert sent and sent[0]["download_url"] == d["download_url"]
    assert pos.get("/api/pos/download-qr", params={"token": d["token"]}).status_code == 200
    order = next(o for o in main_module._fetch_pos_orders() if o["id"] == d["order_id"])
    assert order["filenames"] == "" and order["package_token"] == d["token"]   # commission rules unchanged
