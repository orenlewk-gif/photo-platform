"""Frame prices live in Pricing (one per size): copied from WooCommerce the first time, then
edited in the studio, and what the shop and checkout charge."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


@pytest.fixture()
def frames(main_module, monkeypatch):
    S3.delete_object(Bucket="test-bucket", Key="meta/frame_prices.json")
    main_module._frame_prices_cache["data"] = None
    calls = []
    monkeypatch.setattr(main_module, "WC_KEY", "ck_test")
    monkeypatch.setattr(main_module, "WC_SECRET", "cs_test")

    def fake_get(url, params=None, **kw):
        calls.append(url)
        assert url.endswith("/products/1034/variations")
        return _Resp(200, [{"id": 1057, "price": "40", "stock_status": "instock"},
                           {"id": 1058, "price": "40", "stock_status": "instock"},
                           {"id": 1059, "price": "55", "stock_status": "instock"}])
    monkeypatch.setattr(main_module.http_requests, "get", fake_get)
    monkeypatch.setattr(main_module.http_requests, "put", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no WooCommerce writes")))
    yield calls
    S3.delete_object(Bucket="test-bucket", Key="meta/frame_prices.json")
    main_module._frame_prices_cache["data"] = None


def test_requires_the_owner(client, frames):
    assert client.post("/api/admin/frames/prices", json={"sizes": [{"size_idx": 0, "price": "60"}]}).status_code == 401


def test_copied_from_woocommerce_once_then_kept_in_pricing(client, frames, main_module):
    f = client.get("/api/frames").json()["frames"]
    assert f[0]["landscape"]["price"] == "40" and f[1]["portrait"]["price"] == "55" and f[2]["landscape"]["price"] == "0"
    saved = json.loads(S3.get_object(Bucket="test-bucket", Key="meta/frame_prices.json")["Body"].read())
    assert saved["sizes"] == {"0": {"price": 40.0}, "1": {"price": 55.0}} and saved["from"] == "woocommerce"
    main_module._frame_prices_cache["data"] = None
    client.get("/api/frames")
    assert len(frames) == 1                                  # WooCommerce asked only the first time


def test_studio_edits_the_price_and_the_shop_uses_it(client, owner_client, frames, main_module):
    r = owner_client.post("/api/admin/frames/prices", json={"sizes": [{"size_idx": 1, "price": "64.5"}, {"size_idx": 3, "price": 120}]})
    assert r.status_code == 200 and r.json()["sizes"]["1"] == {"price": 64.5}
    assert main_module._frame_price(1059) == 64.5 and main_module._frame_price(1060) == 64.5 and main_module._frame_price(1063) == 120
    prices = client.get("/api/shop-prices").json()["frame_prices"]
    assert prices[1] == {"landscape": 64.5, "portrait": 64.5} and prices[0]["landscape"] == 40.0
    with pytest.raises(main_module.CheckoutError):
        main_module._frame_price(1061)                       # 10x13: no price, not sold


@pytest.mark.parametrize("bad", ["", "0", "-5", "abc", None])
def test_blank_zero_or_invalid_price_refused(owner_client, frames, bad):
    r = owner_client.post("/api/admin/frames/prices", json={"sizes": [{"size_idx": 1, "price": bad}]})
    assert r.status_code == 400
    assert owner_client.post("/api/admin/frames/prices", json={"sizes": [{"size_idx": 9, "price": "50"}]}).status_code == 400
