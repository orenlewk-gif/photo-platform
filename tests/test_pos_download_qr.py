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
    assert client.get("/api/pos/download-qr", params={"token": TOKEN}).status_code == 401   # POS only
    assert pos.get("/api/pos/download-qr", params={"token": "nope"}).status_code == 404
    assert pos.get("/api/pos/download-qr", params={"token": "b" * 32}).status_code == 404     # no such package


def test_qr_encodes_the_link(main_module):
    import segno
    qr = segno.make(f"{main_module.SITE_URL}/package/{TOKEN}", error="m")
    assert qr.designator  # a valid QR symbol was produced for the link
