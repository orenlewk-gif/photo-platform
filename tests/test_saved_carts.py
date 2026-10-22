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
