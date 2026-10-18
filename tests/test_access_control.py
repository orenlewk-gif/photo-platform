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

def test_timecard_edit_delete_require_admin(client, admin_client):
    assert client.put("/api/clock/record/rec1", json={"location": "x"}).status_code == 401
    assert client.delete("/api/clock/record/rec1").status_code == 401
    assert admin_client.put("/api/clock/record/rec1", json={"location": "Base"}).status_code == 200
    assert admin_client.delete("/api/clock/record/rec1").status_code == 200


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
