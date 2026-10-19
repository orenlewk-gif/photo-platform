"""Per-visitor limits behind Railway's proxy.

Railway connects to the app from shared internal addresses (100.64.x.x) and passes
the visitor's real address in X-Forwarded-For. Limits and lockouts must count each
visitor separately, and a visitor must not be able to dodge them with fake headers."""
import pytest
from starlette.requests import Request


def _req(xff=None, peer="100.64.0.8"):
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234)})


@pytest.mark.parametrize("xff,expected", [
    ("76.75.12.161", "76.75.12.161"),                       # what Railway sends
    ("1.2.3.4, 76.75.12.161", "76.75.12.161"),              # fake entry added by the visitor is ignored
    ("76.75.12.161, 100.64.0.3", "76.75.12.161"),           # Railway's own hop is skipped
    ("not-an-ip, 76.75.12.161", "76.75.12.161"),
    ("2601:1c0:4000::1", "2601:1c0:4000::1"),
    ("10.0.0.5", "100.64.0.8"),                             # nothing public: fall back to the connection
    (None, "100.64.0.8"),
])
def test_visitor_address(main_module, xff, expected):
    assert main_module._client_ip(_req(xff)) == expected


def test_visitors_behind_the_same_railway_proxy_have_their_own_search_limit(client, main_module):
    main_module._lastname_limiter._calls.clear()
    busy = {"X-Forwarded-For": "203.0.113.10"}
    for _ in range(30):
        assert client.get("/api/last-name-search?q=zz", headers=busy).status_code == 200
    assert client.get("/api/last-name-search?q=zz", headers=busy).status_code == 429     # that visitor is limited
    other = {"X-Forwarded-For": "198.51.100.20"}
    assert client.get("/api/last-name-search?q=zz", headers=other).status_code == 200    # everyone else isn't


def test_fake_headers_dont_dodge_the_limit(client, main_module):
    main_module._lastname_limiter._calls.clear()
    for i in range(30):
        client.get("/api/last-name-search?q=zz", headers={"X-Forwarded-For": f"9.9.9.{i}, 203.0.113.10"})
    r = client.get("/api/last-name-search?q=zz", headers={"X-Forwarded-For": "8.8.8.8, 203.0.113.10"})
    assert r.status_code == 429


def test_wrong_pins_lock_out_only_that_visitor(client, main_module):
    import boto3, json
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket="test-bucket", Key="meta/photographers.json",
        Body=json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    main_module._pin_fail_limiter._calls.clear()
    guesser = {"X-Forwarded-For": "203.0.113.66"}
    for _ in range(10):
        client.post("/api/photographer/auth", json={"pin": "0000"}, headers=guesser)
    assert client.post("/api/photographer/auth", json={"pin": "4821"}, headers=guesser).status_code == 429
    ipad = {"X-Forwarded-For": "198.51.100.7"}
    assert client.post("/api/photographer/auth", json={"pin": "4821"}, headers=ipad).status_code == 200
