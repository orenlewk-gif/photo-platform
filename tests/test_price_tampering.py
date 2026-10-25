"""Nobody can change what checkout charges by changing what their browser sends: each photo's
folder, family and location come from the photo itself, never from the cart."""
import json

import boto3
import pytest

from tests.test_checkout_orders import FakeWoo

S3 = boto3.client("s3", region_name="us-east-1")
DAY = "2026-06-07"
AM = "images/2026-06-07/el-dorado-action/am/"
PM = "images/2026-06-07/el-dorado-action/pm/"
ZIP = "images/2026-06-07/adventure-zip/"
SIGN = "images/2026-06-07/big-sky-sign/smith/"


def _photo(path, loc, **kw):
    return {"path": path, "date": DAY, "location": loc, "last_name": "", "group": "", **kw}


@pytest.fixture()
def shop(main_module, monkeypatch, live_photos):
    live_photos([_photo(AM + f"A{i}.jpg", "El Dorado Action", group="am") for i in range(4)] +
                [_photo(PM + f"P{i}.jpg", "El Dorado Action", group="pm") for i in range(4)] +
                [_photo(ZIP + f"jones/J{i}.jpg", "Adventure Zip", last_name="Jones") for i in range(3)] +
                [_photo(ZIP + f"lee/L{i}.jpg", "Adventure Zip", last_name="Lee") for i in range(3)] +
                [_photo(SIGN + "S1.jpg", "Big Sky Sign", last_name="Smith"),
                 _photo("images/2026-06-07/el-dorado-action/am/HIDDEN.jpg", "El Dorado Action", group="am", draft=True),
                 _photo("images/2026-06-07/el-dorado-action/am/EM.jpg", "El Dorado Action", group="am",
                        partner="eagle-mount")])
    S3.put_object(Bucket="test-bucket", Key="meta/pricing.json", Body=json.dumps({
        "default": {"tiers": [{"count": 1, "price": 30}]},
        "activity_categories": [{"name": "Winter Action", "acts": ["El Dorado Action"]},
                                {"name": "Winter Portraits", "acts": ["Big Sky Sign"]},
                                {"name": "Zip Lines", "acts": ["Adventure Zip"]}],
        "activities": {"Winter Action": {"tiers": [{"count": 1, "price": 25}, {"count": "all", "price": 60}]},
                       "Zip Lines": {"tiers": [{"count": 1, "price": 20}]},
                       "Winter Portraits": {"tiers": [{"count": 1, "price": 40}]}},
        "combos": [{"label": "Action + Portrait", "activity_groups": ["Winter Action", "Winter Portraits"],
                    "pct_off": 20}],
        "logo_fee": 2,
    }).encode())
    S3.put_object(Bucket="test-bucket", Key="meta/folder_meta.json", Body=json.dumps({
        f"{DAY}|Adventure Zip|Jones": {"group_size": 6}, f"{DAY}|Adventure Zip|Lee": {"group_size": 2}}).encode())
    S3.put_object(Bucket="test-bucket", Key="meta/zip_pricing.json", Body=json.dumps({"tiers": [
        {"people": 2, "one_photo": 10, "all_photos": 30}, {"people": 6, "one_photo": 30, "all_photos": 90}]}).encode())
    for k in ("_pricing_cache", "_folder_meta_cache", "_zip_pricing_cache"):
        if hasattr(main_module, k):
            getattr(main_module, k)["data"] = None
    main_module._frame_prices_cache["data"] = None
    main_module._coupon_fail_limiter._calls.clear()
    fake = FakeWoo()
    monkeypatch.setattr(main_module.http_requests, "post", fake.post)
    monkeypatch.setattr(main_module.http_requests, "get", fake.get)
    yield fake
    for key in ("meta/pricing.json", "meta/folder_meta.json", "meta/zip_pricing.json"):
        S3.delete_object(Bucket="test-bucket", Key=key)
    for k in ("_pricing_cache", "_folder_meta_cache", "_zip_pricing_cache"):
        if hasattr(main_module, k):
            getattr(main_module, k)["data"] = None


def _album(paths, location="El Dorado Action", last_name="", date=DAY):
    return {"paths": paths, "count": len(paths), "location": location, "last_name": last_name, "date": date,
            "filenames": [p.rsplit("/", 1)[-1] for p in paths]}


def _quote(client, **body):
    body = {"digital_albums": [], "prints": [], "frames": [], "email": "ann@example.com", **body}
    return client.post("/api/checkout/quote", json=body)


def _total(client, **body):
    r = _quote(client, **body)
    assert r.status_code == 200, r.text
    return r.json()["total"]


def test_two_folders_cant_share_one_all_photos_price(client, shop):
    am, pm = [AM + f"A{i}.jpg" for i in range(4)], [PM + f"P{i}.jpg" for i in range(4)]
    honest = _total(client, digital_albums=[_album(am), _album(pm)])
    assert honest == 120.0                                     # All Photos ($60) for each folder
    merged = _quote(client, digital_albums=[_album(am + pm)])
    assert merged.status_code == 400 or merged.json()["total"] == honest


def test_the_family_name_comes_from_the_photos(client, shop):
    jones = [ZIP + f"jones/J{i}.jpg" for i in range(3)]
    honest = _total(client, digital_albums=[_album(jones, "Adventure Zip", "Jones")])
    assert honest == 90.0                                       # group of 6: $30 each, $90 all
    for fake_name in ("Lee", "", "Adventure Zip", "Nobody"):
        r = _quote(client, digital_albums=[_album(jones, "Adventure Zip", fake_name)])
        assert r.status_code == 400 or r.json()["total"] == honest, fake_name


def test_mixing_two_zip_families_isnt_cheaper(client, shop):
    jones, lee = [ZIP + f"jones/J{i}.jpg" for i in range(3)], [ZIP + f"lee/L{i}.jpg" for i in range(3)]
    honest = _total(client, digital_albums=[_album(jones, "Adventure Zip", "Jones"), _album(lee, "Adventure Zip", "Lee")])
    r = _quote(client, digital_albums=[_album(jones + lee, "Adventure Zip", "Lee")])
    assert r.status_code == 400 or r.json()["total"] == honest


PRINT = {"size": '8×10"', "size_idx": 1, "orientation": "landscape", "frame": "No Frame"}


def test_a_prints_location_comes_from_the_photo(client, shop):
    """A print of an action photo labelled as a portrait mustn't unlock the Action + Portrait bundle."""
    am = [AM + "A0.jpg"]
    honest = _quote(client, digital_albums=[_album(am)],
                    prints=[{**PRINT, "path": AM + "A1.jpg", "filename": "A1.jpg", "location": "El Dorado Action"}]).json()
    assert honest["bundle"] is None
    faked = _quote(client, digital_albums=[_album(am)],
                   prints=[{**PRINT, "path": AM + "A1.jpg", "filename": "A1.jpg", "location": "Big Sky Sign"}])
    assert faked.status_code == 400 or (faked.json()["bundle"] is None and faked.json()["total"] == honest["total"])


def test_a_real_portrait_print_does_unlock_the_bundle(client, shop):
    r = _quote(client, digital_albums=[_album([AM + "A0.jpg"])],
               prints=[{**PRINT, "path": SIGN + "S1.jpg", "filename": "S1.jpg", "location": ""}]).json()
    assert r["bundle"] and r["bundle"]["pct"] == 20


@pytest.mark.parametrize("path", ["images/2026-06-07/el-dorado-action/am/HIDDEN.jpg",
                                  "images/2026-06-07/el-dorado-action/am/EM.jpg",
                                  "images/2026-06-07/el-dorado-action/am/MADE_UP.jpg"])
def test_prints_only_of_published_photos(client, shop, path):
    r = _quote(client, prints=[{**PRINT, "path": path, "filename": path.rsplit("/", 1)[-1]}])
    assert r.status_code == 400


def test_a_print_with_no_photo_is_refused(client, shop):
    assert _quote(client, prints=[{**PRINT, "filename": "x.jpg"}]).status_code == 400


def test_half_price_print_needs_the_digital_in_this_order(client, shop):
    full = _total(client, prints=[{**PRINT, "path": AM + "A1.jpg", "filename": "A1.jpg"}])
    with_digital = _total(client, digital_albums=[_album([AM + "A1.jpg"])],
                          prints=[{**PRINT, "path": AM + "A1.jpg", "filename": "A1.jpg"}])
    assert with_digital - 25 < full                             # half price only with the digital


@pytest.mark.parametrize("bad", [{"size_idx": 99}, {"size_idx": -1}, {"size_idx": "abc"}])
def test_made_up_print_sizes_are_refused(client, shop, bad):
    r = _quote(client, prints=[{**PRINT, "path": AM + "A1.jpg", "filename": "A1.jpg", **bad}])
    assert r.status_code == 400


@pytest.mark.parametrize("count", [0, -3, 2, 99])
def test_the_photo_count_must_match_the_photos(client, shop, count):
    album = {**_album([AM + "A0.jpg", AM + "A1.jpg", AM + "A2.jpg"]), "count": count}
    r = _quote(client, digital_albums=[album])
    assert r.status_code == 400 or r.json()["total"] in (0, 60.0)


def test_logo_only_on_photos_bought_in_this_order(client, shop):
    base = _total(client, digital_albums=[_album([AM + "A0.jpg"])])
    t = _total(client, digital_albums=[_album([AM + "A0.jpg"])], digital_logo_paths=[AM + "A0.jpg", AM + "A3.jpg"])
    assert t == base + 2
