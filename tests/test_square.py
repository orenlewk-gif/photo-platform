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

    def get(self, url, headers=None, **kw):
        self.calls.append(("GET", url, None, headers))
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
    assert r.json() == {"client_id": "sq0idp-TESTAPP", "customer_id": "SQ_ANN"}
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


def test_square_problem_still_lets_the_sale_proceed(client, square):
    square(FakeSquare(search_status=401))
    r = _prepare(client)
    assert r.status_code == 200 and r.json() == {"client_id": "sq0idp-TESTAPP", "customer_id": None}


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
