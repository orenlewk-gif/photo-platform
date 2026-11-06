"""Online checkout with Stripe: our own order record (WooCommerce's shape), paid on our pay page
with Stripe's embedded form, delivered once (webhook or thank-you page, whichever comes first),
refunds from Stripe, and the Settings switch. Stripe itself is replaced by a fake here."""
import hashlib
import hmac
import json
import time

import boto3
import pytest

from core import stripe_api

S3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "test-bucket"
ED = "images/2026-06-07/el-dorado-action/"
ALBUM = {"location": "El Dorado Action", "date": "2026-06-07", "count": 2, "last_name": "El Dorado Action",
         "filenames": ["A.jpg", "B.jpg"], "paths": [ED + "A.jpg", ED + "B.jpg"]}
WEBHOOK_SECRET = "whsec_test"


class FakeStripe:
    """Stands in for Stripe's API: sessions made, sessions read back, payment intents."""
    def __init__(self):
        self.sessions, self.calls = {}, []
        self.paid = set()

    def call(self, method, path, key, params=None, idempotency_key="", timeout=20):
        self.calls.append((method, path, params, idempotency_key))
        if method == "POST" and path == "checkout/sessions":
            sid = f"cs_test_{len(self.sessions) + 1}"
            amount = params["line_items"][0]["price_data"]["unit_amount"]
            self.sessions[sid] = {"id": sid, "client_secret": f"{sid}_secret", "amount_total": amount, "currency": "usd",
                                  "metadata": dict(params["metadata"]), "client_reference_id": params["client_reference_id"],
                                  "payment_intent": f"pi_{sid}", "payment_status": "unpaid"}
            return dict(self.sessions[sid])
        if method == "GET" and path.startswith("checkout/sessions/"):
            s = dict(self.sessions[path.rsplit("/", 1)[1]])
            if s["id"] in self.paid:
                s["payment_status"] = "paid"
            return s
        if method == "GET" and path.startswith("payment_intents/"):
            sid = path.rsplit("/", 1)[1][3:]
            return {"id": path.rsplit("/", 1)[1], "metadata": self.sessions[sid]["metadata"],
                    "latest_charge": {"balance_transaction": {"fee": 59}}}
        raise AssertionError((method, path))

    def pay(self, sid):
        self.paid.add(sid)
        s = dict(self.sessions[sid])
        s["payment_status"] = "paid"
        return s


def _photo(path, loc, **kw):
    return {"path": path, "date": "2026-06-07", "location": loc, "last_name": "", "group": "", **kw}


@pytest.fixture()
def stripe(main_module, monkeypatch, live_photos):
    live_photos([_photo(ED + n, "El Dorado Action") for n in ("A.jpg", "B.jpg", "Z.jpg")])
    S3.put_object(Bucket=BUCKET, Key="meta/pricing.json", Body=json.dumps({
        "default": {"tiers": [{"count": 1, "price": 30}, {"count": "all", "price": 100}]},
        "activity_categories": [{"name": "Winter Action", "acts": ["El Dorado Action"]}],
        "activities": {"Winter Action": {"tiers": [{"count": 1, "price": 25}, {"count": "all", "price": 115, "max": True}]}},
    }).encode())
    main_module._pricing_cache["data"] = None
    for prefix in ("online_orders/", "downloads/"):
        for o in S3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []):
            S3.delete_object(Bucket=BUCKET, Key=o["Key"])
    for key in ("meta/online_order_seq.json", "meta/order_refunds.json"):
        S3.delete_object(Bucket=BUCKET, Key=key)
    main_module._online_order_cache.clear()
    monkeypatch.setattr(main_module, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(main_module, "STRIPE_PUBLISHABLE_KEY", "pk_test_x")
    monkeypatch.setattr(main_module, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    main_module._checkout_settings_doc.save({"provider": "stripe"})
    fake = FakeStripe()
    monkeypatch.setattr(stripe_api, "call", fake.call)
    sent = []
    monkeypatch.setattr(main_module, "_send_online_order_email", lambda order, made: sent.append((order["id"], made)) or True)
    fake.emails = sent
    yield fake
    # The fee is noted in the background after a payment and saves the order again: wait for it,
    # or it can put a deleted test order back after the clean-up below (other tests' reports see it)
    import threading
    for t in threading.enumerate():
        if getattr(t, "_target", None) is main_module._note_stripe_fee:
            t.join(10)
    main_module._checkout_settings_doc.save({})
    for o in S3.list_objects_v2(Bucket=BUCKET, Prefix="online_orders/").get("Contents", []):   # no test orders left
        S3.delete_object(Bucket=BUCKET, Key=o["Key"])                                          # for other tests' Orders pages
    main_module._online_order_cache.clear()
    S3.delete_object(Bucket=BUCKET, Key="meta/pricing.json")
    main_module._pricing_cache["data"] = None


def _checkout(client, **extra):
    body = {"digital_albums": [ALBUM], "prints": [], "frames": [], "email": "ann@example.com",
            "billing": {"first_name": "Ann", "last_name": "Smith"}, "date": "2026-06-07",
            "location": "El Dorado Action", **extra}
    r = client.post("/api/checkout", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _signed(event, secret=WEBHOOK_SECRET, t=None):
    payload = json.dumps(event).encode()
    t = t or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return {"content": payload, "headers": {"Stripe-Signature": f"t={t},v1={sig}"}}


def _start_payment(client, made):
    order_id = made["order_id"]
    k = made["checkout_url"].split("k=")[1]
    page = client.get(f"/pay/{order_id}", params={"k": k})
    assert page.status_code == 200 and "js.stripe.com/v3" in page.text and order_id in page.text
    s = client.post(f"/api/pay/{order_id}/session", json={"k": k})
    assert s.status_code == 200 and s.json()["client_secret"].startswith("cs_test_")
    return order_id, k, s.json()["client_secret"].split("_secret")[0]


def test_checkout_makes_our_own_order_and_a_pay_page(client, stripe, main_module):
    made = _checkout(client)
    assert made["order_id"] == "CI-1001" and made["checkout_url"].startswith(f"{main_module.SITE_URL}/pay/CI-1001?k=")
    order = main_module._load_online_order("CI-1001")
    assert order["status"] == "pending" and order["total"] == f"{made['total']:.2f}"
    assert order["billing"]["email"] == "ann@example.com" and order["line_items"]
    assert _checkout(client)["order_id"] == "CI-1002"                       # numbers go up
    # The session charges exactly the order's total, in cents
    _, _, sid = _start_payment(client, made)
    assert stripe.sessions[sid]["amount_total"] == round(made["total"] * 100)


def test_the_pay_page_needs_the_orders_key(client, stripe):
    made = _checkout(client)
    assert client.get(f"/pay/{made['order_id']}", params={"k": "wrong"}).status_code == 404
    assert client.post(f"/api/pay/{made['order_id']}/session", json={"k": "wrong"}).status_code == 404


def test_a_paid_webhook_delivers_once(client, stripe, main_module):
    made = _checkout(client)
    order_id, k, sid = _start_payment(client, made)
    event = {"type": "checkout.session.completed", "livemode": False, "data": {"object": stripe.pay(sid)}}
    assert client.post("/stripe/webhook", **_signed(event)).status_code == 200
    assert client.post("/stripe/webhook", **_signed(event)).status_code == 200          # sent twice: one delivery
    order = main_module._load_online_order(order_id)
    assert order["status"] == "completed" and order["download_token"]
    assert [e[0] for e in stripe.emails] == [order_id]
    rec = main_module._load_token(order["download_token"])
    assert rec["paths"] == ALBUM["paths"] and rec["email"] == "ann@example.com"
    # The thank-you page then shows the download right there
    page = client.get(f"/order/{order_id}", params={"k": k})
    assert page.status_code == 200 and f"/download/{order['download_token']}" in page.text


def test_the_thank_you_page_delivers_even_before_the_webhook(client, stripe, main_module):
    made = _checkout(client)
    order_id, k, sid = _start_payment(client, made)
    stripe.pay(sid)
    r = client.get("/order/complete", params={"session_id": sid}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/order/{order_id}?k={k}"
    assert main_module._load_online_order(order_id)["status"] == "completed" and len(stripe.emails) == 1
    # Unpaid sessions don't deliver
    other = _checkout(client)
    _, _, sid2 = _start_payment(client, other)
    assert "being confirmed" in client.get("/order/complete", params={"session_id": sid2}).text
    assert main_module._load_online_order(other["order_id"])["status"] == "pending"


def test_a_wrong_amount_is_never_delivered(client, stripe, main_module):
    made = _checkout(client)
    order_id, _, sid = _start_payment(client, made)
    paid = stripe.pay(sid)
    paid["amount_total"] = 100
    client.post("/stripe/webhook", **_signed({"type": "checkout.session.completed", "livemode": False, "data": {"object": paid}}))
    order = main_module._load_online_order(order_id)
    assert order["status"] == "check" and not order.get("download_token") and stripe.emails == []


def test_webhooks_must_be_signed_by_stripe(client, stripe, main_module):
    made = _checkout(client)
    _, _, sid = _start_payment(client, made)
    event = {"type": "checkout.session.completed", "livemode": False, "data": {"object": stripe.pay(sid)}}
    assert client.post("/stripe/webhook", **_signed(event, secret="whsec_wrong")).status_code == 400
    assert client.post("/stripe/webhook", **_signed(event, t=int(time.time()) - 3600)).status_code == 400   # old copy
    assert client.post("/stripe/webhook", content=json.dumps(event)).status_code == 400
    assert main_module._load_online_order(made["order_id"])["status"] == "pending"


def test_a_stripe_refund_shows_on_the_order_and_ends_the_link(client, stripe, main_module):
    made = _checkout(client)
    order_id, _, sid = _start_payment(client, made)
    client.post("/stripe/webhook", **_signed({"type": "checkout.session.completed", "livemode": False,
                                             "data": {"object": stripe.pay(sid)}}))
    token = main_module._load_online_order(order_id)["download_token"]
    cents = round(made["total"] * 100)
    charge = {"payment_intent": f"pi_{sid}", "amount_refunded": cents, "refunded": True, "metadata": {}}
    client.post("/stripe/webhook", **_signed({"type": "charge.refunded", "livemode": False, "data": {"object": charge}}))
    order = main_module._load_online_order(order_id)
    assert order["status"] == "refunded" and order["refunded"] == f"{made['total']:.2f}"
    assert main_module._load_order_refunds()[f"wc:{order_id}"]["full"] is True
    assert client.get(f"/download/{token}").status_code in (404, 410)


def test_paid_stripe_orders_show_with_the_online_orders(client, stripe, main_module):
    made = _checkout(client)
    _checkout(client)                                                       # left unpaid
    _, _, sid = _start_payment(client, made)
    client.post("/stripe/webhook", **_signed({"type": "checkout.session.completed", "livemode": False,
                                             "data": {"object": stripe.pay(sid)}}))
    orders = main_module._fetch_online_orders()
    assert [o["id"] for o in orders] == [made["order_id"]]
    row = main_module._order_row(orders[0])
    assert row["order_no"] == made["order_id"] and row["email"] == "ann@example.com" and row["total"] == made["total"]
    assert row["provider"] == "stripe"


def test_test_mode_messages_are_ignored_on_the_live_keys(client, stripe, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "STRIPE_SECRET_KEY", "sk_live_x")
    made = _checkout(client)
    _, _, sid = _start_payment(client, made)
    client.post("/stripe/webhook", **_signed({"type": "checkout.session.completed", "livemode": False,
                                             "data": {"object": stripe.pay(sid)}}))
    assert main_module._load_online_order(made["order_id"])["status"] == "pending"


def test_the_switch(admin_client, owner_client, client, stripe, main_module, monkeypatch):
    s = admin_client.get("/api/admin/checkout-settings").json()
    assert admin_client.put("/api/admin/checkout-settings", json={"provider": "woo"}).status_code == 403   # owner PIN
    assert s["provider"] == "stripe" and s["stripe"]["mode"] == "test" and s["webhook_url"].endswith("/stripe/webhook")
    assert owner_client.put("/api/admin/checkout-settings", json={"provider": "woo"}).json()["provider"] == "woo"
    assert main_module._checkout_provider() == "woo"
    monkeypatch.setattr(main_module, "STRIPE_WEBHOOK_SECRET", "")
    assert owner_client.put("/api/admin/checkout-settings", json={"provider": "stripe"}).status_code == 400
    assert client.get("/api/admin/checkout-settings").status_code == 401


def test_a_test_order_from_admin_payments(admin_client, owner_client, client, stripe, main_module):
    main_module._checkout_settings_doc.save({"provider": "woo"})            # works whatever the switch says
    r = owner_client.post("/api/admin/stripe/test-order")
    assert r.status_code == 200 and "/pay/CI-" in r.json()["checkout_url"]
    order = main_module._load_online_order(r.json()["order_id"])
    assert order["total"] == "1.00" and order["source"] == "test"
    assert client.post("/api/admin/stripe/test-order").status_code == 401
    assert admin_client.post("/api/admin/stripe/test-order").status_code == 403


def test_stripe_signatures_and_fields():
    payload = b'{"a":1}'
    sig = hmac.new(b"s", b"100." + payload, hashlib.sha256).hexdigest()
    assert stripe_api.verify_webhook(payload, f"t=100,v1={sig}", "s", now=150)
    assert not stripe_api.verify_webhook(payload, f"t=100,v1={sig}", "s", now=1000)
    assert not stripe_api.verify_webhook(payload, f"t=100,v1={sig}", "other", now=150)
    assert stripe_api.form_fields({"a": {"b": [1, {"c": True}]}, "d": None}) == [("a[b][0]", "1"), ("a[b][1][c]", "true")]
    assert stripe_api.mode_of("sk_live_1") == "live" and stripe_api.mode_of("pk_test_1") == "test"


def test_invoices_use_the_stripe_pay_page_and_can_be_cancelled(main_module, stripe, client):
    made = _checkout(client)
    assert main_module._cancel_invoice(made["order_id"]) == ""
    assert main_module._load_online_order(made["order_id"])["status"] == "cancelled"
    k = made["checkout_url"].split("k=")[1]
    page = client.get(f"/pay/{made['order_id']}", params={"k": k})
    assert "cancelled" in page.text and "js.stripe.com" not in page.text
    paid = _checkout(client)
    _, _, sid = _start_payment(client, paid)
    client.post("/stripe/webhook", **_signed({"type": "checkout.session.completed", "livemode": False,
                                             "data": {"object": stripe.pay(sid)}}))
    assert "already paid" in main_module._cancel_invoice(paid["order_id"])


def _paid_order(client, stripe, **extra):
    made = _checkout(client, **extra)
    _, _, sid = _start_payment(client, made)
    client.post("/stripe/webhook", **_signed({"type": "checkout.session.completed", "livemode": False,
                                             "data": {"object": stripe.pay(sid)}}))
    return made


def test_studio_pages_show_stripe_orders(client, admin_client, owner_client, stripe, main_module):
    """Orders, the spreadsheet export, Reports, a customer's history and the customer filter all
    include our own online orders, and Copy Link finds the order's download."""
    S3.put_object(Bucket=BUCKET, Key="meta/photographers.json", Body=json.dumps([
        {"id": "p1", "name": "Dana Shooter", "pin": "1111", "file_prefix": "A.j"}]).encode())
    made = _paid_order(client, stripe)
    oid = made["order_id"]
    row = next(r for r in admin_client.get("/api/admin/sales?days=7").json()["rows"] if r["order_id"] == oid)
    assert row["channel"] == "online" and row["provider"] == "stripe" and row["order_no"] == oid and row["link"] == "copy"
    assert row["test"] and row["photographer"] == "Dana Shooter" and row["items"][0]["cat"] == "digital"
    assert oid in admin_client.get("/admin/export?days=7").text
    rep = admin_client.get("/api/admin/reports?days=7").json()
    assert rep["summary"]["orders"] == 1 and rep["summary"]["revenue"] == made["total"]
    link = admin_client.get(f"/api/admin/order-link/{oid}").json()
    assert link["url"].startswith(f"{main_module.SITE_URL}/download/") and not link["expired"]
    assert admin_client.get("/api/admin/order-link/CI-1001x").status_code == 404
    hist = admin_client.get("/api/admin/customer/ann@example.com/history").json()
    assert [o["id"] for o in hist["orders"]] == [oid] and hist["orders"][0]["source"] == "stripe"
    assert hist["orders"][0]["gallery_url"].startswith("/download/")
    found = admin_client.get("/api/admin/customers/filtered?types=digital").json()["customers"]
    assert [c["email"] for c in found] == ["ann@example.com"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
    rows = owner_client.get(f"/api/photographers/commission?date_from={today}&date_to={today}").json()["commission"]
    assert next(p for p in rows if p["id"] == "p1")["revenue"] == made["total"]
    S3.delete_object(Bucket=BUCKET, Key="meta/photographers.json")


def test_test_mode_orders_drop_out_once_live(client, stripe, main_module, monkeypatch):
    made = _paid_order(client, stripe)
    assert main_module._load_online_order(made["order_id"])["stripe"]["mode"] == "test"
    assert [o["id"] for o in main_module._fetch_online_orders()] == [made["order_id"]]
    monkeypatch.setattr(main_module, "STRIPE_SECRET_KEY", "sk_live_x")
    assert main_module._fetch_online_orders() == []


def test_discount_codes_stay_here_while_on_stripe_and_go_back_to_woocommerce(admin_client, owner_client, stripe, main_module, monkeypatch):
    synced = []
    monkeypatch.setattr(main_module, "_wc_sync_coupon", lambda c: synced.append(c["code"]) or {"wc_id": 77})
    S3.put_object(Bucket=BUCKET, Key="meta/discount_codes.json", Body=b"[]")
    r = admin_client.post("/api/admin/discount-codes", json={"code": "WINTER10", "type": "percent", "amount": 10})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert synced == [] and code.get("wc_stale") and not code.get("wc_sync_error")
    monkeypatch.setattr(main_module, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    back = owner_client.put("/api/admin/checkout-settings", json={"provider": "woo"})
    assert back.json()["codes_copied"] == 1 and synced == ["WINTER10"]
    saved = json.loads(S3.get_object(Bucket=BUCKET, Key="meta/discount_codes.json")["Body"].read())
    assert saved[0]["wc_id"] == 77 and "wc_stale" not in saved[0]
    S3.delete_object(Bucket=BUCKET, Key="meta/discount_codes.json")


def test_paid_stripe_order_logs_partner_prints_once(client, stripe, main_module, monkeypatch):
    logged = []
    monkeypatch.setattr(main_module, "_log_partner_online_prints", lambda order: logged.append(order["id"]))
    made = _paid_order(client, stripe)
    main_module._complete_online_order(made["order_id"])          # the thank-you page after the webhook
    assert logged == [made["order_id"]]                           # same Eagle Mount print log as WooCommerce orders


def test_online_buyers_join_the_customer_list(client, stripe, main_module):
    with main_module._customers_lock:
        before = [c for c in main_module._load_customers() if c.get("email") != "ann@example.com"]
        main_module._save_customers(before)
    _paid_order(client, stripe, billing={"first_name": "Ann", "last_name": "Smith", "phone": "406-555-0101",
                                         "address_1": "1 Lone Mountain Trl", "city": "Big Sky", "state": "MT", "postcode": "59716"})
    ann = main_module._customer_by_email(main_module._load_customers(), "ann@example.com")
    assert ann and ann["first"] == "Ann" and ann["source"] == "online" and ann["zip"] == "59716" and ann["phone"] == "406-555-0101"
    main_module._add_online_customer({"billing": {"email": "ann@example.com", "first_name": "Annie", "city": "Bozeman"}})
    again = main_module._customer_by_email(main_module._load_customers(), "ann@example.com")
    assert again["first"] == "Ann" and again["city"] == "Big Sky"          # nothing on file is overwritten
    assert len([c for c in main_module._load_customers() if c.get("email") == "ann@example.com"]) == 1
