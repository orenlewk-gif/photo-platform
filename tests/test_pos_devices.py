"""A new device can't use the POS until the owner approves it with an emailed code; after that
it only needs a PIN. Clocking in and out only ever needs a PIN."""
import json

import boto3
import pytest
from fastapi.testclient import TestClient

S3 = boto3.client("s3", region_name="us-east-1")


@pytest.fixture()
def codes_on(main_module, monkeypatch):
    monkeypatch.setattr(main_module, "POS_DEVICE_CODES_ON", True)
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json", Body=json.dumps([
        {"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    main_module._pos_challenges.clear()
    d = main_module._load_admin_auth()
    d["pos_devices"] = {}
    sent = []
    monkeypatch.setattr(main_module, "_send_pos_device_code_email",
                        lambda code, info, p: sent.append((code, p["name"])) or True)
    yield sent
    d["pos_devices"] = {}


def _pin(c, pin="4821"):
    return c.post("/api/photographer/auth", json={"pin": pin, "pos": True})


def _approve(c, sent):
    r = _pin(c)
    assert r.status_code == 403 and r.json()["device_approval"]
    return c.post("/api/pos/device-verify", json={"challenge": r.json()["challenge"], "code": sent[-1][0]})


def test_a_new_device_gets_a_code_emailed_to_the_owner(main_module, codes_on):
    c = TestClient(main_module.app)
    r = _pin(c)
    assert r.status_code == 403 and r.json()["device_approval"] and "@" in r.json()["email"]
    assert codes_on == [(codes_on[0][0], "Test Photog")] and len(codes_on[0][0]) == 6
    assert main_module.POS_COOKIE not in c.cookies                     # no staff photos yet


def test_a_wrong_pin_never_sends_a_code(main_module, codes_on):
    c = TestClient(main_module.app)
    assert _pin(c, "1111").status_code == 401
    assert codes_on == []


def test_the_code_approves_the_device_and_signs_in(main_module, codes_on):
    c = TestClient(main_module.app)
    r = _approve(c, codes_on)
    assert r.status_code == 200 and r.json()["photographer"]["name"] == "Test Photog"
    assert main_module.POS_DEVICE_COOKIE in c.cookies and main_module.POS_COOKIE in c.cookies
    # From now on just the PIN, and everything the POS does works
    assert _pin(c).status_code == 200 and len(codes_on) == 1
    assert c.get("/api/pos-dates", headers={"X-POS-PIN": "4821"}).status_code == 200
    assert c.get("/api/pos-recent-sales", headers={"X-POS-PIN": "4821"}).status_code == 200


def test_an_unapproved_device_cant_use_the_pos_even_with_the_right_pin(main_module, codes_on):
    c = TestClient(main_module.app)
    h = {"X-POS-PIN": "4821"}
    assert c.get("/api/pos-dates", headers=h).status_code == 401
    assert c.get("/api/pos-recent-sales", headers=h).status_code == 401
    assert c.post("/api/pos-checkout", json={"pin": "4821", "items": [], "square_payment_id": "COMP"}).status_code == 401
    assert c.post("/api/pos-resend", json={"pin": "4821"}).status_code == 401
    # The clock-in page's sign-in (no "pos") works but doesn't unlock the POS
    r = c.post("/api/photographer/auth", json={"pin": "4821"})
    assert r.status_code == 200 and main_module.POS_COOKIE not in c.cookies
    assert c.get("/api/pos-dates").status_code == 401


def test_a_pos_cookie_from_before_needs_an_approved_device(main_module, codes_on):
    import time
    c = TestClient(main_module.app)
    c.cookies.set(main_module.POS_COOKIE, main_module._pos_token(int(time.time()) + 3600))
    assert c.get("/api/pos-dates").status_code == 401


def test_clocking_in_and_out_only_needs_a_pin(main_module, codes_on):
    c = TestClient(main_module.app)
    assert c.get("/api/clock/status", params={"pin": "4821"}).status_code == 200


def test_wrong_codes_and_expired_codes(main_module, codes_on, monkeypatch):
    c = TestClient(main_module.app)
    ch = _pin(c).json()["challenge"]
    for _ in range(main_module.LOGIN_CODE_TRIES - 1):
        assert c.post("/api/pos/device-verify", json={"challenge": ch, "code": "000000"}).json()["error"] == "Incorrect code"
    r = c.post("/api/pos/device-verify", json={"challenge": ch, "code": "000000"})
    assert r.status_code == 401 and r.json()["restart"]
    # the right code no longer works for that attempt
    assert c.post("/api/pos/device-verify", json={"challenge": ch, "code": codes_on[-1][0]}).status_code == 401
    assert main_module.POS_DEVICE_COOKIE not in c.cookies


def test_the_studio_lists_and_removes_pos_devices(main_module, codes_on, admin_client):
    c = TestClient(main_module.app)
    _approve(c, codes_on)
    devices = admin_client.get("/api/admin/pos-devices").json()["devices"]
    assert len(devices) == 1 and devices[0]["by"] == "Test Photog"
    assert admin_client.delete(f"/api/admin/pos-devices/{devices[0]['id']}").json()["ok"]
    assert c.get("/api/pos-dates", headers={"X-POS-PIN": "4821"}).status_code == 401
    assert _pin(c).status_code == 403                                  # a new code is needed


def test_pos_device_list_needs_the_studio(client):
    assert client.get("/api/admin/pos-devices").status_code == 401
    assert client.delete("/api/admin/pos-devices/abc").status_code == 401


def test_a_studio_computer_counts_as_approved(main_module, codes_on, admin_client):
    assert admin_client.post("/api/photographer/auth", json={"pin": "4821", "pos": True}).status_code == 200
    assert codes_on == []


def test_codes_off_is_the_safety_valve(main_module, codes_on, monkeypatch):
    monkeypatch.setattr(main_module, "POS_DEVICE_CODES_ON", False)
    c = TestClient(main_module.app)
    assert _pin(c).status_code == 200 and codes_on == []
