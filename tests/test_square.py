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
        if "/payments/" in url:
            pid = url.rsplit("/", 1)[-1]
            return _Resp(200, {"payment": {"id": pid, "receipt_number": "Kd2P",
                                           "receipt_url": f"https://squareup.com/receipt/preview/{pid}"}})
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


def test_verified_sale_saves_square_tax_and_tip(client, square, no_pos_orders):
    fake = square(FakeSquare())
    fake.orders = {"ORDER1": {**_paid_order(cents=704), "total_tax_money": {"amount": 4},
                              "total_tip_money": {"amount": 600}}}
    assert _checkout(client, "ORDER1").status_code == 200
    [order] = _orders()
    assert (order["square_paid"], order["square_tax"], order["square_tip"]) == (7.04, 0.04, 6.0)


class FakeSquareAndResend(FakeSquare):
    """Also accepts Resend emails, so a sale's emails can be counted."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.emails = []

    def post(self, url, json=None, headers=None, **kw):
        if url == "https://api.resend.com/emails":
            self.emails.append(json)
            return _Resp(200, {"id": "email1"})
        return super().post(url, json=json, headers=headers, **kw)


def test_photo_sale_sends_one_email_whose_link_works_right_away(client, square, no_pos_orders,
                                                                main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    fake = square(FakeSquareAndResend())
    fake.orders = {"ORDER1": _paid_order(cents=2600)}
    r = client.post("/api/pos-checkout", json={
        "pin": PIN, "email": "ann@example.com", "name": "Ann Smith", "total": 25.0,
        "square_payment_id": "ORDER1", "customer": CUSTOMER,
        "items": [{"category": "action_photos", "label": "Action Photos", "qty": 1, "price": 25.0,
                   "subtotal": 25.0, "photo_paths": ["images/2026-06-12/base/IMG_1.jpg"],
                   "filenames": ["IMG_1.jpg"], "location": "Base", "date": "2026-06-12"}]})
    assert r.status_code == 200
    token = r.json()["token"]

    [email] = fake.emails                                   # the receipt only, no second email
    assert email["to"] == ["ann@example.com"] and "Receipt" in email["subject"]
    assert f"/package/{token}" in email["html"] and "available until" in email["html"]
    assert client.get(f"/package/{token}").status_code == 200   # usable the moment it's sent


def test_sale_without_photos_sends_receipt_with_no_download_button(client, square, no_pos_orders,
                                                                   main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    fake = square(FakeSquareAndResend())
    fake.orders = {"ORDER1": _paid_order()}
    assert _checkout(client, "ORDER1").status_code == 200
    [email] = fake.emails
    assert "/package/" not in email["html"] and "Download Your Photos" not in email["html"]


# ── Sales that Square finishes in Safari ─────────────────────────────────────
STATE = "0f8c1d2e-3a4b-4c5d-8e9f-0a1b2c3d4e5f"
SALE = {"email": "ann@example.com", "name": "Ann Smith", "total": 25.0, "customer": CUSTOMER,
        "items": [{"category": "action_photos", "label": "Action Photos", "qty": 1, "price": 25.0,
                   "subtotal": 25.0, "photo_paths": ["images/2026-06-12/base/IMG_1.jpg"],
                   "filenames": ["IMG_1.jpg"], "location": "Base", "date": "2026-06-12"}]}


@pytest.fixture()
def no_pending(main_module):
    s3 = boto3.client("s3", region_name="us-east-1")
    for o in s3.list_objects_v2(Bucket="test-bucket", Prefix="pos_pending/").get("Contents", []):
        s3.delete_object(Bucket="test-bucket", Key=o["Key"])


def _save_pending(client, state=STATE, sale=SALE, headers=PIN_HDR):
    return client.post("/api/pos/square-pending", json={"state": state, "sale": sale}, headers=headers)


def _complete(client, transaction_id="ORDER1", state=STATE):
    return client.post("/api/pos/square-complete", json={"state": state, "transaction_id": transaction_id})


def test_saving_a_pending_sale_needs_the_pin_and_a_real_reference(client, square, no_pending):
    square(FakeSquare())
    assert _save_pending(client, headers={}).status_code == 401
    assert _save_pending(client, headers={"X-POS-PIN": "0000"}).status_code == 401
    assert _save_pending(client, state="abc").status_code == 400          # too short to be unguessable
    assert _save_pending(client, state="../../images.json-xxxxxxxx").status_code == 400
    assert _save_pending(client, sale={**SALE, "email": ""}).status_code == 400


def test_sale_finished_in_safari_is_verified_recorded_and_emailed_once(client, square, no_pos_orders,
                                                                      no_pending, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    fake = square(FakeSquareAndResend())
    fake.orders = {"ORDER1": _paid_order(cents=2600)}
    assert _save_pending(client).status_code == 200

    r = _complete(client)                                    # Safari: no PIN, no saved cart
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "ann@example.com" and data["download_url"].endswith(data["token"])
    [order] = _orders()
    assert order["square_payment_id"] == "ORDER1" and order["square_verified"] is True
    assert order["photographer_name"] == "Test Photog" and order["total"] == 25.0
    assert len(fake.emails) == 1

    # The Home Screen app learns the sale is done
    assert client.get(f"/api/pos/square-pending/{STATE}").status_code == 401
    status = client.get(f"/api/pos/square-pending/{STATE}", headers=PIN_HDR).json()
    assert status["status"] == "completed" and status["result"]["order_id"] == data["order_id"]

    # Reloading the Safari page doesn't record or email it again
    again = _complete(client)
    assert again.status_code == 200 and again.json()["order_id"] == data["order_id"]
    assert len(_orders()) == 1 and len(fake.emails) == 1

    # And a completed sale can't be replaced with different details
    assert _save_pending(client, sale={**SALE, "total": 1.0}).status_code == 409


def test_unconfirmed_payment_leaves_the_sale_pending_so_it_can_be_checked_again(client, square,
                                                                                no_pos_orders, no_pending):
    fake = square(FakeSquare())
    fake.orders = {}
    _save_pending(client)
    r = _complete(client)
    assert r.status_code == 402 and r.json()["verify_failed"]
    assert _orders() == []
    assert client.get(f"/api/pos/square-pending/{STATE}", headers=PIN_HDR).json()["status"] == "pending"

    fake.orders = {"ORDER1": _paid_order(cents=2600)}         # Square catches up
    assert _complete(client).status_code == 200 and len(_orders()) == 1


def test_underpaid_square_sale_is_refused_against_the_saved_total(client, square, no_pos_orders, no_pending):
    fake = square(FakeSquare())
    fake.orders = {"ORDER1": _paid_order(cents=104)}          # paid $1.04 for a $25 sale
    _save_pending(client)
    r = _complete(client)
    assert r.status_code == 402 and "less than" in r.json()["error"]
    assert _orders() == []


def test_unknown_or_stale_sales_cannot_be_finished(client, square, no_pos_orders, no_pending, main_module):
    fake = square(FakeSquare())
    fake.orders = {"ORDER1": _paid_order(cents=2600)}
    assert _complete(client, state="ffffffff-0000-0000-0000-000000000000").status_code == 404
    _save_pending(client)
    old = main_module._load_pos_pending(STATE)
    main_module._save_pos_pending(STATE, {**old, "created": "2026-01-01T00:00:00+00:00"})
    assert _complete(client).status_code == 410
    assert _orders() == []


# ── Square fees and receipt numbers ──────────────────────────────────────────

def test_fee_info_by_payment_type(main_module):
    info = main_module._square_fee_info
    cash = {"tenders": [{"id": "T1", "type": "CASH"}]}
    card_waiting = {"tenders": [{"id": "T2", "type": "CARD"}]}
    card_done = {"tenders": [{"id": "T3", "type": "CARD", "processing_fee_money": {"amount": 31}}]}
    assert info(cash) == {"fee_cents": 0, "fee_final": True, "payment_ids": ["T1"]}
    assert info(card_waiting)["fee_final"] is False
    assert info(card_done) == {"fee_cents": 31, "fee_final": True, "payment_ids": ["T3"]}


def test_sale_records_square_fee_and_receipt_number(client, square, no_pos_orders):
    fake = square(FakeSquare())
    fake.orders = {"ORDER1": {**_paid_order(cents=2600),
                              "tenders": [{"id": "PAY1", "type": "CARD", "amount_money": {"amount": 2600},
                                           "processing_fee_money": {"amount": 80}}]}}
    assert _checkout(client, "ORDER1").status_code == 200
    [order] = _orders()
    assert order["square_fee"] == 0.8 and order["square_fee_final"] is True
    assert order["square_payment_ids"] == ["PAY1"] and order["square_receipt_number"] == "Kd2P"


def test_card_fee_square_fills_in_later_shows_up_on_the_orders_tab(admin_client, square, no_pos_orders,
                                                                  main_module, monkeypatch):
    from datetime import datetime, timedelta, timezone
    s3 = boto3.client("s3", region_name="us-east-1")
    created = datetime.now(timezone.utc) - timedelta(minutes=5)
    rec = {"id": "pos-fee1", "date": created.strftime("%Y-%m-%d"), "created": created.isoformat(),
           "name": "Ann Smith", "email": "ann@example.com", "location": "Base", "total": 25.0,
           "coupon_discount": 0, "square_payment_id": "ORDER1", "square_verified": True,
           "square_paid": 26.0, "square_fee": 0, "square_fee_final": False, "filenames": "IMG_1.jpg"}
    key = f"pos_orders/{rec['date']}_pos-fee1.json"
    s3.put_object(Bucket="test-bucket", Key=key, Body=json.dumps(rec).encode())

    fake = square(FakeSquare())
    fake.orders = {"ORDER1": {**_paid_order(cents=2600), "tenders": [{"id": "PAY1", "type": "CARD"}]}}
    monkeypatch.setattr(main_module, "_fetch_wc_orders", lambda *a, **kw: [])   # no online orders
    html = admin_client.get("/admin/orders?days=7").text
    assert ">pending<" in html                                  # Square hasn't filled it in yet

    fake.orders["ORDER1"]["tenders"][0]["processing_fee_money"] = {"amount": 75}
    html = admin_client.get("/admin/orders?days=7").text
    assert "$0.75" in html and "$24.25" in html                 # fee, and net = 25.00 - 0.75
    assert "Kd2P" in html and "<th>Order #</th>" in html and "<th>Card Fee</th>" in html
    saved = json.loads(s3.get_object(Bucket="test-bucket", Key=key)["Body"].read())
    assert saved["square_fee"] == 0.75 and saved["square_fee_final"] is True

    calls = len(fake.calls)
    admin_client.get("/admin/orders?days=7")                    # once final, Square isn't asked again
    assert len(fake.calls) == calls
    csv_line = next(l for l in admin_client.get("/admin/export?days=7").text.splitlines() if "Ann" in l)
    assert "Square Kd2P" in csv_line and "$0.75" in csv_line
