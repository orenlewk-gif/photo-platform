"""Receipts show what Square really charged (tax + tip); the Orders tab lists in-person sales with tips."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
CREATED = datetime.now(timezone.utc) - timedelta(minutes=30)   # sales made a little while ago
TODAY = CREATED.strftime("%Y-%m-%d")                              # their (UTC) file day
MT_TODAY = CREATED.astimezone(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")


def _sales(admin_client):
    r = admin_client.get("/api/admin/sales?days=7")
    assert r.status_code == 200
    return r.json()["rows"]


def _sale(admin_client, name):
    return next(r for r in _sales(admin_client) if r["first"] == name)


class _Resp:
    ok, status_code, text = True, 200, "{}"


@pytest.fixture()
def sent(main_module, monkeypatch):
    emails = []
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: emails.append(json) or _Resp())
    return emails


ITEMS = [{"label": "8x10 Print", "qty": 1, "price": 25.0, "subtotal": 25.0}]


def test_receipt_uses_square_tax_tip_and_total(main_module, sent):
    main_module._send_pos_receipt_email("ann@example.com", "Ann", "pos-1", ITEMS, 25.0, "", 0, None,
                                        square={"square_paid": 31.0, "square_tax": 1.0, "square_tip": 5.0})
    html = sent[0]["html"]
    assert "Tip</td>" in html and "$5.00" in html
    assert "$1.00" in html and "Total Charged" in html and "$31.00" in html


def test_receipt_without_tip_has_no_tip_line(main_module, sent):
    main_module._send_pos_receipt_email("ann@example.com", "Ann", "pos-1", ITEMS, 25.0, "", 0, None,
                                        square={"square_paid": 26.0, "square_tax": 1.0, "square_tip": 0})
    assert "Tip</td>" not in sent[0]["html"] and "$26.00" in sent[0]["html"]


def test_comp_receipt_says_no_charge(main_module, sent):
    main_module._send_pos_receipt_email("ann@example.com", "Ann", "pos-1", ITEMS, 25.0, "", 0, None, comp=True)
    html = sent[0]["html"]
    assert "Complimentary" in html and "No charge" in html
    assert "Total Charged" not in html and "Resort Tax" not in html


# ── Orders tab ───────────────────────────────────────────────────────────────

@pytest.fixture()
def pos_orders(main_module):
    for o in S3.list_objects_v2(Bucket="test-bucket", Prefix="pos_orders/").get("Contents", []):
        S3.delete_object(Bucket="test-bucket", Key=o["Key"])
    orders = [
        {"id": "pos-tip1", "date": TODAY, "created": CREATED.isoformat(), "name": "Ann Smith",
         "email": "ann@example.com", "location": "Mountain Biking", "total": 25.0, "coupon_discount": 0,
         "square_payment_id": "ORDER1", "square_verified": True, "square_paid": 31.0, "square_tax": 1.0,
         "square_tip": 5.0, "photographer_name": "Test Photog", "package_token": "abc123", "filenames": "IMG_1.jpg"},
        {"id": "pos-comp1", "date": TODAY, "created": CREATED.isoformat(), "name": "Bob Jones",
         "email": "bob@example.com", "location": "Base Area", "total": 40.0, "coupon_discount": 0,
         "square_payment_id": "COMP", "photographer_name": "Test Photog"},
    ]
    for o in orders:
        S3.put_object(Bucket="test-bucket", Key=f"pos_orders/{o['date']}_{o['id']}.json", Body=json.dumps(o).encode())
    return orders


def test_orders_tab_lists_in_person_sales_with_tips(admin_client, pos_orders):
    assert "Orders" in admin_client.get("/admin/orders").text
    rows = _sales(admin_client)
    ann = next(r for r in rows if r["first"] == "Ann")
    assert ann["channel"] == "store" and ann["location"] == "Mountain Biking" and ann["date"] == MT_TODAY
    assert ann["charged"] == 31.0 and ann["tip"] == 5.0 and ann["tax"] == 1.0   # whole amount charged, tip, tax
    assert ann["link"] == "package" and ann["package_token"] == "abc123"
    bob = next(r for r in rows if r["first"] == "Bob")
    assert bob["is_comp"] and bob["charged"] == 0 and bob["provider"] == "comp"
    assert sum(r["charged"] for r in rows) == 31.0                              # comp adds nothing


def test_csv_export_includes_in_person_sales_and_tip(admin_client, pos_orders):
    csv_text = admin_client.get("/admin/export?days=7").text
    header, *lines = csv_text.strip().splitlines()
    assert ",Total,Tip," in header
    ann = next(l for l in lines if "Ann" in l)
    assert "$31.00,$5.00" in ann
    assert any("Bob" in l and ",Comp," in l for l in lines)


def test_download_button_survives_a_template_edit_that_drops_it(main_module, sent, monkeypatch):
    edited = {"pos_receipt": {**main_module._EMAIL_TEMPLATE_DEFAULTS["pos_receipt"],
                              "body": "Hi {{first_name}},\n\n{{items_table}}\n\nThanks!"}}
    monkeypatch.setattr(main_module, "_load_email_templates", lambda: edited)
    main_module._send_pos_receipt_email("ann@example.com", "Ann", "pos-1", ITEMS, 25.0, "", 0,
                                        "https://photos.example.com/package/tok1", expire_str="November 16, 2026")
    html = sent[0]["html"]
    assert "/package/tok1" in html and "available until November 16, 2026" in html


def test_in_person_sales_credit_the_photographer_by_jpeg_code_not_the_seller(admin_client, pos_orders):
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json", Body=json.dumps([
        {"id": "p1", "name": "Owner Seller", "pin": "1111"},
        {"id": "p2", "name": "Dana Shooter", "pin": "2222", "file_prefix": "dsc"}]).encode())
    o = {"id": "pos-dsc1", "date": TODAY, "created": CREATED.isoformat(), "name": "Cal Day",
         "email": "cal@example.com", "location": "Base Area", "total": 1.0, "coupon_discount": 0,
         "square_payment_id": "ORDER9", "square_paid": 1.04, "photographer_name": "Owner Seller",
         "filenames": "DSC_7773.JPG, DSC_7775.JPG"}
    S3.put_object(Bucket="test-bucket", Key=f"pos_orders/{TODAY}_pos-dsc1.json", Body=json.dumps(o).encode())
    cal = _sale(admin_client, "Cal")
    assert cal["photographer"] == "Dana Shooter" and cal["seller"] == "Owner Seller"
    csv_line = next(l for l in admin_client.get("/admin/export?days=7").text.splitlines() if "Cal" in l)
    assert "Dana Shooter" in csv_line and "Owner Seller" not in csv_line


# ── Refunds recorded by hand ─────────────────────────────────────────────────

@pytest.fixture()
def no_refunds(main_module):
    S3.delete_object(Bucket="test-bucket", Key="meta/order_refunds.json")


def test_refund_endpoints_need_admin_and_a_real_order_key(client, admin_client, no_refunds):
    assert client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": 5}).status_code == 401
    assert client.delete("/api/admin/orders/refund?key=pos:pos-tip1").status_code == 401
    assert admin_client.post("/api/admin/orders/refund", json={"key": "../x", "amount": 5}).status_code == 400
    assert admin_client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": 0}).status_code == 400
    assert admin_client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": "x"}).status_code == 400


def test_full_refund_comes_off_revenue_net_and_tips(admin_client, pos_orders, no_refunds):
    r = admin_client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": 31, "full": True,
                                                            "note": "wrong photos"})
    assert r.status_code == 200
    ann = _sale(admin_client, "Ann")
    assert ann["refund_full"] and ann["refunded"] == 31.0 and ann["refund_note"] == "wrong photos"
    assert ann["charged"] == 0 and ann["tip"] == 0 and ann["net"] == 0 and ann["charged_original"] == 31.0
    csv_line = next(l for l in admin_client.get("/admin/export?days=7").text.splitlines() if "Ann" in l)
    assert "$31.00" in csv_line and csv_line.count("$31.00") == 2           # original total + refunded

    assert admin_client.delete("/api/admin/orders/refund?key=pos:pos-tip1").status_code == 200
    ann = _sale(admin_client, "Ann")
    assert ann["charged"] == 31.0 and not ann["refunded"]


def test_partial_refund_reduces_the_sale(admin_client, pos_orders, no_refunds):
    admin_client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": 10, "full": False})
    ann = _sale(admin_client, "Ann")
    assert ann["charged"] == 21.0 and ann["refunded"] == 10.0 and not ann["refund_full"]


def test_refunded_sale_earns_no_commission(admin_client, owner_client, pos_orders, no_refunds):
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json", Body=json.dumps([
        {"id": "p2", "name": "Dana Shooter", "pin": "2222", "file_prefix": "img"}]).encode())
    def revenue():
        rows = owner_client.get(f"/api/photographers/commission?date_from={TODAY}&date_to={TODAY}").json()["commission"]
        return next(p for p in rows if p["id"] == "p2")["revenue"]
    assert revenue() == 25.0                                     # IMG_1.jpg sale, $25 before tax
    admin_client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": 10, "full": False})
    assert revenue() == 15.0
    admin_client.post("/api/admin/orders/refund", json={"key": "pos:pos-tip1", "amount": 31, "full": True})
    assert revenue() == 0.0


def test_orders_page_carries_customer_typed_text_only_as_data(admin_client, pos_orders, main_module, monkeypatch):
    """Customer text never goes into the page's HTML: the page loads it as JSON and escapes it
    when it shows it (templates/admin_orders.html, esc())."""
    evil = '<img src=x onerror=alert(1)>'
    monkeypatch.setattr(main_module, "WC_KEY", "ck_test")
    monkeypatch.setattr(main_module, "WC_SECRET", "cs_test")
    monkeypatch.setattr(main_module, "_fetch_wc_orders_all", lambda *a, **kw: [{
        "id": 901, "number": "901", "status": "completed", "date_created": MT_TODAY + "T12:00:00",
        "total": "25.00", "discount_total": "0", "coupon_lines": [{"code": evil}],
        "billing": {"first_name": evil, "last_name": "O'Brien", "email": "x@example.com",
                    "address_1": evil, "city": "", "state": "", "postcode": ""},
        "line_items": [{"name": "Photo", "meta_data": [{"key": "Location", "value": evil}]}],
        "fee_lines": [], "shipping_lines": [], "meta_data": []}])
    page = admin_client.get("/admin/orders?days=7").text
    assert evil not in page and "O'Brien" not in page
    r = admin_client.get("/api/admin/sales?days=7")
    assert r.headers["content-type"].startswith("application/json")
    row = next(x for x in r.json()["rows"] if x["order_id"] == 901)
    assert row["first"] == evil and row["code"] == evil and row["channel"] == "online" and row["order_no"] == "#901"
