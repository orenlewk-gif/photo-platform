"""Studio: Move to Trash for several selected photos at once (recoverable for 7 days)."""
import json

import boto3

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"
BASE = "images/2026-12-07/el-dorado-action/"


def _keys(prefix):
    return [o["Key"] for o in S3.list_objects_v2(Bucket=B, Prefix=prefix).get("Contents", [])]


def test_selected_photos_go_to_the_trash_together(admin_client, main_module, live_photos):
    paths = [BASE + f"T{i}.jpg" for i in range(4)]
    live_photos([{"path": p, "date": "2026-12-07", "location": "El Dorado Action", "group": "", "last_name": ""}
                 for p in paths])
    for p in paths[:3]:                                  # T3's original is missing: it can't be copied
        S3.put_object(Bucket=B, Key=p, Body=b"jpeg")
    S3.put_object(Bucket=B, Key="meta/trash_meta.json", Body=b"[]")
    r = admin_client.post("/api/admin/photo/trash", json={"paths": [paths[0], paths[1], paths[3]]})
    assert r.json() == {"trashed": 2, "failed": 1}
    left = {it["path"] for it in main_module.data}
    assert left == {paths[2], paths[3]}                  # the one that couldn't be moved stays on the site
    assert set(_keys(BASE)) == {paths[2]}                # originals of the trashed ones are gone
    trash = json.loads(S3.get_object(Bucket=B, Key="meta/trash_meta.json")["Body"].read())
    assert sorted(t["original_key"] for t in trash) == [paths[0], paths[1]]
    assert all(t["key"] in _keys("studio_trash/") and t["image_entry"]["path"] == t["original_key"] for t in trash)


def test_trash_needs_the_studio(client):
    assert client.post("/api/admin/photo/trash", json={"paths": [BASE + "T0.jpg"]}).status_code == 401
