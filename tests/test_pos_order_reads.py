"""In-store sales are read once and remembered; only new/changed sale files and the days asked for are opened."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"


@pytest.fixture()
def sales(main_module, monkeypatch):
    for o in S3.list_objects_v2(Bucket=B, Prefix="pos_orders/").get("Contents", []):
        S3.delete_object(Bucket=B, Key=o["Key"])
    main_module._pos_order_cache.clear()
    reads = []
    real_get = main_module.s3.get_object
    monkeypatch.setattr(main_module.s3, "get_object",
                        lambda **kw: (reads.append(kw["Key"]) if kw["Key"].startswith("pos_orders/") else None) or real_get(**kw))

    def put(day, sid, **kw):
        rec = {"id": sid, "date": day, "created": f"{day}T12:00:{sid[-2:]}", "total": 25, **kw}
        S3.put_object(Bucket=B, Key=f"pos_orders/{day}_{sid}.json", Body=json.dumps(rec).encode())
        return rec
    yield put, reads
    for o in S3.list_objects_v2(Bucket=B, Prefix="pos_orders/").get("Contents", []):
        S3.delete_object(Bucket=B, Key=o["Key"])
    main_module._pos_order_cache.clear()


def test_sales_are_read_once_and_remembered(main_module, sales):
    put, reads = sales
    for i in range(5):
        put("2026-12-01", f"s{i:02d}")
    assert [o["id"] for o in main_module._fetch_pos_orders()] == ["s04", "s03", "s02", "s01", "s00"]
    assert len(reads) == 5
    main_module._fetch_pos_orders()
    assert len(reads) == 5                                   # nothing re-read the second time


def test_a_changed_or_new_sale_is_read_again(main_module, sales):
    put, reads = sales
    put("2026-12-01", "s01"); put("2026-12-01", "s02")
    main_module._fetch_pos_orders()
    put("2026-12-01", "s02", square_fee=0.79)                 # e.g. Square's fee filled in later
    put("2026-12-02", "s03")
    orders = {o["id"]: o for o in main_module._fetch_pos_orders()}
    assert orders["s02"]["square_fee"] == 0.79 and "s03" in orders
    assert reads[2:] == ["pos_orders/2026-12-01_s02.json", "pos_orders/2026-12-02_s03.json"] or \
           sorted(reads[2:]) == ["pos_orders/2026-12-01_s02.json", "pos_orders/2026-12-02_s03.json"]


def test_only_the_days_asked_for_are_opened(main_module, sales):
    put, reads = sales
    put("2026-11-30", "s01"); put("2026-12-01", "s02"); put("2026-12-05", "s03")
    assert [o["id"] for o in main_module._fetch_pos_orders(after="2026-12-01", before="2026-12-02")] == ["s02"]
    assert reads == ["pos_orders/2026-12-01_s02.json"]


def test_one_screen_changing_a_sale_doesnt_change_what_others_see(main_module, sales):
    put, _ = sales
    put("2026-12-01", "s01")
    main_module._fetch_pos_orders()[0]["total"] = 999
    assert main_module._fetch_pos_orders()[0]["total"] == 25
