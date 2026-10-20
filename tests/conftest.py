"""
Test harness: every test runs against an in-memory fake of R2 (moto).

main.py talks to R2 the moment it is imported (CORS, migrations, pricing seed),
so the fake must be running and the environment pointed at it *before* main is
imported. The guards below refuse to run if anything could reach production.
"""
import os
import sys

import pytest

TEST_BUCKET = "test-bucket"

# Point boto3 at a fake AWS endpoint that moto intercepts — never the real R2.
os.environ.update({
    "R2_ENDPOINT_URL":       "https://s3.us-east-1.amazonaws.com",
    "R2_ACCESS_KEY_ID":      "testing",
    "R2_SECRET_ACCESS_KEY":  "testing",
    "R2_BUCKET_NAME":        TEST_BUCKET,
    "AWS_DEFAULT_REGION":    "us-east-1",
    "ADMIN_PASSWORD":        "test-admin-password",
})
for _var in ("WC_CONSUMER_KEY", "WC_CONSUMER_SECRET", "RESEND_API_KEY", "ANTHROPIC_API_KEY",
             "SQUARE_ACCESS_TOKEN", "WC_WEBHOOK_SECRET", "POS_PASSCODE"):
    os.environ.pop(_var, None)

assert "crystal-images" not in os.environ["R2_BUCKET_NAME"]
assert "r2.cloudflarestorage.com" not in os.environ["R2_ENDPOINT_URL"]

from moto import mock_aws  # noqa: E402

_mock = mock_aws()
_mock.start()

import boto3  # noqa: E402

boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)

# main.py reads templates/, frames/ and static/ relative to the working directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)


class _BlockedHTTP(Exception):
    pass


def _blocked(*args, **kwargs):
    raise _BlockedHTTP(f"Outbound HTTP blocked in tests: {args[:1]}")


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    """WooCommerce, Resend, Anthropic etc. must never be called from tests."""
    import requests
    for name in ("get", "post", "put", "delete", "patch", "request"):
        monkeypatch.setattr(requests, name, _blocked)


@pytest.fixture(scope="session")
def main_module():
    import main
    assert main.R2_BUCKET == TEST_BUCKET
    return main


@pytest.fixture()
def client(main_module):
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


@pytest.fixture()
def admin_client(main_module):
    from fastapi.testclient import TestClient
    c = TestClient(main_module.app)
    c.cookies.set(main_module.ADMIN_COOKIE, main_module._create_admin_session())
    return c


@pytest.fixture()
def owner_client(main_module):
    """Signed into the studio AND past the owner PIN (Pricing, Activities, Photographers)."""
    import time
    from fastapi.testclient import TestClient
    c = TestClient(main_module.app)
    c.cookies.set(main_module.ADMIN_COOKIE, main_module._create_admin_session())
    c.cookies.set(main_module.OWNER_COOKIE, main_module._owner_token(int(time.time()) + 600))
    return c


@pytest.fixture()
def live_photos(main_module):
    """Put photos in the live photo list for a test (restored afterwards)."""
    saved = main_module.data

    def use(items):
        main_module.data = list(items)
        main_module._index_state["version"] += 1
        return main_module.data
    yield use
    main_module.data = saved
    main_module._index_state["version"] += 1
