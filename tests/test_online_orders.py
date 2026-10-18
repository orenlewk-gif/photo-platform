"""Online checkout and the WooCommerce webhook: off the main loop, one link and one email per order."""
import base64
import hashlib
import hmac
import json
import threading

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")


SECRET = "test-webhook-secret"


def _signed(order, secret=SECRET):
    """A webhook message signed the way WooCommerce signs it."""
    body = json.dumps(order).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return {"content": body, "headers": {"X-WC-Webhook-Signature": sig}}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload or {}
        self.ok = 200 <= status < 300
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeWoo:
    """Records WooCommerce calls: order creation and customer notes (the download email)."""
    def __init__(self, delay=0.0):
        self.notes, self.created, self.delay = [], [], delay

    def post(self, url, json=None, **kw):
        if url.endswith("/notes"):
            threading.Event().wait(self.delay)
            self.notes.append((url, json))
            return _Resp(201, {"id": len(self.notes)})
        if url.endswith("/orders"):
            self.created.append(json)
            return _Resp(201, {"id": 4321, "order_key": "wc_order_abc"})
        raise AssertionError(url)


@pytest.fixture()
def woo(main_module, monkeypatch):
    for prefix in ("downloads/",):
        for o in S3.list_objects_v2(Bucket="test-bucket", Prefix=prefix).get("Contents", []):
            S3.delete_object(Bucket="test-bucket", Key=o["Key"])
    S3.put_object(Bucket="test-bucket", Key="meta/discount_codes.json", Body=json.dumps([
        {"id": "SAVE10", "code": "SAVE10", "discount_type": "percent", "amount": 10, "active": True,
         "usage_count": 0, "uses": []}]).encode())
    monkeypatch.setattr(main_module, "WC_WEBHOOK_SECRET", SECRET)
    fake = FakeWoo(delay=0.3)
    monkeypatch.setattr(main_module.http_requests, "post", fake.post)
    return fake


def _paid_order(status, order_id=1576):
    return {"id": order_id, "status": status, "order_key": "wc_order_k", "total": "45.00",
            "billing": {"first_name": "Ann", "last_name": "Smith", "email": "ann@example.com"},
            "meta_data": [{"key": "_photo_paths", "value": "images/2026-06-12/base/IMG_1.jpg"},
                          {"key": "_photo_location", "value": "Base"},
                          {"key": "_photo_date", "value": "2026-06-12"},
                          {"key": "_photo_files", "value": "IMG_1.jpg"},
                          {"key": "_coupon_code", "value": "save10"},
                          {"key": "_coupon_discount", "value": "5"}]}


def _tokens():
    return [o["Key"] for o in S3.list_objects_v2(Bucket="test-bucket", Prefix="downloads/").get("Contents", [])
            if "/by_order/" not in o["Key"]]


def _coupon_uses():
    codes = json.loads(S3.get_object(Bucket="test-bucket", Key="meta/discount_codes.json")["Body"].read())
    return codes[0]["usage_count"]


def test_paid_then_completed_sends_one_link_and_one_email(client, woo):
    assert client.post("/webhook", **_signed(_paid_order("processing"))).status_code == 200
    assert client.post("/webhook", **_signed(_paid_order("completed"))).status_code == 200
    assert len(_tokens()) == 1 and len(woo.notes) == 1 and _coupon_uses() == 1


def test_both_calls_arriving_together_still_send_one(client, woo, main_module, monkeypatch):
    real_store = main_module._store_token

    def slow_store(token, payload):                          # slow storage widens the gap
        threading.Event().wait(0.3)                          # between "has a link?" and saving it
        real_store(token, payload)
    monkeypatch.setattr(main_module, "_store_token", slow_store)
    def call(status):
        return lambda: client.post("/webhook", **_signed(_paid_order(status))).status_code
    out = []
    threads = [threading.Thread(target=lambda f=f: out.append(f())) for f in (call("processing"), call("completed"))]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert out == [200, 200]
    assert len(_tokens()) == 1 and len(woo.notes) == 1 and _coupon_uses() == 1


def test_different_orders_each_get_their_link(client, woo):
    client.post("/webhook", **_signed(_paid_order("processing", 1)))
    client.post("/webhook", **_signed(_paid_order("processing", 2)))
    assert len(_tokens()) == 2 and len(woo.notes) == 2 and _coupon_uses() == 2


def test_an_expired_link_is_replaced_by_a_new_one(client, woo):
    S3.put_object(Bucket="test-bucket", Key="downloads/by_order/1576.json",
                  Body=json.dumps({"token": "old", "expires": "2026-01-01T00:00:00+00:00"}).encode())
    client.post("/webhook", **_signed(_paid_order("completed")))
    assert len(woo.notes) == 1


def test_unpaid_orders_are_ignored(client, woo):
    assert client.post("/webhook", **_signed(_paid_order("pending"))).status_code == 200
    assert _tokens() == [] and woo.notes == []


def test_online_checkout_doesnt_hold_up_other_visitors(main_module, monkeypatch):
    from fastapi.testclient import TestClient
    entered, release = threading.Event(), threading.Event()

    def stuck_post(url, json=None, **kw):
        entered.set()
        release.wait(10)
        return _Resp(201, {"id": 1, "order_key": "k"})
    monkeypatch.setattr(main_module.http_requests, "post", stuck_post)

    with TestClient(main_module.app) as c:                    # one event loop for every request
        result = []
        checkout = threading.Thread(target=lambda: result.append(c.post("/api/checkout", json={
            "digital_albums": [], "prints": [], "frames": [], "email": "ann@example.com"})))
        checkout.start()
        assert entered.wait(5)
        answered = []
        other = threading.Thread(target=lambda: answered.append(c.get("/healthz").status_code))
        other.start()
        other.join(3)
        answered_while_stuck = list(answered)
        release.set()
        checkout.join(10)
        other.join(10)
    assert answered_while_stuck == [200]
    assert result[0].status_code == 200 and "order-pay/1/" in result[0].json()["checkout_url"]



def test_unsigned_or_forged_messages_are_refused(client, woo):
    order = _paid_order("processing")
    assert client.post("/webhook", content=json.dumps(order)).status_code == 401              # no signature
    assert client.post("/webhook", **_signed(order, secret="guess")).status_code == 401       # wrong secret
    assert _tokens() == [] and woo.notes == []


def test_without_the_secret_on_the_server_every_message_is_refused(client, woo, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "WC_WEBHOOK_SECRET", "")
    r = client.post("/webhook", **_signed(_paid_order("processing"), secret=""))   # "signed" with an empty secret
    assert r.status_code == 503
    assert _tokens() == [] and woo.notes == []
