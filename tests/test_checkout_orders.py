"""What online checkout sends WooCommerce: line items, shipping, coupons and order meta.

The first tests pin today's behaviour for orders that must not change; later tests
cover the pricing the server now works out itself (half-price prints, the logo
add-on, frame prices from WooCommerce, bundles)."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
WOO_FRAME_PRICES = {1057: "30", 1058: "30", 1059: "40", 1060: "40", 1061: "60", 1062: "60", 1063: "140", 1064: "140"}


class FakeWoo:
    def __init__(self):
        self.created, self.variation_calls = [], 0
        self.frame_prices = dict(WOO_FRAME_PRICES)

    def post(self, url, json=None, **kw):
        assert url.endswith("/orders"), url
        self.created.append(json)
        return _Resp(201, {"id": 501, "order_key": "wc_order_k"})

    def get(self, url, params=None, **kw):
        if url.endswith("/products/1034/variations"):
            self.variation_calls += 1
            return _Resp(200, [{"id": vid, "price": p, "regular_price": p, "stock_status": "instock"}
                               for vid, p in self.frame_prices.items()])
        raise AssertionError(url)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.ok = status, payload, 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture()
def woo(main_module, monkeypatch):
    S3.put_object(Bucket="test-bucket", Key="meta/pricing.json", Body=json.dumps({
        "default": {"tiers": [{"count": 1, "price": 30}, {"count": "all", "price": 100}]},
        "activity_categories": [{"name": "Winter Action", "acts": ["El Dorado Action"]},
                                {"name": "Winter Portraits", "acts": ["Big Sky Sign"]}],
        "activities": {"Winter Action": {"tiers": [{"count": 1, "price": 25},
                                                   {"count": "all", "price": 115, "max": True}]},
                       "Mountain Biking": {"tiers": [{"count": 1, "price": 25}, {"count": 2, "price": 45}]}},
        "logo_fee": 2,
    }).encode())
    S3.put_object(Bucket="test-bucket", Key="meta/discount_codes.json", Body=json.dumps([
        {"id": "PRINTS50", "code": "PRINTS50", "type": "percent", "amount": 50, "applies_to": "prints", "active": True},
        {"id": "SAVE15", "code": "SAVE15", "type": "percent", "amount": 15, "applies_to": "all", "active": True},
        {"id": "PARTNER10", "code": "PARTNER10", "type": "percent", "amount": 10, "applies_to": "all", "active": True},
        {"id": "FIVEOFF", "code": "FIVEOFF", "type": "fixed", "amount": 5, "applies_to": "all", "active": True},
    ]).encode())
    for k in ("_pricing_cache", "_portrait_pricing_cache"):
        getattr(main_module, k)["data"] = None
    if hasattr(main_module, "_frame_prices_cache"):
        main_module._frame_prices_cache["data"] = None
    fake = FakeWoo()
    monkeypatch.setattr(main_module.http_requests, "post", fake.post)
    monkeypatch.setattr(main_module.http_requests, "get", fake.get)
    yield fake
    S3.delete_object(Bucket="test-bucket", Key="meta/pricing.json")
    S3.delete_object(Bucket="test-bucket", Key="meta/discount_codes.json")
    main_module._pricing_cache["data"] = None


def _order(client, woo, **body):
    body = {"digital_albums": [], "prints": [], "frames": [], "email": "ann@example.com",
            "date": "2026-06-07", "location": "El Dorado Action", **body}
    r = client.post("/api/checkout", json=body)
    assert r.status_code == 200, r.text
    return woo.created[-1]


def _lines(order):
    return [(li["name"], float(li["total"])) for li in order["line_items"]]


def _shipping(order):
    return [float(f["total"]) for f in order["fee_lines"] if f["name"] == "Shipping"]


def _meta(order):
    return {m["key"]: m["value"] for m in order["meta_data"]}


PRINT_8X10 = {"filename": "IMG_9.jpg", "size": '8×10"', "size_idx": 1, "orientation": "landscape",
              "frame": "No Frame", "price": 30, "frame_price": 0}


# ── Unchanged behaviour ──────────────────────────────────────────────────────

def test_portrait_poses_and_singles(client, woo):
    order = _order(client, woo,
                   portrait_poses=[{"pose_label": "Red", "files": ["a.jpg", "b.jpg"], "paths": ["images/a", "images/b"], "family": "Smith"},
                                   {"pose_label": "Blue", "files": ["c.jpg"], "paths": ["images/c"], "family": "Smith"}],
                   portrait_singles=[{"filename": "d.jpg", "path": "images/d", "family": "Smith"}])
    names = [n for n, _ in _lines(order)]
    assert names == ["Portrait Red (2 photos) — Smith", "Portrait Blue (1 photo) — Smith", "Portrait Single Photo — Smith"]
    assert "images/a|images/b|images/c|images/d" in _meta(order)["_photo_paths"]


def test_one_unframed_print_pays_its_shipping(client, woo):
    order = _order(client, woo, prints=[PRINT_8X10])
    assert _lines(order) == [('8×10" Print — IMG_9.jpg', 30.0)]
    assert _shipping(order) == [8.0]


def test_three_unframed_prints_ship_free(client, woo):
    order = _order(client, woo, prints=[{**PRINT_8X10, "filename": f"P{i}.jpg"} for i in range(3)])
    assert _shipping(order) == []


def test_framed_print_frame_line_and_frame_shipping(client, woo):
    order = _order(client, woo, prints=[{**PRINT_8X10, "frame": "Black", "frame_price": 40, "orientation": "portrait"}])
    frame = order["line_items"][1]
    assert frame["variation_id"] == 1060 and float(frame["total"]) == 40.0
    assert _shipping(order) == [15.0]


def test_standalone_frames_quantity_and_shipping(client, woo):
    order = _order(client, woo, frames=[{"variation_id": 1063, "size": "16x20", "sizeIdx": 3, "orientation": "landscape",
                                         "price": 140, "quantity": 2}])
    li = order["line_items"][0]
    assert li["variation_id"] == 1063 and li["quantity"] == 2 and float(li["total"]) == 280.0
    assert _shipping(order) == [75.0]                       # $50 + half of the second $50


def test_coupon_goes_to_woocommerce_and_order_meta(client, woo):
    order = _order(client, woo, prints=[PRINT_8X10], coupon_code="prints50", coupon_discount=15)
    assert order["coupon_lines"] == [{"code": "prints50"}]
    assert _meta(order)["_coupon_code"] == "prints50"


def test_order_meta_for_delivery(client, woo):
    order = _order(client, woo, digital_albums=[{"location": "El Dorado Action", "date": "2026-06-07", "count": 2,
                                                 "last_name": "El Dorado Action", "filenames": ["A.jpg", "B.jpg"],
                                                 "paths": ["images/a", "images/b"]}],
                   digital_filenames=["A.jpg", "B.jpg"], digital_paths=["images/a", "images/b"])
    meta = _meta(order)
    assert meta["_photo_files"] == "A.jpg, B.jpg" and meta["_photo_paths"] == "images/a|images/b"
    assert meta["_photo_location"] == "El Dorado Action" and meta["_photo_date"] == "2026-06-07"
    assert order["billing"]["email"] == "ann@example.com"
    assert _lines(order) == [("Digital Photos — El Dorado Action", 50.0)]


# ── Prices the server now works out itself ───────────────────────────────────

ALBUM = {"location": "El Dorado Action", "date": "2026-06-07", "count": 2, "last_name": "El Dorado Action",
         "filenames": ["A.jpg", "B.jpg"], "paths": ["images/2026-06-07/el-dorado/A.jpg", "images/2026-06-07/el-dorado/B.jpg"]}
POSE = {"pose_label": "Red", "files": ["S1.jpg"], "paths": ["images/2026-06-07/big-sky-sign/S1.jpg"], "family": "Smith"}


def test_print_of_a_photo_also_bought_digitally_is_half_price(client, woo):
    order = _order(client, woo, digital_albums=[ALBUM],
                   prints=[{**PRINT_8X10, "filename": "A.jpg", "path": ALBUM["paths"][0]},
                           {**PRINT_8X10, "filename": "Z.jpg", "path": "images/2026-06-07/el-dorado/Z.jpg"}])
    assert _lines(order)[1:] == [('8×10" Print — A.jpg', 15.0), ('8×10" Print — Z.jpg', 30.0)]


def test_print_of_a_portrait_bought_digitally_is_half_price(client, woo):
    order = _order(client, woo, portrait_poses=[POSE],
                   prints=[{**PRINT_8X10, "filename": "S1.jpg", "path": POSE["paths"][0]}])
    assert _lines(order)[1] == ('8×10" Print — S1.jpg', 15.0)


def test_browser_print_and_frame_prices_are_ignored(client, woo):
    order = _order(client, woo, prints=[{**PRINT_8X10, "price": 0.01, "frame": "Black", "frame_price": 0.01}],
                   frames=[{"variation_id": 1057, "price": 0.01, "quantity": 3, "sizeIdx": 0}])
    assert [t for _, t in _lines(order)] == [30.0, 40.0, 90.0]


def test_unknown_or_silly_frames_are_refused(client, woo):
    for bad in ({"variation_id": 999, "quantity": 1}, {"variation_id": 1057, "quantity": 0},
                {"variation_id": 1057, "quantity": 500}, {"variation_id": "x", "quantity": 1}):
        r = client.post("/api/checkout", json={"frames": [bad], "email": "ann@example.com"})
        assert r.status_code == 400, bad
    assert woo.created == []


def test_frame_prices_come_from_woocommerce_and_are_kept_briefly(client, woo, main_module):
    _order(client, woo, frames=[{"variation_id": 1059, "quantity": 1}])
    _order(client, woo, frames=[{"variation_id": 1059, "quantity": 1}])
    assert woo.variation_calls == 1                          # second order reused the prices
    woo.frame_prices[1059] = "45"
    main_module._frame_prices_cache["data"] = None           # what saving prices in the studio does
    order = _order(client, woo, frames=[{"variation_id": 1059, "quantity": 1}])
    assert float(order["line_items"][0]["total"]) == 45.0


def test_logo_on_digital_photos_is_charged_and_listed_by_name(client, woo):
    order = _order(client, woo, digital_albums=[ALBUM],
                   digital_logo_paths=[ALBUM["paths"][1], "images/not-bought/X.jpg"])
    logo = order["line_items"][1]
    assert logo["name"] == "Big Sky Resort logo — 1 photo" and float(logo["total"]) == 2.0
    assert {"key": "Logo photos", "value": "B.jpg"} in logo["meta_data"]


def test_logo_on_a_print_is_charged_and_marked(client, woo):
    order = _order(client, woo, digital_albums=[ALBUM],
                   prints=[{**PRINT_8X10, "filename": "A.jpg", "path": ALBUM["paths"][0], "logo": True},
                           {**PRINT_8X10, "filename": "Z.jpg", "logo": True}])
    a, z = order["line_items"][1], order["line_items"][2]
    assert a["name"] == '8×10" Print — A.jpg + Big Sky Resort logo' and float(a["total"]) == 17.0   # half + $2
    assert float(z["total"]) == 32.0
    assert {"key": "Logo", "value": "Big Sky Resort logo"} in z["meta_data"]


# ── Bundles: worked out by the server; a bigger code discount wins per item ──

@pytest.fixture()
def bundle(woo, main_module):
    p = json.loads(S3.get_object(Bucket="test-bucket", Key="meta/pricing.json")["Body"].read())
    p["combos"] = [{"activity_groups": ["Winter Action", "Winter Portraits"], "pct_off": 10, "label": "Winter Bundle"}]
    S3.put_object(Bucket="test-bucket", Key="meta/pricing.json", Body=json.dumps(p).encode())
    main_module._pricing_cache["data"] = None
    return woo


BUNDLE_CART = dict(digital_albums=[ALBUM], portrait_poses=[POSE],
                   prints=[{**PRINT_8X10, "filename": "Z.jpg", "path": "images/2026-06-07/el-dorado/Z.jpg"}])


def test_bundle_takes_its_percent_off_every_item(client, bundle):
    order = _order(client, bundle, **BUNDLE_CART)
    assert [t for _, t in _lines(order)] == [45.0, 58.5, 27.0]            # 50, 65, 30 less 10%
    meta = _meta(order)
    assert meta["_bundle_discount_pct"] == "10.0" and meta["_bundle_savings"] == "14.5"


def test_bundle_needs_both_groups_whatever_the_browser_says(client, bundle):
    order = _order(client, bundle, digital_albums=[ALBUM], bundle_discount_pct=90, bundle_savings_amount=999)
    assert _lines(order) == [("Digital Photos — El Dorado Action", 50.0)]
    assert "_bundle_discount_pct" not in _meta(order)


def test_a_bigger_category_code_wins_on_its_items_and_the_bundle_covers_the_rest(client, bundle):
    order = _order(client, bundle, **BUNDLE_CART, coupon_code="PRINTS50")
    assert [t for _, t in _lines(order)] == [45.0, 58.5, 30.0]            # print full price: the code takes 50%
    assert order["coupon_lines"] == [{"code": "prints50"}]


def test_a_bigger_whole_order_code_replaces_the_bundle(client, bundle):
    order = _order(client, bundle, **BUNDLE_CART, coupon_code="SAVE15")
    assert [t for _, t in _lines(order)] == [50.0, 65.0, 30.0]
    assert order["coupon_lines"] == [{"code": "save15"}] and "_bundle_discount_pct" not in _meta(order)


@pytest.mark.parametrize("code", ["PARTNER10", "FIVEOFF"])        # equal, or smaller, than the bundle
def test_a_code_that_saves_less_than_the_bundle_is_not_used(client, bundle, code):
    q = client.post("/api/checkout/quote", json={**BUNDLE_CART, "coupon_code": code, "date": "2026-06-07"}).json()
    assert q["coupon"] is None and "saves you more" in q["coupon_note"]
    order = _order(client, bundle, **BUNDLE_CART, coupon_code=code)
    assert order["coupon_lines"] == [] and [t for _, t in _lines(order)] == [45.0, 58.5, 27.0]


def test_upsell_names_the_missing_group(client, bundle):
    q = client.post("/api/checkout/quote", json={"digital_albums": [ALBUM], "date": "2026-06-07"}).json()
    assert q["upsell"] == {"missing": ["Portraits"], "pct": 10.0} and q["bundle"] is None


# ── The page's totals are the charge ─────────────────────────────────────────

@pytest.mark.parametrize("extra", [
    {}, {"coupon_code": "PRINTS50"}, {"coupon_code": "SAVE15"}, {"coupon_code": "FIVEOFF"},
])
def test_quote_is_exactly_what_checkout_charges(client, bundle, extra):
    cart = {**BUNDLE_CART, "frames": [{"variation_id": 1061, "quantity": 2}], "date": "2026-06-07",
            "digital_logo_paths": [ALBUM["paths"][0]], **extra}
    q = client.post("/api/checkout/quote", json=cart).json()
    order = _order(client, bundle, **cart)
    lines = round(sum(float(li["total"]) for li in order["line_items"]), 2)
    ship = sum(_shipping(order))
    assert q["subtotal"] == lines and q["shipping"] == ship
    assert q["total"] == round(lines + ship - (q["coupon"] or {"discount": 0})["discount"], 2)
    assert sum(r["price"] for r in q["rows"]) == pytest.approx(lines)


def test_invalid_code_is_ignored_with_a_reason(client, woo):
    q = client.post("/api/checkout/quote", json={"prints": [PRINT_8X10], "coupon_code": "NOPE"}).json()
    assert q["coupon"] is None and q["coupon_note"] == "Invalid coupon code"


def test_validate_coupon_answers_unchanged(client, woo):
    assert client.get("/api/validate-coupon?code=nope").status_code == 404
    assert client.get("/api/validate-coupon?code=").status_code == 400
    ok = client.get("/api/validate-coupon?code=prints50").json()
    assert ok["valid"] and ok["code"] == "prints50" and ok["applies_to"] == "prints"


def test_shop_prices_for_the_pages(client, woo):
    d = client.get("/api/shop-prices").json()
    assert [s["price"] for s in d["print_sizes"]] == [25.0, 30.0, 45.0, 90.0]
    assert [f["landscape"] for f in d["frame_prices"]] == [30.0, 40.0, 60.0, 140.0]
    assert d["logo_fee"] == 2.0 and d["free_ship_threshold"] == 3
