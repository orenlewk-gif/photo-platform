"""Receipts show what Square really charged (tax + tip); the Orders tab lists in-person sales with tips."""
import json
from datetime import datetime, timezone

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
        {"id": "pos-tip1", "date": TODAY, "created": TODAY + "T15:00:00", "name": "Ann Smith",
         "email": "ann@example.com", "location": "Mountain Biking", "total": 25.0, "coupon_discount": 0,
         "square_payment_id": "ORDER1", "square_verified": True, "square_paid": 31.0, "square_tax": 1.0,
         "square_tip": 5.0, "photographer_name": "Test Photog", "package_token": "abc123", "filenames": "IMG_1.jpg"},
        {"id": "pos-comp1", "date": TODAY, "created": TODAY + "T16:00:00", "name": "Bob Jones",
         "email": "bob@example.com", "location": "Base Area", "total": 40.0, "coupon_discount": 0,
         "square_payment_id": "COMP", "photographer_name": "Test Photog"},
    ]
    for o in orders:
        S3.put_object(Bucket="test-bucket", Key=f"pos_orders/{o['date']}_{o['id']}.json", Body=json.dumps(o).encode())
    return orders


def test_orders_tab_lists_in_person_sales_with_tips(admin_client, pos_orders):
    html = admin_client.get("/admin/orders?days=7").text
    assert "<th>Tip</th>" in html
    assert "Ann Smith" in html and "In-Person — Mountain Biking" in html
    assert "$31.00" in html and "$5.00" in html                  # whole amount charged, tip
    assert '<div class="label">Tips</div><div class="val">$5.00</div>' in html
    assert '<div class="label">Revenue</div><div class="val">$31.00</div>' in html   # comp adds nothing
    assert "/package/abc123" in html
    assert ">Comp<" in html


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
    o = {"id": "pos-dsc1", "date": TODAY, "created": TODAY + "T17:00:00", "name": "Cal Day",
         "email": "cal@example.com", "location": "Base Area", "total": 1.0, "coupon_discount": 0,
         "square_payment_id": "ORDER9", "square_paid": 1.04, "photographer_name": "Owner Seller",
         "filenames": "DSC_7773.JPG, DSC_7775.JPG"}
    S3.put_object(Bucket="test-bucket", Key=f"pos_orders/{TODAY}_pos-dsc1.json", Body=json.dumps(o).encode())
    html = admin_client.get("/admin/orders?days=7").text
    row = html[html.index("Cal Day") - 2000: html.index("Cal Day") + 2000]
    assert "Dana Shooter" in row and "Owner Seller" not in row
    csv_line = next(l for l in admin_client.get("/admin/export?days=7").text.splitlines() if "Cal" in l)
    assert "Dana Shooter" in csv_line and "Owner Seller" not in csv_line
