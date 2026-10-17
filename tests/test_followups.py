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
