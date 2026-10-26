"""The labeling jobs (push-live, Re-classify Selected, Classify Folder, backfill) share _label_photos."""
import json
import time

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"


def _p(name, loc="El Dorado Action", **kw):
    slug = loc.lower().replace(" ", "-")
    return {"path": f"images/2026-12-12/{slug}/{name}.jpg", "date": "2026-12-12", "location": loc,
            "last_name": "", "group": "", **kw}


@pytest.fixture()
def lab(main_module, monkeypatch):
    saved = main_module.data
    seen = []
    monkeypatch.setattr(main_module, "_load_onnx_model", lambda: True)
    monkeypatch.setattr(main_module, "classify_photo_colors",
                        lambda raw: seen.append(raw) or {"sport": "ski", "jacket_colors": ["red"], "pants_colors": ["black"]})
    yield seen
    main_module.data = saved


def _put_photos(items):
    for it in items:
        S3.put_object(Bucket=B, Key=it["path"], Body=it["path"].encode())


def _wait(admin_client, job_id):
    end = time.time() + 10
    while time.time() < end:
        if admin_client.get(f"/api/admin/embed-status?job_id={job_id}").json()["finished"]:
            return
        time.sleep(0.05)
    raise AssertionError("job didn't finish")


def _stored(main_module):
    return {p["path"]: p for p in json.loads(S3.get_object(Bucket=B, Key="images.json")["Body"].read())}


def test_classify_folder_labels_unlabeled_photos_and_saves(admin_client, main_module, lab):
    items = [_p("A1"), _p("A2"), _p("A3", jacket_colors=["blue"])]
    main_module.data = items; _put_photos(items)
    r = admin_client.post("/api/admin/classify-folder", json={"date": "2026-12-12", "location": "El Dorado Action"}).json()
    assert r["queued"] == 2
    _wait(admin_client, r["job_id"])
    stored = _stored(main_module)
    assert stored[items[0]["path"]]["jacket_colors"] == ["red"] and stored[items[0]["path"]]["classifier"] == main_module.MODEL_LABEL
    assert stored[items[2]["path"]]["jacket_colors"] == ["blue"]        # already labeled: left alone


def test_reclassify_relabels_the_chosen_photos(admin_client, main_module, lab):
    items = [_p("B1", jacket_colors=["green"], manually_corrected=True), _p("B2", jacket_colors=["green"])]
    main_module.data = items; _put_photos(items)
    r = admin_client.post("/api/admin/reclassify-photos", json={"paths": [items[0]["path"]]}).json()
    _wait(admin_client, r["job_id"])
    assert items[0]["jacket_colors"] == ["red"] and items[1]["jacket_colors"] == ["green"]


def test_push_live_labeling_skips_portraits_and_labeled_photos_without_downloading(main_module, lab):
    items = [_p("C1"), _p("C2", jacket_colors=["blue"]), _p("C3", loc="Lone Peak Portraits")]
    _put_photos(items)
    assert main_module.embed_items_fast(items) == 3
    assert lab == [items[0]["path"].encode()]                          # only the one that needed it was read
    assert items[0]["classifier"] == main_module.MODEL_LABEL and "classifier" not in items[2]


def test_a_photo_that_cant_be_read_is_skipped_not_fatal(main_module, lab):
    items = [_p("D1"), _p("MISSING")]
    _put_photos(items[:1])
    assert main_module._label_photos(items) == 1
    assert items[0]["jacket_colors"] == ["red"] and "jacket_colors" not in items[1]
