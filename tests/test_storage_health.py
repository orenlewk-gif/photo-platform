"""/healthz/storage: what the uptime monitor watches — fails when photo storage can't be read."""
import boto3
import pytest


@pytest.fixture(autouse=True)
def photo_list_in_storage(main_module):
    boto3.client("s3", region_name="us-east-1").put_object(Bucket="test-bucket", Key="images.json", Body=b"[]") \
        if "Contents" not in boto3.client("s3", region_name="us-east-1").list_objects_v2(
            Bucket="test-bucket", Prefix="images.json") else None


def test_storage_reachable(client, main_module):
    main_module._storage_check["ts"] = 0.0
    r = client.get("/healthz/storage")
    assert r.status_code == 200 and r.json()["storage"] == "reachable"


def test_storage_unreachable_is_an_error(client, main_module, monkeypatch):
    from botocore.exceptions import ClientError

    def denied(**kw):
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
    monkeypatch.setattr(main_module.s3, "head_object", denied)
    main_module._storage_check["ts"] = 0.0
    r = client.get("/healthz/storage")
    assert r.status_code == 503 and r.json()["storage"] == "unreachable" and r.json()["reason"] == "403"
    assert client.get("/healthz").status_code == 200        # Railway's own check is unchanged
    main_module._storage_check["ts"] = 0.0


def test_answer_is_reused_briefly(client, main_module, monkeypatch):
    main_module._storage_check["ts"] = 0.0
    assert client.get("/healthz/storage").status_code == 200
    calls = []
    monkeypatch.setattr(main_module.s3, "head_object", lambda **kw: calls.append(1))
    client.get("/healthz/storage"); client.get("/healthz/storage")
    assert calls == []
    main_module._storage_check["ts"] = 0.0
