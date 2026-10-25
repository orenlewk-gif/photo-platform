"""Policy pages, the site footer's business details, and the accessibility basics on customer pages."""
import json
import re

import boto3

S3 = boto3.client("s3", region_name="us-east-1")


def test_privacy_policy_is_published(client):
    r = client.get("/privacy")
    assert r.status_code == 200 and "Privacy Policy" in r.text and "facial recognition" in r.text
    assert "Want a photo taken down?" in r.text and 'lang="en"' in r.text


def test_policies_with_an_unanswered_question_are_never_shown(client, main_module):
    for page in ("/terms", "/refunds"):
        body = open(f"templates{page}.html").read()
        r = client.get(page)
        assert (r.status_code == 404) == ("[[CONFIRM" in body)
        assert "[[CONFIRM" not in r.text


def test_footer_details_come_from_settings_and_blank_fields_keep_the_defaults(client, main_module):
    S3.put_object(Bucket="test-bucket", Key="settings/config.json",
                  Body=json.dumps({"business": {"address": "PO Box 1, Big Sky, MT 59716", "phone": ""}}).encode())
    try:
        info = client.get("/api/site-info").json()
        assert info["address"] == "PO Box 1, Big Sky, MT 59716" and info["phone"] == "(406) 404-6680"
    finally:
        S3.delete_object(Bucket="test-bucket", Key="settings/config.json")


def test_customer_pages_link_the_policies_and_label_their_fields(client):
    for page in ("/", "/checkout"):
        html = client.get(page).text
        for link in ('href="/privacy"', 'href="/terms"', 'href="/refunds"'):
            assert link in html
    checkout = client.get("/checkout").text
    assert "By continuing you agree" in checkout
    labels = re.findall(r'<label class="co-label"( for="[^"]+")?>', checkout)
    assert labels and all(f for f in labels)                     # every checkout label points at its field
    gallery = client.get("/").text
    assert 'aria-label="Last name"' in gallery and 'class="select-check"' in gallery and "<button type=\"button\" class=\"select-check\"" in gallery


def test_photo_removal_form_emails_the_business(client, main_module, monkeypatch):
    sent = []

    class _R:
        ok = True
        text = ""
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: sent.append(json) or _R())

    page = client.get("/photo-removal")
    assert page.status_code == 200 and 'id="rm-form"' in page.text and 'class="custom-header"' in page.text
    r = client.post("/api/photo-removal", json={"name": "Ann", "email": "ann@example.com", "photos": "AMS02244",
                                                "message": "My kid <b>please</b>"})
    assert r.json() == {"sent": True}
    mail = sent[-1]
    assert mail["reply_to"] == "ann@example.com" and "AMS02244" in mail["subject"]
    assert "&lt;b&gt;please&lt;/b&gt;" in mail["html"]                      # shown as text, never as HTML
    assert client.post("/api/photo-removal", json={"email": "bad"}).status_code == 400
    assert client.post("/api/photo-removal", json={"email": "a@b.co"}).status_code == 400   # which photo?
    n = len(sent)
    assert client.post("/api/photo-removal", json={"email": "a@b.co", "photos": "x", "website": "spam"}).json() == {"sent": True}
    assert len(sent) == n                                                    # robots are quietly ignored
    assert 'href="/photo-removal"' in client.get("/").text
