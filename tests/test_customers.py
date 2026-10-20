"""Customer directory: permanent ids, one-at-a-time studio edits, POS sales add customers."""
import json

import boto3
import pytest
from fastapi.testclient import TestClient

PIN = "4821"
S3 = boto3.client("s3", region_name="us-east-1")


def _put_json(key, payload):
    S3.put_object(Bucket="test-bucket", Key=key, Body=json.dumps(payload).encode())


def _r2_customers():
    return json.loads(S3.get_object(Bucket="test-bucket", Key="pos_data/customers.json")["Body"].read())


@pytest.fixture(autouse=True)
def seed(main_module):
    _put_json("meta/photographers.json", [{"id": "p1", "name": "Test Photog", "pin": PIN}])
    _put_json("pos_data/customers.json", [
        {"first": "Ann",  "last": "Smith", "email": "ann@example.com", "phone": "(406) 555-0101",
         "notes": "Prefers prints", "tags": ["VIP"],
         "followups": [{"date": "2026-09-01", "type": "review", "subject": "Thanks"}]},
        {"first": "Bob",  "last": "Jones", "email": "bob@example.com", "phone": "555-0102"},
        {"first": "Cara", "last": "Adams", "email": "", "phone": "555-0103"},
    ])
    main_module._customers_cache["data"] = None
    main_module._pin_fail_limiter._calls.clear()
    yield
    main_module._customers_cache["data"] = None


def _id_of(admin_client, email):
    r = admin_client.get(f"/api/admin/customers?q={email}")
    return next(c["id"] for c in r.json()["customers"] if c["email"] == email)


# ── Store ────────────────────────────────────────────────────────────────────

def test_every_customer_gets_a_permanent_id_saved_to_r2(admin_client):
    admin_client.get("/api/admin/customers")
    stored = _r2_customers()
    assert all(c.get("id") for c in stored)
    assert len({c["id"] for c in stored}) == 3
    # ids are stable across reloads
    first_ids = [c["id"] for c in stored]
    admin_client.get("/api/admin/customers")
    assert [c["id"] for c in _r2_customers()] == first_ids


def test_failed_r2_read_is_an_error_not_an_empty_directory(main_module, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("R2 timeout")
    monkeypatch.setattr(main_module.s3, "get_object", broken)
    c = TestClient(main_module.app, raise_server_exceptions=False)
    c.cookies.set(main_module.ADMIN_COOKIE, main_module._create_admin_session())
    assert c.post("/api/admin/customers", json={"first": "New"}).status_code == 500
    monkeypatch.undo()
    assert len(_r2_customers()) == 3          # nothing was overwritten


# ── Studio list / search ─────────────────────────────────────────────────────

def test_admin_customer_list_requires_login(client):
    assert client.get("/api/admin/customers").status_code == 401


def test_admin_list_is_paged_and_sorted_by_last_name(admin_client):
    r = admin_client.get("/api/admin/customers?limit=2").json()
    assert r["total"] == 3
    assert [c["last"] for c in r["customers"]] == ["Adams", "Jones"]
    r2 = admin_client.get("/api/admin/customers?limit=2&offset=2").json()
    assert [c["last"] for c in r2["customers"]] == ["Smith"]


@pytest.mark.parametrize("q,expected", [
    ("smi", {"Smith"}),            # name prefix
    ("bob@ex", {"Jones"}),         # email
    ("5550101", {"Smith"}),        # phone digits, any formatting
    ("ann smith", {"Smith"}),      # first + last
])
def test_admin_search(admin_client, q, expected):
    r = admin_client.get("/api/admin/customers", params={"q": q}).json()
    assert {c["last"] for c in r["customers"]} == expected


# ── Studio edits ─────────────────────────────────────────────────────────────

def test_create_customer(admin_client):
    r = admin_client.post("/api/admin/customers", json={"first": "Dan", "last": "Lee", "email": "dan@example.com"})
    assert r.status_code == 200
    assert any(c["email"] == "dan@example.com" and c.get("id") for c in _r2_customers())


def test_create_rejects_duplicate_email(admin_client):
    r = admin_client.post("/api/admin/customers", json={"first": "Ann2", "email": "ANN@example.com"})
    assert r.status_code == 409
    assert len(_r2_customers()) == 3


def test_update_changes_only_that_customer_and_keeps_history(admin_client):
    cid = _id_of(admin_client, "ann@example.com")
    r = admin_client.put(f"/api/admin/customers/{cid}", json={"phone": "406-555-9999", "tags": ["VIP", "Local"]})
    assert r.status_code == 200
    stored = {c["email"]: c for c in _r2_customers()}
    ann = stored["ann@example.com"]
    assert ann["phone"] == "406-555-9999" and ann["tags"] == ["VIP", "Local"]
    assert ann["notes"] == "Prefers prints"
    assert ann["followups"][0]["subject"] == "Thanks"
    assert stored["bob@example.com"]["phone"] == "555-0102"
    assert len(stored) == 3


def test_update_rejects_email_used_by_someone_else(admin_client):
    cid = _id_of(admin_client, "ann@example.com")
    assert admin_client.put(f"/api/admin/customers/{cid}", json={"email": "bob@example.com"}).status_code == 409


def test_delete_removes_only_that_customer(admin_client):
    cid = _id_of(admin_client, "bob@example.com")
    assert admin_client.delete(f"/api/admin/customers/{cid}").status_code == 200
    assert {c["email"] for c in _r2_customers()} == {"ann@example.com", ""}
    assert admin_client.delete(f"/api/admin/customers/{cid}").status_code == 404


# ── POS sales add customers ──────────────────────────────────────────────────

def _pos_sale(client, customer, **extra):
    body = {"pin": PIN, "email": customer["email"],
            "name": f'{customer.get("first","")} {customer.get("last","")}'.strip(),
            "phone": customer.get("phone", ""), "items": [], "total": 25, "customer": customer,
            "square_payment_id": "COMP", **extra}
    return client.post("/api/pos-checkout", json=body)


def test_pos_sale_adds_new_customer_with_full_address(client):
    r = _pos_sale(client, {"first": "Eve", "last": "Park", "email": "eve@example.com", "phone": "555-0199",
                           "street": "1 Lone Peak Dr", "city": "Big Sky", "state": "MT", "zip": "59716"})
    assert r.status_code == 200
    eve = next(c for c in _r2_customers() if c["email"] == "eve@example.com")
    assert (eve["first"], eve["last"], eve["city"], eve["zip"], eve["source"]) == \
           ("Eve", "Park", "Big Sky", "59716", "pos")
    assert eve.get("id")


def test_pos_sale_fills_blanks_on_existing_customer_without_overwriting(client):
    _pos_sale(client, {"first": "Ann", "last": "Smith", "email": "Ann@Example.com", "phone": "406-555-1111",
                       "street": "", "city": "Bozeman", "state": "", "zip": ""})
    stored = [c for c in _r2_customers() if c["email"].lower() == "ann@example.com"]
    assert len(stored) == 1                               # no duplicate created
    ann = stored[0]
    assert ann["phone"] == "(406) 555-0101"               # existing value kept
    assert ann["city"] == "Bozeman"                       # blank filled in
    assert ann["notes"] == "Prefers prints" and ann["tags"] == ["VIP"] and ann["followups"]


def test_shared_email_does_not_rename_the_existing_customer(client):
    """A spouse buying under the same email must not overwrite Ann's name."""
    _pos_sale(client, {"first": "Mark", "last": "Smith", "email": "ann@example.com", "phone": "406-555-2222"})
    ann = next(c for c in _r2_customers() if c["email"] == "ann@example.com")
    assert (ann["first"], ann["last"], ann["phone"]) == ("Ann", "Smith", "(406) 555-0101")
    assert len(_r2_customers()) == 3


def test_old_pos_payload_without_customer_object_still_adds_customer(client):
    body = {"pin": PIN, "email": "old@example.com", "name": "Old Style", "phone": "555-0000",
            "items": [], "total": 10, "square_payment_id": "COMP"}
    assert client.post("/api/pos-checkout", json=body).status_code == 200
    old = next(c for c in _r2_customers() if c["email"] == "old@example.com")
    assert (old["first"], old["last"], old["phone"]) == ("Old", "Style", "555-0000")


def test_customer_save_failure_never_blocks_the_sale(client, main_module, monkeypatch):
    def broken(_info):
        raise RuntimeError("R2 down")
    monkeypatch.setattr(main_module, "_upsert_customer_from_pos", broken)
    r = _pos_sale(client, {"first": "Fay", "last": "Q", "email": "fay@example.com"})
    assert r.status_code == 200 and r.json()["order_id"].startswith("pos-")


# ── Purchase history link ────────────────────────────────────────────────────

def test_history_gallery_link_points_at_real_download_route(main_module):
    import inspect
    from routes import customers
    src = inspect.getsource(customers.api_customer_history)
    assert '"/download/{token}"' in src.replace("f\"", "\"") and "/d/{token}" not in src


def test_customer_filter_is_reachable_not_mistaken_for_a_customer_id(admin_client, main_module, monkeypatch):
    # /api/admin/customers/filtered used to be caught by /api/admin/customers/{cid} ("Customer not found")
    monkeypatch.setattr(main_module, "_fetch_pos_orders", lambda *a, **k: [
        {"id": "p1", "date": "2026-10-18", "created": "2026-10-18T10:00:00", "email": "pos@example.com",
         "name": "Pat Smith", "items": [], "total": 25}])
    monkeypatch.setattr(main_module, "_fetch_wc_orders_all", lambda *a, **k: [])
    r = admin_client.get("/api/admin/customers/filtered?date_from=2026-10-01&date_to=2026-10-31&types=in-person")
    assert r.status_code == 200 and "customers" in r.json()
    assert [c["email"] for c in r.json()["customers"]] == ["pos@example.com"]
