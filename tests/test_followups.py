"""Review request emails use the Review Request template and its saved Google review link."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
REVIEW_LINK = "https://g.page/r/example/review"


class _Resp:
    ok, status_code, text = True, 200, "{}"


@pytest.fixture(autouse=True)
def setup(main_module, monkeypatch):
    S3.put_object(Bucket="test-bucket", Key="pos_data/customers.json", Body=json.dumps([
        {"id": "c1", "first": "Ann", "last": "Smith", "email": "ann@example.com"},
    ]).encode())
    S3.delete_object(Bucket="test-bucket", Key="settings/email_templates.json")
    main_module._email_templates_cache["data"] = None
    main_module._customers_cache["data"] = None
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")


@pytest.fixture()
def sent(main_module, monkeypatch):
    emails = []
    def fake_post(url, json=None, **kw):
        emails.append(json)
        return _Resp()
    monkeypatch.setattr(main_module.http_requests, "post", fake_post)
    return emails


def _save_link(admin_client, url):
    return admin_client.put("/api/admin/email-templates/review_request", json={"review_url": url})


def test_review_link_saved_on_template(admin_client):
    assert _save_link(admin_client, REVIEW_LINK).status_code == 200
    templates = {t["id"]: t for t in admin_client.get("/api/admin/email-templates").json()["templates"]}
    assert templates["review_request"]["review_url"] == REVIEW_LINK


def test_review_link_must_be_https(admin_client):
    assert _save_link(admin_client, "http://example.com").status_code == 400


def test_review_request_without_saved_link_explains_where_to_add_it(admin_client, sent):
    r = admin_client.post("/api/admin/customer/ann@example.com/followup", json={"type": "review"})
    assert r.status_code == 400
    assert "Settings > Email Templates" in r.json()["error"]
    assert sent == []


def test_review_request_uses_template_wording_and_saved_link(admin_client, sent):
    admin_client.put("/api/admin/email-templates/review_request", json={
        "review_url": REVIEW_LINK, "subject": "Thanks {{first_name}}!",
        "body": "Hi {{first_name}},\n\nCustom wording here.\n\n{{review_button}}"})
    r = admin_client.post("/api/admin/customer/ann@example.com/followup", json={"type": "review"})
    assert r.status_code == 200
    email = sent[0]
    assert email["subject"] == "Thanks Ann!"
    assert "Custom wording here." in email["html"] and REVIEW_LINK in email["html"]
    assert REVIEW_LINK in email["text"]


def test_bulk_review_send_refused_without_link(admin_client, sent):
    r = admin_client.post("/api/admin/customers/bulk-followup",
                          json={"emails": ["ann@example.com"], "template_id": "review_request"})
    assert r.status_code == 400 and sent == []


def test_bulk_review_send_uses_saved_link(admin_client, sent):
    _save_link(admin_client, REVIEW_LINK)
    r = admin_client.post("/api/admin/customers/bulk-followup",
                          json={"emails": ["ann@example.com"], "template_id": "review_request"})
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert REVIEW_LINK in sent[0]["html"]


# ── Discount code emails use the Discount Code template ──────────────────────

@pytest.fixture()
def discount_code():
    S3.put_object(Bucket="test-bucket", Key="meta/discount_codes.json", Body=json.dumps([
        {"id": "d1", "code": "SAVE20", "type": "percent", "amount": 20.0, "expires": "2026-12-31", "active": True},
    ]).encode())


def test_discount_send_uses_template_wording(admin_client, sent, discount_code):
    admin_client.put("/api/admin/email-templates/discount_code", json={
        "subject": "A gift for {{first_name}}",
        "body": "Hi {{first_name}},\n\nMy own discount wording.\n\n{{code_box}}"})
    r = admin_client.post("/api/admin/customer/ann@example.com/followup",
                          json={"type": "discount", "discount_code": "save20"})
    assert r.status_code == 200
    email = sent[0]
    assert email["subject"] == "A gift for Ann"
    assert "My own discount wording." in email["html"]
    assert "SAVE20" in email["html"] and "20% off — expires 2026-12-31" in email["html"]
    assert "SAVE20" in email["text"]


def test_discount_send_unknown_code_refused(admin_client, sent, discount_code):
    r = admin_client.post("/api/admin/customer/ann@example.com/followup",
                          json={"type": "discount", "discount_code": "NOPE"})
    assert r.status_code == 400 and sent == []


def test_bulk_discount_box_shows_amount(admin_client, sent, discount_code):
    r = admin_client.post("/api/admin/customers/bulk-followup", json={
        "emails": ["ann@example.com"], "template_id": "discount_code",
        "variables": {"discount_code": "SAVE20"}})
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert "SAVE20" in sent[0]["html"] and "20% off" in sent[0]["html"]


# ── The email has a real button / code box, not plain text ───────────────────

def test_review_email_has_a_button_not_plain_text(admin_client, sent):
    _save_link(admin_client, REVIEW_LINK)
    admin_client.post("/api/admin/customer/ann@example.com/followup", json={"type": "review"})
    html, text = sent[0]["html"], sent[0]["text"]
    assert f'href="{REVIEW_LINK}"' in html and "Leave a Review</a>" in html
    assert "Leave us a review:" not in html            # the text fallback must not leak into the HTML
    assert f"Leave us a review: {REVIEW_LINK}" in text  # but is used in the plain-text version


def test_bulk_review_email_has_a_button(admin_client, sent):
    _save_link(admin_client, REVIEW_LINK)
    admin_client.post("/api/admin/customers/bulk-followup",
                      json={"emails": ["ann@example.com"], "template_id": "review_request"})
    assert "Leave a Review</a>" in sent[0]["html"] and "Leave us a review:" not in sent[0]["html"]


def test_discount_email_has_the_code_box(admin_client, sent, discount_code):
    admin_client.post("/api/admin/customer/ann@example.com/followup",
                      json={"type": "discount", "discount_code": "SAVE20"})
    html = sent[0]["html"]
    assert "Your discount code</div>" in html and "Your discount code: SAVE20" not in html


# ── Preview shows the exact email without sending ────────────────────────────

def test_preview_returns_the_email_and_sends_nothing(admin_client, sent):
    _save_link(admin_client, REVIEW_LINK)
    r = admin_client.post("/api/admin/customer/ann@example.com/followup/preview", json={"type": "review"})
    assert r.status_code == 200
    d = r.json()
    assert d["to"] == "ann@example.com" and "Leave a Review</a>" in d["html"]
    assert sent == []


def test_preview_requires_admin(client, sent):
    assert client.post("/api/admin/customer/ann@example.com/followup/preview",
                       json={"type": "review"}).status_code == 401


def test_preview_explains_missing_review_link(admin_client, sent):
    r = admin_client.post("/api/admin/customer/ann@example.com/followup/preview", json={"type": "review"})
    assert r.status_code == 400 and "Settings > Email Templates" in r.json()["error"]


def test_discount_email_has_shop_now_button(admin_client, sent, discount_code):
    admin_client.post("/api/admin/customer/ann@example.com/followup",
                      json={"type": "discount", "discount_code": "SAVE20"})
    html, text = sent[0]["html"], sent[0]["text"]
    assert 'href="https://photos.bigskyphotos.com"' in html and "Shop Now</a>" in html
    assert "Shop now: https://photos.bigskyphotos.com" in text


def test_bulk_discount_email_has_shop_now_button(admin_client, sent, discount_code):
    admin_client.post("/api/admin/customers/bulk-followup", json={
        "emails": ["ann@example.com"], "template_id": "discount_code",
        "variables": {"discount_code": "SAVE20"}})
    assert "Shop Now</a>" in sent[0]["html"]
