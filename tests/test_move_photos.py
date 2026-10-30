"""Studio: moving the photos selected in a folder (not the whole folder) to another date, location
and folder. Labels go with them, nothing is overwritten, times and portrait poses follow."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"
D = "2026-12-05"
SRC = f"images/{D}/big-sky-ski-school/"


def _photo(name, **extra):
    return {"path": SRC + name, "date": D, "location": "Big Sky Ski School", "group": "", "last_name": "",
            "jacket_colors": ["red"], "pants_colors": ["black"], "sport": "ski", **extra}


@pytest.fixture()
def photos(live_photos, main_module):
    items = [_photo("A1.JPG", partner="eagle-mount"), _photo("A2.JPG"), _photo("A3.JPG")]
    for it in items:
        S3.put_object(Bucket=B, Key=it["path"], Body=b"jpeg-" + it["path"].encode())
    live_photos(items)
    main_module._folder_times_cache.clear()
    S3.put_object(Bucket=B, Key=main_module._times_r2_key(D, "Big Sky Ski School"),
                  Body=json.dumps({"A1.JPG": 600, "A2.JPG": 601, "A3.JPG": 602}).encode())
    yield items
    for loc in ("Big Sky Ski School", "El Dorado Action"):
        for d in (D, "2026-12-06"):
            S3.delete_object(Bucket=B, Key=main_module._times_r2_key(d, loc))
    main_module._folder_times_cache.clear()


def _move(admin_client, paths, **dest):
    return admin_client.post("/api/admin/photos/move", json={"paths": paths, **dest})


def test_selected_photos_move_with_their_labels_and_times(admin_client, photos, main_module):
    r = _move(admin_client, [SRC + "A1.JPG", SRC + "A2.JPG"], new_date="2026-12-06", new_location="El Dorado Action")
    assert r.status_code == 200 and r.json() == {"moved": 2, "skipped": [], "failed": 0}
    by = {i["path"].rsplit("/", 1)[1]: i for i in main_module.data}
    a1 = by["A1.JPG"]
    assert a1["path"] == "images/2026-12-06/el-dorado-action/A1.JPG" and a1["location"] == "El Dorado Action"
    assert a1["jacket_colors"] == ["red"] and a1["partner"] == "eagle-mount"            # labels and marks kept
    assert by["A3.JPG"]["path"] == SRC + "A3.JPG"                                       # the unselected one stays
    assert S3.get_object(Bucket=B, Key=a1["path"])["Body"].read() == b"jpeg-" + (SRC + "A1.JPG").encode()
    with pytest.raises(Exception):
        S3.head_object(Bucket=B, Key=SRC + "A1.JPG")                                    # moved, not copied
    times = admin_client.get("/api/folder-times", params={"date": "2026-12-06", "location": "El Dorado Action"}).json()["times"]
    assert times == {"A1.JPG": 600, "A2.JPG": 601}


def test_a_name_already_in_the_new_folder_is_skipped_not_overwritten(admin_client, photos, main_module, live_photos):
    other = {"path": "images/2026-12-06/el-dorado-action/A2.JPG", "date": "2026-12-06", "location": "El Dorado Action",
             "group": "", "last_name": ""}
    S3.put_object(Bucket=B, Key=other["path"], Body=b"the photo already there")
    live_photos(photos + [other])
    r = _move(admin_client, [SRC + "A1.JPG", SRC + "A2.JPG"], new_date="2026-12-06", new_location="El Dorado Action")
    assert r.json()["moved"] == 1 and r.json()["skipped"] == ["A2.JPG"]
    assert S3.get_object(Bucket=B, Key=other["path"])["Body"].read() == b"the photo already there"
    assert any(i["path"] == SRC + "A2.JPG" for i in main_module.data)


def test_portraits_need_a_family_and_poses_follow(admin_client, photos, main_module):
    assert _move(admin_client, [SRC + "A1.JPG"], new_date=D, new_location="Lone Peak Portraits").status_code == 400
    fm = main_module._load_folder_meta()
    fk = main_module._folder_key(D, "Lone Peak Portraits", "Smith")
    fm[fk] = {"poses": [{"color": "red", "label": "Pose 1", "files": ["S1.JPG"]}], "poses_live": True}
    main_module._save_folder_meta(fm)
    r = _move(admin_client, [SRC + "A1.JPG", SRC + "A3.JPG"], new_date=D, new_location="Lone Peak Portraits", new_folder="Smith")
    assert r.json()["moved"] == 2
    poses = main_module._load_folder_meta()[fk]["poses"]
    assert poses[-1] == {"color": "", "label": "Pose 2", "files": ["A1.JPG", "A3.JPG"]}
    moved = [i for i in main_module.data if i["path"].endswith(("A1.JPG", "A3.JPG"))]
    assert all(i["last_name"] == "Smith" and i["location"] == "Lone Peak Portraits" for i in moved)
    fm = main_module._load_folder_meta(); fm.pop(fk, None); main_module._save_folder_meta(fm)


def test_only_the_studio_can_move_photos(client, photos):
    assert _move(client, [SRC + "A1.JPG"], new_date=D, new_location="El Dorado Action").status_code == 401
