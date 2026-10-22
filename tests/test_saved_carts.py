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
    monkeypatch.setattr(main_module, "_create_checkout", lambda b, **kw: made.append(b) or {"checkout_url": "https://pay/x", "total": 61.0})
    monkeypatch.setattr(main_module, "_send_invoice_email", lambda *a: mailed.append(a) or True)
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={"first_name": "Sarah", "last_name": "Smith"})
    assert r.status_code == 400 and "ZIP" in r.json()["error"]                 # prints need an address
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={
        "first_name": "Sarah", "last_name": "Smith", "address_1": "1 Lone Peak Dr", "city": "Big Sky", "state": "MT", "postcode": "59716"})
    assert r.json()["pay_url"] == "https://pay/x" and r.json()["total"] == 61.0 and r.json()["emailed"]
    assert made[0]["email"] == "sarah.smith@example.com" and made[0]["shipping"]["city"] == "Big Sky"
    assert mailed[0][2] == "https://pay/x"
    assert client.post(f"/api/admin/saved-carts/{cid}/invoice", json={}).status_code == 401


def test_invoice_custom_price_adds_a_price_adjustment(admin_client, client, main_module, monkeypatch):
    sent = []
    monkeypatch.setattr(main_module, "_price_order", lambda b, pp=None: {
        "line_items": [{"name": "Digital Photos", "total": "75.00"}], "fee_lines": [], "coupon_lines": [], "meta": [],
        "summary": {"total": 75.0, "shipping": 0}})

    class _Resp:
        status_code = 201
        def json(self):
            return {"id": 5, "order_key": "k"}
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: sent.append(json) or _Resp())
    monkeypatch.setattr(main_module, "_send_invoice_email", lambda *a: True)
    _save(client, cart=_cart(prints=0))
    cid = main_module._saved_cart_id(EMAIL)
    r = admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={"custom_price": "60"})
    assert r.json()["total"] == 60.0 and r.json()["normal_total"] == 75.0 and r.json()["custom"]
    assert {"name": "Price adjustment", "total": "-15.00"} in sent[0]["fee_lines"]
    assert admin_client.post(f"/api/admin/saved-carts/{cid}/invoice", json={"custom_price": "abc"}).status_code == 400


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
