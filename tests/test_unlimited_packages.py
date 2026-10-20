"""Unlimited Action Photo Packages: pricing settings (Admin > Pricing)."""
import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")


@pytest.fixture(autouse=True)
def fresh(main_module):
    S3.delete_object(Bucket="test-bucket", Key="meta/unlimited_packages.json")
    main_module._unlimited_doc.cache["data"] = None
    yield
    S3.delete_object(Bucket="test-bucket", Key="meta/unlimited_packages.json")
    main_module._unlimited_doc.cache["data"] = None


def test_defaults_are_the_agreed_packages(client):
    d = client.get("/api/unlimited-packages").json()
    assert [(t["days"], t["price"]) for t in d["tiers"]] == [(3, 205), (5, 275), (7, 345)]
    assert d["included_people"] == 4 and d["extra_person_price"] == 30 and d["link_days_after"] == 30


def test_owner_can_rename_and_reprice(owner_client, client):
    body = client.get("/api/unlimited-packages").json()
    body["tiers"][1]["name"] = "5-Day Unlimited"
    body["tiers"][1]["price"] = 285
    body["extra_person_price"] = 35
    r = owner_client.post("/api/admin/unlimited-packages", json=body)
    assert r.status_code == 200
    d = client.get("/api/unlimited-packages").json()
    assert d["tiers"][1] == {"id": "unl-5", "name": "5-Day Unlimited", "days": 5, "price": 285.0}
    assert d["extra_person_price"] == 35


def test_saving_needs_the_owner_pin(admin_client, client):
    body = client.get("/api/unlimited-packages").json()
    r = admin_client.post("/api/admin/unlimited-packages", json=body)
    assert r.status_code == 403 and r.json()["pin_required"]
    assert client.post("/api/admin/unlimited-packages", json=body).status_code == 401


@pytest.mark.parametrize("change,msg", [
    (lambda b: b["tiers"].__setitem__(0, {**b["tiers"][0], "name": " "}), "needs a name"),
    (lambda b: b["tiers"].__setitem__(0, {**b["tiers"][0], "days": 0}), "Days"),
    (lambda b: b["tiers"].__setitem__(0, {**b["tiers"][0], "price": "abc"}), "must be a number"),
    (lambda b: b.__setitem__("tiers", []), "at least one"),
    (lambda b: b.__setitem__("included_people", 0), "People included"),
])
def test_bad_settings_are_refused_with_a_reason(owner_client, client, change, msg):
    body = client.get("/api/unlimited-packages").json()
    change(body)
    r = owner_client.post("/api/admin/unlimited-packages", json=body)
    assert r.status_code == 400 and msg in r.json()["error"]


def test_new_package_gets_an_id(owner_client, client):
    body = client.get("/api/unlimited-packages").json()
    body["tiers"].append({"name": "10-Day Unlimited", "days": 10, "price": 450})
    d = owner_client.post("/api/admin/unlimited-packages", json=body).json()
    assert d["tiers"][-1]["id"].startswith("unl-") and d["tiers"][-1]["days"] == 10
