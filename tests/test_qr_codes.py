"""Studio QR codes: a printed code's short address forwards to a destination the studio can
change, and scans are counted."""
import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")


@pytest.fixture(autouse=True)
def fresh(main_module):
    from routes import qr_codes
    S3.delete_object(Bucket="test-bucket", Key=qr_codes.QR_KEY)
    qr_codes._cache["data"] = None
    qr_codes._pending.clear()
    yield qr_codes
    qr_codes._pending.clear()


def _make(admin_client, **kw):
    return admin_client.post("/api/admin/qr-codes", json={"name": "Eagle Mount flyer", "target": "/eaglemount", **kw})


def test_a_code_forwards_and_counts_scans(admin_client, client, fresh):
    code = _make(admin_client).json()
    assert code["slug"] == "eagle-mount-flyer" and code["short_url"].endswith("/q/eagle-mount-flyer")
    for _ in range(3):
        r = client.get("/q/eagle-mount-flyer", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/eaglemount"
        assert r.headers["cache-control"] == "no-store"            # every scan reaches the site
    assert admin_client.get("/api/admin/qr-codes").json()["codes"][0]["scans"] == 3   # counted before saving
    fresh.flush_scans()
    row = admin_client.get("/api/admin/qr-codes").json()["codes"][0]
    assert row["scans"] == 3 and row["last_7"] == 3 and row["last_scan"]


def test_changing_the_destination_keeps_the_printed_address(admin_client, client):
    code = _make(admin_client, slug="em").json()
    r = admin_client.put(f"/api/admin/qr-codes/{code['id']}", json={"target": "https://bigskyphotos.com/eagle-mount", "slug": "other"})
    assert r.json()["slug"] == "em"
    assert client.get("/q/em", follow_redirects=False).headers["location"] == "https://bigskyphotos.com/eagle-mount"


@pytest.mark.parametrize("target", ["javascript:alert(1)", "eaglemount", "//evil.example", "ftp://x.com/a", ""])
def test_destinations_must_be_web_addresses(admin_client, target):
    assert _make(admin_client, target=target).status_code == 400


def test_short_names_are_unique_and_tidy(admin_client):
    assert _make(admin_client, slug="em").status_code == 200
    assert _make(admin_client, slug="em").status_code == 409
    assert _make(admin_client, slug="Bad Name!").status_code == 400


def test_unknown_or_deleted_codes_open_the_gallery(admin_client, client):
    code = _make(admin_client, slug="gone").json()
    assert admin_client.delete(f"/api/admin/qr-codes/{code['id']}").json()["ok"]
    assert client.get("/q/gone", follow_redirects=False).headers["location"] == "/"
    assert client.get("/q/never-made", follow_redirects=False).headers["location"] == "/"


def test_print_images(admin_client):
    code = _make(admin_client).json()
    png = admin_client.get(f"/api/admin/qr-codes/{code['id']}/image")
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    from PIL import Image
    from io import BytesIO
    assert Image.open(BytesIO(png.content)).size[0] >= 1000
    svg = admin_client.get(f"/api/admin/qr-codes/{code['id']}/image", params={"format": "svg"})
    assert svg.headers["content-type"].startswith("image/svg") and b"<svg" in svg.content


def test_studio_only(client):
    assert client.get("/api/admin/qr-codes").status_code == 401
    assert client.post("/api/admin/qr-codes", json={"name": "x", "target": "/"}).status_code == 401
