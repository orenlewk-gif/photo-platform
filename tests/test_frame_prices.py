"""Studio frame-price editing pushes prices to WooCommerce safely."""
import pytest


class _Resp:
    def __init__(self, status=200):
        self.status_code = status


@pytest.fixture()
def wc(main_module, monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "WC_KEY", "ck_test")
    monkeypatch.setattr(main_module, "WC_SECRET", "cs_test")

    def fake_put(url, json=None, **kw):
        calls.append((url, json))
        return _Resp(200)
    monkeypatch.setattr(main_module.http_requests, "put", fake_put)
    return calls


def test_requires_admin(client, wc):
    assert client.post("/api/admin/frames/prices", json=[{"variation_id": 1057, "price": "60"}]).status_code == 401
    assert wc == []


def test_valid_price_sent_to_woocommerce(owner_client, wc):
    r = owner_client.post("/api/admin/frames/prices", json=[{"variation_id": 1057, "price": "64.5"}])
    assert r.json()["results"] == [{"variation_id": 1057, "ok": True}]
    assert wc[0][0].endswith("/products/1034/variations/1057")
    assert wc[0][1] == {"regular_price": "64.50"}


@pytest.mark.parametrize("bad", ["", "0", "-5", "abc", None])
def test_blank_zero_or_invalid_price_refused(owner_client, wc, bad):
    r = owner_client.post("/api/admin/frames/prices", json=[{"variation_id": 1057, "price": bad}])
    assert r.json()["results"][0]["ok"] is False
    assert wc == []
