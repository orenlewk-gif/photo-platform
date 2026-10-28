"""Studio upload: only photo files, always into images/<date>/<location>[/<family>]/.
Winter action ignores the folder name (location chosen at upload); portrait families keep
their folders and their poses, which are matched by file name."""
import pytest

DATE = "2026-12-05"


def _presign(admin_client, location, files, folder="", date=DATE):
    return admin_client.post("/api/admin/upload/presign", json={
        "date": date, "location": location, "folder": folder,
        "files": [{"name": n, "type": t} for n, t in files]})


def _index(admin_client, location, keys, folder="", poses=None, date=DATE):
    body = {"date": date, "location": location, "folder": folder, "keys": keys}
    if poses is not None:
        body["poses"] = poses
    return admin_client.post("/api/admin/upload/index", json=body)


@pytest.fixture(autouse=True)
def fresh_list(live_photos):
    live_photos([])


def test_winter_action_goes_by_location_and_ignores_folder_names(admin_client):
    r = _presign(admin_client, "Big Sky Ski School", [("DSC_0001.JPG", "image/jpeg")], folder="whatever")
    assert r.status_code == 200
    [u] = r.json()["urls"]
    assert u["key"] == f"images/{DATE}/big-sky-ski-school/DSC_0001.JPG"
    r = _index(admin_client, "Big Sky Ski School", [u["key"]], folder="whatever")
    assert r.status_code == 200 and r.json()["indexed"] == 1


def test_portrait_families_keep_their_folders_and_poses(admin_client, main_module):
    files = [("WWB_0101.jpg", "image/jpeg"), ("WWB_0102.jpg", "image/jpeg"), ("WWB_0103.png", "image/png")]
    r = _presign(admin_client, "Lone Peak Portraits", files, folder="O'Brien Family")
    assert r.status_code == 200
    keys = [u["key"] for u in r.json()["urls"]]
    assert keys[0] == f"images/{DATE}/lone-peak-portraits/o'brien-family/WWB_0101.jpg"   # same naming as before
    poses = [{"color": "#f00", "label": "Pose 1", "files": ["WWB_0101.jpg", "WWB_0102.jpg"]},
             {"color": "#0f0", "label": "Pose 2", "files": ["WWB_0103.png"]}]
    r = _index(admin_client, "Lone Peak Portraits", keys, folder="O'Brien Family", poses=poses)
    assert r.status_code == 200 and r.json()["indexed"] == 3
    fk = main_module._folder_key(DATE, "Lone Peak Portraits", "O'Brien Family")
    saved = main_module._load_folder_meta()[fk]["poses"]
    assert [p["files"] for p in saved] == [["WWB_0101.jpg", "WWB_0102.jpg"], ["WWB_0103.png"]]
    photo = next(i for i in main_module.data if i["path"] == keys[0])
    assert photo["last_name"] == "O'Brien Family" and photo["location"] == "Lone Peak Portraits"


def test_a_batch_of_families_each_gets_its_own_folder(admin_client):
    for family in ("Smith", "Garcia-Lopez", "St. John"):
        r = _presign(admin_client, "Lone Peak Portraits", [("IMG_1.jpg", "image/jpeg")], folder=family)
        [u] = r.json()["urls"]
        assert _index(admin_client, "Lone Peak Portraits", [u["key"]], folder=family).status_code == 200
        assert f"/lone-peak-portraits/{family.lower().replace(' ', '-')}/IMG_1.jpg" in u["key"]


@pytest.mark.parametrize("name,ctype", [
    ("x.php", "image/jpeg"), ("x.html", "text/html"), ("x.svg", "image/svg+xml"), ("x.jpg", "text/html"),
    ("shell.php.jpg", "image/svg+xml"), ("notes.txt", "text/plain"),
])
def test_only_photos_can_be_uploaded(admin_client, name, ctype):
    r = _presign(admin_client, "Big Sky Ski School", [("DSC_1.JPG", "image/jpeg"), (name, ctype)])
    assert r.status_code == 400 and "Only photos" in r.json()["error"]


@pytest.mark.parametrize("location,folder,date", [
    ("Lone Peak Portraits", "../meta", DATE), ("Lone Peak Portraits", "a/b", DATE),
    ("Lone Peak Portraits", "..", DATE), ("../../meta", "", DATE), ("Big Sky Ski School", "", "../x"),
])
def test_names_cant_point_outside_the_upload_folder(admin_client, location, folder, date):
    r = _presign(admin_client, location, [("IMG_1.jpg", "image/jpeg")], folder=folder, date=date)
    assert r.status_code == 400


@pytest.mark.parametrize("key", [
    "meta/pricing.json", f"images/{DATE}/other-place/IMG_1.jpg", f"images/{DATE}/big-sky-ski-school/x.php",
    f"images/{DATE}/big-sky-ski-school/sub/IMG_1.jpg", f"images/{DATE}/big-sky-ski-school/../IMG_1.jpg",
])
def test_only_this_uploads_photos_join_the_photo_list(admin_client, main_module, key):
    r = _index(admin_client, "Big Sky Ski School", [key])
    assert r.status_code == 400 and not any(i["path"] == key for i in main_module.data)


def test_uploads_need_the_studio_login(client):
    assert _presign(client, "Big Sky Ski School", [("IMG_1.jpg", "image/jpeg")]).status_code == 401


def test_orange_labeled_action_photos_are_marked_eagle_mount(admin_client, main_module):
    files = [("DSC_1.JPG", "image/jpeg"), ("DSC_2.JPG", "image/jpeg"), ("DSC_3.JPG", "image/jpeg")]
    keys = [u["key"] for u in _presign(admin_client, "Big Sky Ski School", files).json()["urls"]]
    r = admin_client.post("/api/admin/upload/index", json={
        "date": DATE, "location": "Big Sky Ski School", "folder": "", "keys": keys,
        "partner_files": {"eagle-mount": ["DSC_2.JPG", "DSC_3.JPG"], "made-up": ["DSC_1.JPG"]}})
    assert r.status_code == 200 and r.json()["partner_marked"] == 2
    marks = {i["path"].rsplit("/", 1)[-1]: i.get("partner") for i in main_module.data}
    assert marks == {"DSC_1.JPG": None, "DSC_2.JPG": "eagle-mount", "DSC_3.JPG": "eagle-mount"}


def test_portrait_colors_are_poses_never_partners(admin_client, main_module):
    keys = [u["key"] for u in _presign(admin_client, "Lone Peak Portraits", [("P1.jpg", "image/jpeg")],
                                       folder="Smith").json()["urls"]]
    r = admin_client.post("/api/admin/upload/index", json={
        "date": DATE, "location": "Lone Peak Portraits", "folder": "Smith", "keys": keys,
        "partner_files": {"eagle-mount": ["P1.jpg"]}})
    assert r.status_code == 200 and r.json()["partner_marked"] == 0
    assert all("partner" not in i for i in main_module.data)


def test_partner_settings_default_to_eagle_mount_orange(admin_client, client):
    assert admin_client.get("/api/admin/partners").json()["partners"][0] == {
        "id": "eagle-mount", "name": "Eagle Mount", "label": "Orange", "slug": "eaglemount", "count": 0}
    assert client.get("/api/admin/partners").status_code == 401


def test_label_reader_finds_the_color_written_while_culling(main_module):
    xmp = (b'\xff\xd8\xff\xe1\x00\x10http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><rdf:Description '
           b'xmp:Label="Orange" xmp:Rating="3"/></x:xmpmeta>')
    assert main_module.read_xmp_color_label(xmp) == "Orange"
    assert main_module.read_xmp_color_label(b'\xff\xd8 no metadata') == ""


def test_portrait_families_are_live_on_upload_with_their_poses(admin_client, client, main_module):
    # Poses are set before upload, so a family is on the site (and its pose prices) right away;
    # winter action photos wait for push-live (it labels their colors first)
    files = [("P1.jpg", "image/jpeg"), ("P2.jpg", "image/jpeg"), ("P3.jpg", "image/jpeg")]
    keys = [u["key"] for u in _presign(admin_client, "Lone Peak Portraits", files, folder="Nguyen").json()["urls"]]
    poses = [{"color": "#f00", "label": "Pose 1", "files": ["P1.jpg", "P2.jpg"]},
             {"color": "#ff0", "label": "Pose 2", "files": ["P3.jpg"]}]     # the photo with no label: its own pose
    assert _index(admin_client, "Lone Peak Portraits", keys, folder="Nguyen", poses=poses).status_code == 200
    assert not any(i.get("draft") for i in main_module.data if i["path"] in keys)
    params = {"date": DATE, "location": "Lone Peak Portraits", "family": "Nguyen"}
    assert len(client.get("/api/browse", params={**params, "dims": 0}).json()["photos"]) == 3
    p = client.get("/api/folder/poses", params=params).json()
    assert p["poses_live"] and [len(x["files"]) for x in p["poses"]] == [2, 1]
    action = [u["key"] for u in _presign(admin_client, "Big Sky Ski School", [("A1.JPG", "image/jpeg")]).json()["urls"]]
    _index(admin_client, "Big Sky Ski School", action)
    assert next(i for i in main_module.data if i["path"] == action[0]).get("draft") is True


def test_photos_already_on_the_site_are_marked_when_dropped_again(admin_client):
    # After an upload with failures, dropping the folder again offers to skip what's already in
    files = [("DSC_7001.JPG", "image/jpeg"), ("DSC_7002.JPG", "image/jpeg")]
    first = _presign(admin_client, "Big Sky Ski School", files).json()["urls"]
    assert [u["on_site"] for u in first] == [False, False]
    _index(admin_client, "Big Sky Ski School", [first[0]["key"]])               # only the first made it
    again = _presign(admin_client, "Big Sky Ski School", files).json()["urls"]
    assert [u["on_site"] for u in again] == [True, False]


def test_a_different_photo_with_a_name_already_in_the_folder_is_flagged(admin_client, main_module):
    import boto3
    s3 = boto3.client("s3", region_name="us-east-1")
    [u] = _presign(admin_client, "Big Sky Ski School", [("DSC_0042.JPG", "image/jpeg")]).json()["urls"]
    s3.put_object(Bucket="test-bucket", Key=u["key"], Body=b"x" * 5000)
    _index(admin_client, "Big Sky Ski School", [u["key"]])
    same = admin_client.post("/api/admin/upload/presign", json={"date": DATE, "location": "Big Sky Ski School", "folder": "",
        "files": [{"name": "DSC_0042.JPG", "type": "image/jpeg", "size": 5000}]}).json()["urls"][0]
    other = admin_client.post("/api/admin/upload/presign", json={"date": DATE, "location": "Big Sky Ski School", "folder": "",
        "files": [{"name": "DSC_0042.JPG", "type": "image/jpeg", "size": 7777}]}).json()["urls"][0]
    assert same["on_site"] and not same.get("name_taken")                  # the same photo, dropped again
    assert not other["on_site"] and other["name_taken"]                    # another camera's DSC_0042


def test_upload_links_come_on_both_storage_addresses(admin_client, main_module, monkeypatch):
    # Chrome sends 6 at a time per address: the second address lets 12 photos upload at once
    import boto3
    from botocore.config import Config
    from urllib.parse import urlparse
    alt = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    monkeypatch.setattr(main_module, "s3_alt_host", alt)
    [u] = _presign(admin_client, "Big Sky Ski School", [("DSC_0500.JPG", "image/jpeg")]).json()["urls"]
    a, b = urlparse(u["url"]), urlparse(u["url_alt"])
    assert a.netloc != b.netloc and b.netloc.startswith("test-bucket.") and u["key"].lstrip("/") in b.path.lstrip("/")
    monkeypatch.setattr(main_module, "s3_alt_host", None)
    [u] = _presign(admin_client, "Big Sky Ski School", [("DSC_0501.JPG", "image/jpeg")]).json()["urls"]
    assert "url_alt" not in u                                                   # local fake storage: one address
