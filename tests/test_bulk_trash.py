"""Studio: Move to Trash for several selected photos at once (recoverable for 7 days).
Photos stay where they are while in the trash (nothing is copied, so it's quick); only the
thumbnails visitors see go at once. Purging deletes the files; restoring puts them back."""
import json

import boto3

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"
BASE = "images/2026-12-07/el-dorado-action/"


def _keys(prefix):
    return [o["Key"] for o in S3.list_objects_v2(Bucket=B, Prefix=prefix).get("Contents", [])]


def _setup(main_module, live_photos, n=4):
    paths = [BASE + f"T{i}.jpg" for i in range(n)]
    live_photos([{"path": p, "date": "2026-12-07", "location": "El Dorado Action", "group": "", "last_name": "",
                  "jacket_colors": ["red"]} for p in paths])
    for p in paths:
        S3.put_object(Bucket=B, Key=p, Body=b"jpeg")
        S3.put_object(Bucket=B, Key=main_module._THUMB_VARIANTS["grid"][0](p), Body=b"thumb")
        S3.put_object(Bucket=B, Key=main_module._THUMB_VARIANTS["admin"][0](p), Body=b"thumb")
    S3.put_object(Bucket=B, Key="meta/trash_meta.json", Body=b"[]")
    return paths


def _trash():
    return json.loads(S3.get_object(Bucket=B, Key="meta/trash_meta.json")["Body"].read())


def test_selected_photos_go_to_the_trash_together(admin_client, main_module, live_photos):
    paths = _setup(main_module, live_photos)
    r = admin_client.post("/api/admin/photo/trash", json={"paths": [paths[0], paths[1], paths[3]]})
    assert r.json() == {"trashed": 3, "failed": 0}
    assert {it["path"] for it in main_module.data} == {paths[2]}          # off the site at once
    assert set(_keys(BASE)) == set(paths)                                   # originals stay for the week
    grid = main_module._THUMB_VARIANTS["grid"][0]
    assert not set(_keys(grid(BASE)[:-1])) & {grid(p) for p in (paths[0], paths[1], paths[3])}   # visitors' thumbnails gone
    trash = _trash()
    assert sorted(t["original_key"] for t in trash) == [paths[0], paths[1], paths[3]]
    assert all(t["in_place"] and t["key"] == t["original_key"] and t["image_entry"]["jacket_colors"] == ["red"]
               for t in trash)
    assert not _keys("studio_trash/")                                       # nothing copied


def test_restore_puts_the_photo_back_with_its_labels(admin_client, main_module, live_photos):
    paths = _setup(main_module, live_photos, 2)
    admin_client.post("/api/admin/photo/trash", json={"paths": [paths[0]]})
    listed = admin_client.get("/api/trash").json()["items"]
    assert listed[0]["thumb_url"] and "admin" in listed[0]["thumb_url"]     # the small studio thumbnail
    assert admin_client.post("/api/trash/restore", json={"key": paths[0]}).json() == {"restored": True}
    back = next(it for it in main_module.data if it["path"] == paths[0])
    assert back["jacket_colors"] == ["red"] and _trash() == []


def test_purging_deletes_the_files_but_never_a_photo_uploaded_again(admin_client, main_module, live_photos):
    paths = _setup(main_module, live_photos, 2)
    admin_client.post("/api/admin/photo/trash", json={"paths": paths})
    # T1 is uploaded again under the same name while its old copy is in the trash
    main_module.data.append({"path": paths[1], "date": "2026-12-07", "location": "El Dorado Action", "group": "", "last_name": ""})
    assert admin_client.post("/api/trash/restore", json={"key": paths[1]}).status_code == 409
    main_module._purge_trash_entries(_trash())
    assert paths[0] not in _keys(BASE) and paths[1] in _keys(BASE)
    assert not _keys(main_module._THUMB_VARIANTS["admin"][0](paths[0]))


def test_trash_needs_the_studio(client):
    assert client.post("/api/admin/photo/trash", json={"paths": [BASE + "T0.jpg"]}).status_code == 401


def test_a_photo_on_an_active_download_link_is_never_purged(admin_client, main_module, live_photos):
    from datetime import datetime, timedelta, timezone
    paths = _setup(main_module, live_photos, 3)
    soon, past = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(), (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    S3.put_object(Bucket=B, Key="downloads/tok1.json", Body=json.dumps({"paths": [paths[0]], "expires": soon}).encode())
    S3.put_object(Bucket=B, Key="packages/pkg1/meta.json", Body=json.dumps({"paths": [paths[1]], "expires": soon}).encode())
    S3.put_object(Bucket=B, Key="downloads/old.json", Body=json.dumps({"paths": [paths[2]], "expires": past}).encode())
    admin_client.post("/api/admin/photo/trash", json={"paths": paths})
    held = main_module._purge_trash_entries(_trash())
    assert sorted(t["original_key"] for t in held) == [paths[0], paths[1]] and all(t["held"] for t in held)
    left = _keys(BASE)
    assert paths[0] in left and paths[1] in left and paths[2] not in left       # the expired link's photo goes
    for k in ("downloads/tok1.json", "packages/pkg1/meta.json", "downloads/old.json"):
        S3.delete_object(Bucket=B, Key=k)


def test_emptying_the_trash_keeps_photos_on_active_links(admin_client, main_module, live_photos):
    import time
    from datetime import datetime, timedelta, timezone
    paths = _setup(main_module, live_photos, 2)
    S3.put_object(Bucket=B, Key="downloads/tok2.json", Body=json.dumps(
        {"paths": [paths[0]], "expires": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()}).encode())
    admin_client.post("/api/admin/photo/trash", json={"paths": paths})
    assert admin_client.post("/api/trash/empty").json() == {"emptied": 2}
    for _ in range(50):
        if _trash():
            break
        time.sleep(0.05)
    assert [t["original_key"] for t in _trash()] == [paths[0]]                   # back in the trash, marked held
    assert paths[0] in _keys(BASE) and paths[1] not in _keys(BASE)
    S3.delete_object(Bucket=B, Key="downloads/tok2.json")
