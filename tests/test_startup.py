"""While the photo index is loading the site says 'starting up' and nothing can save."""
import pytest


@pytest.fixture()
def loading(main_module):
    main_module._data_ready.clear()
    yield
    main_module._data_ready.set()


def test_healthz_ok_when_loaded(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_healthz_503_while_loading(client, loading):
    assert client.get("/healthz").status_code == 503


@pytest.mark.parametrize("path", ["/", "/pos", "/admin/dashboard"])
def test_pages_show_starting_up_while_loading(client, loading, path):
    r = client.get(path)
    assert r.status_code == 503 and "starting up" in r.text.lower()


def test_api_refuses_while_loading(client, admin_client, loading):
    assert client.get("/api/days").status_code == 503
    # a save (push-live) must not run against the empty in-memory index
    assert admin_client.post("/api/admin/push-live",
                             json={"date": "2026-10-01", "location": "x", "folder": ""}).status_code == 503


def test_customer_download_links_still_work_while_loading(client, loading):
    # an unknown token is a normal 404 from the download page, not the startup page
    assert client.get("/download/notarealtoken").status_code == 404
    assert client.get("/package/notarealtoken").status_code == 404


def test_failed_read_retries_instead_of_starting_empty(main_module, monkeypatch):
    calls = {"n": 0}
    real_get = main_module.s3.get_object

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("R2 timeout")
        return real_get(**kw)

    import boto3, json
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket="test-bucket", Key="images.json", Body=json.dumps([{"path": "images/a.jpg", "date": "2026-01-01",
                                                                    "location": "X"}]).encode())
    monkeypatch.setattr(main_module.s3, "get_object", flaky)
    monkeypatch.setattr(main_module._time, "sleep", lambda s: None)
    saved = main_module.data
    try:
        main_module._data_ready.clear()
        main_module._load_data_bg()
        assert calls["n"] == 3 and len(main_module.data) == 1 and main_module._data_ready.is_set()
    finally:
        main_module.data = saved
        main_module._data_ready.set()
