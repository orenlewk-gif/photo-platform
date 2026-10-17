"""Square handoff: the POS gets the app ID and a Square customer id; the studio can test the token."""
import json

import boto3
import pytest

PIN = "4821"
PIN_HDR = {"X-POS-PIN": PIN}
CUSTOMER = {"first": "Ann", "last": "Smith", "email": "ann@example.com", "phone": "406-555-0101",
            "street": "1 Lone Peak Dr", "city": "Big Sky", "state": "MT", "zip": "59716"}


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSquare:
    """Records calls; answers search/create/locations like Square would."""
    def __init__(self, existing=None, create_fail_with_phone=False, search_status=200):
        self.calls, self.existing = [], existing
        self.create_fail_with_phone, self.search_status = create_fail_with_phone, search_status

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append(("POST", url, json, headers))
        if url.endswith("/customers/search"):
            if self.search_status != 200:
                return _Resp(self.search_status, {"errors": [{"code": "UNAUTHORIZED"}]})
            return _Resp(200, {"customers": [{"id": self.existing}]} if self.existing else {})
        if url.endswith("/customers"):
            if self.create_fail_with_phone and "phone_number" in json:
                return _Resp(400, {"errors": [{"code": "INVALID_PHONE_NUMBER"}]})
            return _Resp(200, {"customer": {"id": "NEW_SQ_CUSTOMER"}})
        raise AssertionError(url)

    orders = {}   # order id -> order dict (set per test)

    def get(self, url, headers=None, **kw):
        self.calls.append(("GET", url, None, headers))
        if "/orders/" in url:
            oid = url.rsplit("/", 1)[-1]
            if oid in self.orders:
                return _Resp(200, {"order": self.orders[oid]})
            return _Resp(404, {"errors": [{"code": "NOT_FOUND"}]})
        return _Resp(200, {"locations": [{"id": "LOC1", "name": "Big Sky Base"}]})


@pytest.fixture()
def square(main_module, monkeypatch):
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket="test-bucket", Key="meta/photographers.json",
        Body=json.dumps([{"id": "p1", "name": "Test Photog", "pin": PIN}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    monkeypatch.setattr(main_module, "SQUARE_APP_ID", "sq0idp-TESTAPP")
    monkeypatch.setattr(main_module, "SQUARE_ACCESS_TOKEN", "EAAA_test_token")
    monkeypatch.setattr(main_module, "SQUARE_LOCATION_ID", "LOC1")

    def install(fake):
        monkeypatch.setattr(main_module.http_requests, "post", fake.post)
        monkeypatch.setattr(main_module.http_requests, "get", fake.get)
        return fake
    return install


def _prepare(client, customer=CUSTOMER, headers=PIN_HDR):
    return client.post("/api/pos/square-prepare", json={"customer": customer}, headers=headers)


def test_prepare_requires_pin(client, square):
    fake = square(FakeSquare())
    assert _prepare(client, headers={}).status_code == 401
    assert fake.calls == []


def test_prepare_returns_app_id_and_existing_customer(client, square):
    fake = square(FakeSquare(existing="SQ_ANN"))
    r = _prepare(client)
    assert r.status_code == 200
    assert r.json() == {"client_id": "sq0idp-TESTAPP", "customer_id": "SQ_ANN", "customer_problem": None}
    method, url, body, headers = fake.calls[0]
    assert url.endswith("/customers/search")
    assert body["query"]["filter"]["email_address"]["exact"] == "ann@example.com"
    assert headers["Authorization"] == "Bearer EAAA_test_token"
    assert len(fake.calls) == 1                      # found — no create


def test_prepare_creates_customer_when_new(client, square):
    fake = square(FakeSquare())
    assert _prepare(client).json()["customer_id"] == "NEW_SQ_CUSTOMER"
    created = fake.calls[1][2]
    assert created["given_name"] == "Ann" and created["email_address"] == "ann@example.com"
    assert created["address"]["locality"] == "Big Sky" and created["idempotency_key"]


def test_prepare_retries_without_phone_when_square_rejects_it(client, square):
    fake = square(FakeSquare(create_fail_with_phone=True))
    assert _prepare(client).json()["customer_id"] == "NEW_SQ_CUSTOMER"
    assert "phone_number" not in fake.calls[-1][2]


def test_rejected_token_blocks_the_sale_before_charging(client, square):
    square(FakeSquare(search_status=401))
    r = _prepare(client)
    assert r.status_code == 503 and "rejected the access token" in r.json()["error"]


def test_customer_problem_is_reported_but_sale_can_proceed(client, square):
    square(FakeSquare(search_status=500))
    r = _prepare(client)
    d = r.json()
    assert r.status_code == 200 and d["client_id"] == "sq0idp-TESTAPP" and d["customer_id"] is None
    assert "500" in d["customer_problem"]


def test_missing_token_blocks_the_sale(client, square, main_module, monkeypatch):
    square(FakeSquare())
    monkeypatch.setattr(main_module, "SQUARE_ACCESS_TOKEN", "")
    assert _prepare(client).status_code == 503


def test_prepare_without_app_id_explains(client, square, main_module, monkeypatch):
    square(FakeSquare())
    monkeypatch.setattr(main_module, "SQUARE_APP_ID", "")
    r = _prepare(client)
    assert r.status_code == 503 and "SQUARE_APP_ID" in r.json()["error"]


def test_connection_test_requires_admin(client, square):
    square(FakeSquare())
    assert client.get("/api/admin/square/test").status_code == 401


def test_connection_test_reports_without_leaking_token(admin_client, square):
    square(FakeSquare())
    r = admin_client.get("/api/admin/square/test")
    d = r.json()
    assert d["ok"] is True and d["locations"] == ["Big Sky Base"] and d["location_id_matches"] is True
    assert "EAAA_test_token" not in r.text



# ── Payment is confirmed with Square before the sale is recorded ─────────────

def _paid_order(cents=100, location="LOC1", state="COMPLETED"):
    return {"id": "ORDER1", "location_id": location, "state": state,
            "tenders": [{"type": "CASH", "amount_money": {"amount": cents, "currency": "USD"}}]}


def _checkout(client, square_id, total=1.0):
    return client.post("/api/pos-checkout", json={
        "pin": PIN, "email": "ann@example.com", "name": "Ann Smith", "items": [], "total": total,
        "square_payment_id": square_id, "customer": CUSTOMER})


def _orders():
    s3 = boto3.client("s3", region_name="us-east-1")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket", Prefix="pos_orders/").get("Contents", [])]
    return [json.loads(s3.get_object(Bucket="test-bucket", Key=k)["Body"].read()) for k in keys]


@pytest.fixture()
def no_pos_orders(main_module, monkeypatch):
    s3 = boto3.client("s3", region_name="us-east-1")
    for o in s3.list_objects_v2(Bucket="test-bucket", Prefix="pos_orders/").get("Contents", []):
        s3.delete_object(Bucket="test-bucket", Key=o["Key"])
    monkeypatch.setattr(main_module._time, "sleep", lambda s: None)


def test_verified_square_payment_records_the_sale(client, square, no_pos_orders):
    fake = square(FakeSquare(existing="SQ_ANN"))
    fake.orders = {"ORDER1": _paid_order(cents=104)}          # $1.00 + 4% tax
    r = _checkout(client, "ORDER1")
    assert r.status_code == 200
    [order] = _orders()
    assert order["square_payment_id"] == "ORDER1" and order["square_verified"] is True
    assert order["square_paid"] == 1.04 and order["square_tenders"] == ["CASH"]


@pytest.mark.parametrize("orders,expected", [
    ({}, "no record"),                                          # Square never heard of it
    ({"ORDER1": _paid_order(cents=50)}, "less than"),           # underpaid
    ({"ORDER1": _paid_order(location="OTHER")}, "different Square location"),
    ({"ORDER1": {**_paid_order(), "tenders": []}}, "no payment"),
    ({"ORDER1": _paid_order(state="CANCELED")}, "isn't paid"),
])
def test_unconfirmed_payment_records_nothing(client, square, no_pos_orders, orders, expected):
    fake = square(FakeSquare())
    fake.orders = orders
    r = _checkout(client, "ORDER1")
    assert r.status_code == 402 and expected in r.json()["error"] and r.json()["verify_failed"]
    assert _orders() == []


def test_missing_transaction_id_is_not_accepted(client, square, no_pos_orders):
    square(FakeSquare())
    for sid in ("", None):
        assert _checkout(client, sid).status_code == 402
    assert _orders() == []


def test_same_square_payment_cannot_be_used_twice(client, square, no_pos_orders):
    fake = square(FakeSquare())
    fake.orders = {"ORDER1": _paid_order()}
    assert _checkout(client, "ORDER1").status_code == 200
    r = _checkout(client, "ORDER1")
    assert r.status_code == 409 and len(_orders()) == 1


def test_comp_sale_needs_no_square_check(client, square, no_pos_orders):
    fake = square(FakeSquare())
    assert _checkout(client, "COMP").status_code == 200
    assert not any("/orders/" in c[1] for c in fake.calls)
