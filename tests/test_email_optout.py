"""Marketing emails carry the business address and a working unsubscribe; unsubscribed people aren't emailed."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")


class _Resp:
    ok, status_code, text = True, 200, "{}"


@pytest.fixture(autouse=True)
def setup(main_module, monkeypatch):
    S3.put_object(Bucket="test-bucket", Key="pos_data/customers.json", Body=json.dumps([
        {"id": "c1", "first": "Ann", "last": "Smith", "email": "ann@example.com"},
        {"id": "c2", "first": "Bob", "last": "Lee", "email": "bob@example.com"}]).encode())
    S3.put_object(Bucket="test-bucket", Key="settings/config.json",
                  Body=json.dumps({"business": {"address": "PO Box 160111, Big Sky, MT 59716"}}).encode())
    S3.delete_object(Bucket="test-bucket", Key="meta/email_optouts.json")
    main_module._email_optouts_doc.cache["data"] = None
    main_module._customers_cache["data"] = None
    main_module._email_templates_cache["data"] = None
    monkeypatch.setattr(main_module, "RESEND_API_KEY", "re_test")
    yield
    S3.delete_object(Bucket="test-bucket", Key="settings/config.json")
    S3.delete_object(Bucket="test-bucket", Key="meta/email_optouts.json")
    main_module._email_optouts_doc.cache["data"] = None


@pytest.fixture()
def sent(main_module, monkeypatch):
    emails = []
    monkeypatch.setattr(main_module.http_requests, "post", lambda url, json=None, **kw: emails.append(json) or _Resp())
    return emails


def _custom(admin_client, to="ann@example.com"):
    return admin_client.post(f"/api/admin/customer/{to}/followup",
                             json={"type": "custom", "subject": "Spring deal", "message": "Come back soon"})


def test_marketing_email_has_address_unsubscribe_link_and_one_click_headers(admin_client, main_module, sent):
    assert _custom(admin_client).status_code == 200
    mail = sent[0]
    assert "PO Box 160111, Big Sky, MT 59716" in mail["html"] and "Unsubscribe" in mail["html"]
    url = main_module._unsubscribe_url("ann@example.com")
    assert url in mail["html"] and url in mail["text"]
    assert mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and url in mail["headers"]["List-Unsubscribe"]


def test_opening_the_link_asks_and_the_button_unsubscribes(client, main_module):
    token = main_module._unsubscribe_token("Ann@Example.com")
    page = client.get(f"/unsubscribe/{token}")
    assert page.status_code == 200 and "ann@example.com" in page.text and "<button" in page.text
    assert main_module._opted_out("ann@example.com") is None          # a mail scanner opening the link changes nothing
    done = client.post(f"/unsubscribe/{token}", data={"List-Unsubscribe": "One-Click"})
    assert done.status_code == 200 and "unsubscribed" in done.text
    assert main_module._opted_out("ANN@example.com")["how"] == "link"


def test_forged_or_garbled_links_do_nothing(client, main_module):
    good = main_module._unsubscribe_token("ann@example.com")
    other = main_module._unsubscribe_token("bob@example.com")
    forged = good.split(".")[0] + "." + other.split(".")[1]
    for tok in (forged, "garbage", good + "x"):
        assert client.post(f"/unsubscribe/{tok}").status_code == 404
    assert main_module._opted_out("ann@example.com") is None


def test_unsubscribed_customers_are_not_emailed(admin_client, main_module, sent):
    main_module._record_optout("ann@example.com", "link")
    r = _custom(admin_client)
    assert r.status_code == 409 and "unsubscribed" in r.json()["error"] and sent == []
    admin_client.put("/api/admin/email-templates/discount_code", json={"subject": "Hi", "body": "Hello {{first_name}}"})
    r = admin_client.post("/api/admin/customers/bulk-followup", json={
        "emails": ["ann@example.com", "bob@example.com"], "template_id": "discount_code", "variables": {}})
    results = {x["email"]: x for x in r.json()["results"]}
    assert results["ann@example.com"]["error"] == "unsubscribed" and results["bob@example.com"]["ok"]
    assert [m["to"] for m in sent] == [["bob@example.com"]]
    assert main_module._unsubscribe_url("bob@example.com") in sent[0]["html"]


def test_receipts_are_not_marketing(main_module, sent):
    main_module._send_pos_receipt_email("ann@example.com", "Ann Smith", "pos-1", [{"label": "Photos", "price": 25}],
                                        25.0, "", 0, None)
    assert sent and "Unsubscribe" not in sent[0]["html"] and "headers" not in sent[0]
