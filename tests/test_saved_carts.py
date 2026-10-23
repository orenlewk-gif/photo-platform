"""Saved carts: customers save their gallery selection (first name + email) and load it anywhere;
the studio lists them; a paid order empties it; the studio can clear them all at season's end."""
import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
EMAIL = "Sarah.Smith@Example.com"


def _cart(n=2, prints=1):
    return {"ts": 1, "frameCart": [], "currentFolderKey": "k1", "digitalLogoPaths": [],
            "folderCarts": {"k1": {
                "selectedPaths": [f"images/2026-12-05/ski/DSC_{i}.JPG" for i in range(n)],
                "selectedPhotos": [], "label": "Big Sky Ski School — Dec 5", "location": "Big Sky Ski School",
                "printCart": [{"photoPath": "images/2026-12-05/ski/DSC_0.JPG", "size": '8×10"', "frame": "No Frame"}] * prints,
                "poses": [{"color": "red", "label": "Pose 1", "paths": ["images/p/a.jpg", "images/p/b.jpg"]}],
                "poseState": {"red": [1]}}}}


@pytest.fixture(autouse=True)
def fresh(main_module):
    for o in S3.list_objects_v2(Bucket="test-bucket", Prefix="saved_carts/").get("Contents", []):
        S3.delete_object(Bucket="test-bucket", Key=o["Key"])
    main_module._saved_carts_cache["records"] = None
    main_module._saved_cart_save_limiter._calls.clear()
    main_module._saved_cart_load_limiter._calls.clear()
    yield
    main_module._saved_carts_cache["records"] = None


def _save(client, cart=None, email=EMAIL, first="Sarah"):
    return client.post("/api/saved-cart", json={"email": email, "first_name": first, "cart": cart or _cart()})


def test_save_and_load_on_another_device(client, main_module):
    r = _save(client)
    assert r.status_code == 200 and r.json()["items"] == 4          # 2 photos + 1 pose photo + 1 print
    other = client.post("/api/saved-cart/load", json={"email": "sarah.smith@example.com"})   # any capitals
    assert other.status_code == 200 and other.json()["first_name"] == "Sarah"
    assert other.json()["cart"]["folderCarts"]["k1"]["selectedPaths"][0] == "images/2026-12-05/ski/DSC_0.JPG"


def test_saving_again_updates_and_empty_removes(client):
    _save(client)
    _save(client, cart=_cart(n=5, prints=0))
    assert client.post("/api/saved-cart/load", json={"email": EMAIL}).json()["cart"]["folderCarts"]["k1"]["selectedPaths"][4]
    empty = {"ts": 1, "frameCart": [], "folderCarts": {}}
    assert _save(client, cart=empty).json()["items"] == 0
    assert client.post("/api/saved-cart/load", json={"email": EMAIL}).status_code == 404


@pytest.mark.parametrize("body,msg", [
    ({"email": "nope", "first_name": "S", "cart": {}}, "valid email"),
    ({"email": EMAIL, "first_name": " ", "cart": {}}, "first name"),
    ({"email": EMAIL, "first_name": "S", "cart": "x"}, "Nothing to save"),
])
def test_bad_saves_are_refused(client, body, msg):
    r = client.post("/api/saved-cart", json=body)
    assert r.status_code == 400 and msg in r.json()["error"]


def test_huge_cart_refused(client):
    big = _cart()
    big["folderCarts"]["k1"]["selectedPhotos"] = ["x" * 1000] * 500
    assert _save(client, cart=big).status_code == 413


def test_loading_is_limited_so_emails_cant_be_guessed(client, main_module):
    for i in range(main_module._saved_cart_load_limiter.max_calls):
        client.post("/api/saved-cart/load", json={"email": f"guess{i}@example.com"})
    assert client.post("/api/saved-cart/load", json={"email": EMAIL}).status_code == 429


def test_studio_list_detail_delete(admin_client, client):
    _save(client)
    _save(client, email="bo@example.com", first="Bo", cart=_cart(n=1, prints=0))
    rows = admin_client.get("/api/admin/saved-carts").json()["carts"]
    assert {r["first_name"]: r["items"] for r in rows} == {"Sarah": 4, "Bo": 2}
    sarah = next(r for r in rows if r["first_name"] == "Sarah")
    d = admin_client.get(f"/api/admin/saved-carts/{sarah['id']}").json()
    f = d["folders"][0]
    assert d["email"] == "sarah.smith@example.com" and len(f["photos"]) == 2
    assert f["poses"] == [{"label": "Pose 1", "whole": False, "paths": ["images/p/b.jpg"]}]
    assert f["prints"][0]["size"] == '8×10"'
    assert client.get("/api/admin/saved-carts").status_code == 401
    assert admin_client.delete(f"/api/admin/saved-carts/{sarah['id']}").json()["deleted"]
    assert [r["first_name"] for r in admin_client.get("/api/admin/saved-carts").json()["carts"]] == ["Bo"]


def test_clear_all_at_seasons_end(admin_client, client):
    _save(client); _save(client, email="bo@example.com", first="Bo")
    assert admin_client.post("/api/admin/saved-carts/clear-all", json={"confirm": "yes"}).status_code == 400
    assert admin_client.post("/api/admin/saved-carts/clear-all", json={"confirm": "CLEAR"}).json()["cleared"] == 2
    assert admin_client.get("/api/admin/saved-carts").json()["carts"] == []


def test_a_paid_order_empties_their_saved_cart(client, main_module):
    _save(client)
    main_module._handle_paid_wc_order({"id": 9, "status": "processing", "billing": {"email": "SARAH.smith@example.com"},
                                       "meta_data": []})
    assert client.post("/api/saved-cart/load", json={"email": EMAIL}).status_code == 404


def test_pos_finds_the_saved_cart_by_email(client, main_module):
    import json as _json
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json",
                  Body=_json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    cart = _cart()
    cart["folderCarts"] = {"2026-12-05||Big Sky Ski School": cart["folderCarts"]["k1"]}
    _save(client, cart=cart)
    r = client.get("/api/pos/saved-cart", params={"email": "sarah.smith@example.com"}, headers={"X-POS-PIN": "4821"})
    assert r.status_code == 200
    f = r.json()["folders"][0]
    assert (f["date"], f["location"], f["family"]) == ("2026-12-05", "Big Sky Ski School", "")
    assert len(f["photos"]) == 2 and f["prints"][0]["size"] == '8×10"'
    assert client.get("/api/pos/saved-cart", params={"email": "sarah.smith@example.com"}).status_code == 401
    assert client.get("/api/pos/saved-cart", params={"email": "nobody@example.com"},
                      headers={"X-POS-PIN": "4821"}).status_code == 404


def test_pos_saved_online_search_by_name_or_email(client, main_module):
    import json as _json
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json",
                  Body=_json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    _save(client)
    _save(client, email="tom@example.com", first="Tom")
    pin = {"X-POS-PIN": "4821"}
    assert client.get("/api/pos/saved-carts", params={"q": "sarah"}).status_code == 401
    names = lambda q: [c["first_name"] for c in client.get("/api/pos/saved-carts", params={"q": q}, headers=pin).json()["carts"]]
    assert names("SARAH") == ["Sarah"] and names("tom@ex") == ["Tom"] and sorted(names("")) == ["Sarah", "Tom"]
    c = client.get("/api/pos/saved-carts", params={"q": "sarah"}, headers=pin).json()["carts"][0]
    assert c["email"] == "sarah.smith@example.com" and c["summary"] == {"photos": 3, "poses": 0, "prints": 1, "frames": 0}   # a single pose photo counts as a photo


def test_an_in_store_sale_empties_their_saved_cart(client, main_module):
    import json as _json
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json",
                  Body=_json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    _save(client)
    r = client.post("/api/pos-checkout", json={"pin": "4821", "email": "sarah.smith@example.com", "name": "Sarah Smith",
                                               "items": [], "total": 0, "square_payment_id": "COMP"})
    assert r.status_code == 200
    assert client.post("/api/saved-cart/load", json={"email": EMAIL}).status_code == 404


def test_invoice_from_a_saved_cart(admin_client, client, main_module, monkeypatch):
    cart = _cart()
    cart["folderCarts"] = {"2026-12-05||Big Sky Ski School": cart["folderCarts"]["k1"]}
    _save(client, cart=cart)
    cid = main_module._saved_cart_id(EMAIL)
    body = main_module._saved_cart_order_body(main_module._load_saved_cart(cid))
    assert body["digital_albums"][0]["count"] == 2 and body["digital_albums"][0]["date"] == "2026-12-05"
    assert body["prints"][0]["path"] == "images/2026-12-05/ski/DSC_0.JPG"
    assert body["portrait_singles"][0]["path"] == "images/p/b.jpg"
    made, mailed = [], []
    monkeypatch.setattr(main_module, "_create_checkout", lambda b, **kw: made.append(b) or {
        "checkout_url": "https://pay/x", "total": 61.0, "normal_total": 61.0, "order_id": 77})
    monkeypatch.setattr(main_module, "_send_invoice_email", lambda *a: mailed.append(a) or True)
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={"first_name": "Sarah", "last_name": "Smith"})
    assert r.status_code == 400 and "ZIP" in r.json()["error"]                 # prints need an address
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={
        "first_name": "Sarah", "last_name": "Smith", "address_1": "1 Lone Peak Dr", "city": "Big Sky", "state": "MT", "postcode": "59716"})
    assert r.json()["pay_url"] == "https://pay/x" and r.json()["total"] == 61.0 and r.json()["emailed"]
    assert made[0]["email"] == "sarah.smith@example.com" and made[0]["shipping"]["city"] == "Big Sky"
    assert mailed[0][2] == "https://pay/x"
    assert client.post(f"/api/admin/saved-carts/{cid}/invoice", json={}).status_code == 401


class _WC:
    """A fake WooCommerce: orders created, read and cancelled."""
    def __init__(self, main_module, monkeypatch):
        self.orders, self.sent = {}, []
        wc = self

        class Resp:
            def __init__(self, code, data):
                self.status_code, self._data, self.text = code, data, ""
                self.ok = code < 400
            def json(self):
                return self._data

        def post(url, json=None, **kw):
            wc.sent.append(json)
            oid = 100 + len(wc.orders)
            wc.orders[oid] = "pending"
            return Resp(201, {"id": oid, "order_key": "k"})

        def get(url, params=None, **kw):
            if url.endswith("/orders"):
                return Resp(200, [{"id": i, "status": st} for i, st in wc.orders.items()])
            return Resp(200, {"status": wc.orders[int(url.rsplit("/", 1)[1])]})

        def put(url, json=None, **kw):
            wc.orders[int(url.rsplit("/", 1)[1])] = json["status"]
            return Resp(200, {})
        monkeypatch.setattr(main_module.http_requests, "post", post)
        monkeypatch.setattr(main_module.http_requests, "get", get)
        monkeypatch.setattr(main_module.http_requests, "put", put)


def _priced(monkeypatch, main_module):
    monkeypatch.setattr(main_module, "_price_order", lambda b, pp=None: {
        "line_items": [{"product_id": 1152, "name": "Digital Photos — Ski", "total": "50.00", "subtotal": "50.00"},
                       {"product_id": 1156, "name": "8x10 Print — DSC_0.JPG", "total": "25.00", "subtotal": "25.00"}],
        "fee_lines": [{"name": "Shipping", "total": "8.00"}], "coupon_lines": [], "meta": [],
        "summary": {"total": 83.0, "shipping": 8.0}})
    monkeypatch.setattr(main_module, "_send_invoice_email", lambda *a: True)


ADDR = {"address_1": "1 Lone Peak Dr", "city": "Big Sky", "state": "MT", "postcode": "59716"}


def test_invoice_is_itemized_with_editable_prices_and_added_items(admin_client, client, main_module, monkeypatch):
    for o in S3.list_objects_v2(Bucket="test-bucket", Prefix="invoices/").get("Contents", []):
        S3.delete_object(Bucket="test-bucket", Key=o["Key"])
    _priced(monkeypatch, main_module)
    wc = _WC(main_module, monkeypatch)
    _save(client)
    cid = main_module._saved_cart_id(EMAIL)
    form = admin_client.get(f"/api/admin/saved-carts/{cid}/invoice-lines").json()
    assert form["lines"] == [{"name": "Digital Photos — Ski", "price": 50.0}, {"name": "8x10 Print — DSC_0.JPG", "price": 25.0}]
    assert form["shipping"] == 8.0 and form["ships"]
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={
        **ADDR, "prices": [40, 0], "extra": [{"name": "Photo retouching", "price": "15"}], "shipping": "5"})
    assert r.status_code == 200 and r.json()["total"] == 60.0 and r.json()["normal_total"] == 83.0
    order = wc.sent[0]
    # every item at its invoice price, the added item, the new shipping, and no adjustment line
    assert [(ln["name"], ln["total"]) for ln in order["line_items"]] == [
        ("Digital Photos — Ski", "40.00"), ("8x10 Print — DSC_0.JPG", "0.00"), ("Photo retouching", "15.00")]
    assert order["fee_lines"] == [{"name": "Shipping", "total": "5.00"}]
    assert {"key": "_normal_price", "value": "83.00"} in order["meta_data"]
    bad = lambda body: admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={**ADDR, **body})
    assert bad({"prices": [40]}).status_code == 400                       # the cart changed since the form opened
    assert bad({"prices": ["abc", 1]}).status_code == 400
    assert bad({"extra": [{"name": "", "price": 5}]}).status_code == 400
    # the Invoices list shows it with its status, and an unpaid one can be cancelled
    inv = admin_client.get("/api/admin/invoices").json()["invoices"]
    assert [(i["first_name"], i["total"], i["status"]) for i in inv] == [("Sarah", 60.0, "Waiting for payment")]
    oid = inv[0]["order_id"]
    assert client.post(f"/api/admin/invoices/{oid}/cancel").status_code == 401
    assert admin_client.post(f"/api/admin/invoices/{oid}/cancel").json() == {"cancelled": True}
    assert admin_client.get("/api/admin/invoices").json()["invoices"][0]["status"] == "Cancelled"
    wc.orders[oid] = "processing"                                         # paid: can't be cancelled
    assert admin_client.post(f"/api/admin/invoices/{oid}/cancel").status_code == 400


def test_invoice_at_normal_prices_sends_the_cart_unchanged(admin_client, client, main_module, monkeypatch):
    _priced(monkeypatch, main_module)
    wc = _WC(main_module, monkeypatch)
    _save(client)
    r = admin_client.post(f"/api/admin/saved-carts/{main_module._saved_cart_id(EMAIL)}/invoice", json=ADDR)
    assert r.json()["total"] == 83.0
    assert [ln["total"] for ln in wc.sent[0]["line_items"]] == ["50.00", "25.00"]
    assert not any(m["key"] == "_normal_price" for m in wc.sent[0]["meta_data"])


def test_reminder_email_and_its_link(admin_client, client, main_module, monkeypatch):
    sent = []

    class _Ok:
        ok = True
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: sent.append(json) or _Ok())
    _save(client)
    cid = main_module._saved_cart_id(EMAIL)
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/remind")
    assert r.status_code == 200 and len(r.json()["reminders"]) == 1
    mail = sent[0]
    assert mail["to"] == ["sarah.smith@example.com"] and "List-Unsubscribe" in mail["headers"]
    assert "/unsubscribe/" in mail["html"] and "View your photos" in mail["html"]
    assert "<img" not in mail["html"] and "/api/photo" not in mail["html"]      # no photos in the email
    assert "because you saved photos on photos.bigskyphotos.com" in mail["html"]
    token = mail["html"].split("?saved=")[1].split('"')[0]
    opened = client.get("/api/saved-cart/link", params={"t": token})
    assert opened.status_code == 200 and opened.json()["first_name"] == "Sarah"
    assert client.get("/api/saved-cart/link", params={"t": token[:-1] + "0"}).status_code == 404   # forged link
    assert admin_client.get(f"/api/admin/saved-carts/{cid}").json()["reminders"]
    _save(client)                                                            # re-saving keeps the history
    assert admin_client.get(f"/api/admin/saved-carts/{cid}").json()["reminders"]
    assert client.post(f"/api/admin/saved-carts/{cid}/remind").status_code == 401


def test_no_reminder_to_someone_who_unsubscribed(admin_client, client, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(main_module, "_opted_out", lambda e: {"how": "link"})
    _save(client)
    r = admin_client.post(f"/api/admin/saved-carts/{main_module._saved_cart_id(EMAIL)}/remind")
    assert r.status_code == 400 and "unsubscribed" in r.json()["error"]


def test_bulk_reminders_and_delete(admin_client, client, main_module, monkeypatch):
    class _Ok:
        ok = True
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: _Ok())
    monkeypatch.setattr(main_module, "_opted_out", lambda e: {"how": "link"} if e.startswith("bo@") else None)
    _save(client); _save(client, email="bo@example.com", first="Bo")
    ids = [r["id"] for r in admin_client.get("/api/admin/saved-carts").json()["carts"]]
    d = admin_client.post("/api/admin/saved-carts/bulk", json={"action": "remind", "ids": ids}).json()
    assert d["sent"] == 1 and d["skipped"][0]["email"] == "bo@example.com"
    d = admin_client.post("/api/admin/saved-carts/bulk", json={"action": "delete", "ids": ids}).json()
    assert d["deleted"] == 2 and admin_client.get("/api/admin/saved-carts").json()["carts"] == []
    assert admin_client.post("/api/admin/saved-carts/bulk", json={"action": "nope", "ids": ids}).status_code == 400
    assert client.post("/api/admin/saved-carts/bulk", json={"action": "delete", "ids": ids}).status_code == 401
