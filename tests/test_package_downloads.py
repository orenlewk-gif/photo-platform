"""Customer download page: direct links for Save to Photos, and a streamed zip that's complete."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
PATHS = [f"images/2026-06-12/base/DSC_{n}.JPG" for n in range(1, 8)]


def _pkg(token, expires):
    S3.put_object(Bucket="test-bucket", Key=f"packages/{token}/meta.json", Body=json.dumps({
        "token": token, "title": "Your Photos — Base", "emails": ["ann@example.com"], "paths": PATHS,
        "created": datetime.now(timezone.utc).isoformat(), "expires": expires.isoformat()}).encode())


@pytest.fixture()
def package(main_module):
    for i, p in enumerate(PATHS):
        S3.put_object(Bucket="test-bucket", Key=p, Body=f"photo-{i}".encode() * 1000)
    _pkg("tok-live", datetime.now(timezone.utc) + timedelta(days=30))
    _pkg("tok-old", datetime.now(timezone.utc) - timedelta(days=1))
    return "tok-live"


def test_links_list_every_original_in_order(client, package):
    files = client.get(f"/package/{package}/links").json()["files"]
    assert [f["name"] for f in files] == [p.rsplit("/", 1)[-1] for p in PATHS]
    assert all("X-Amz-Signature" in f["url"] or "Signature" in f["url"] for f in files)


def test_links_refused_for_expired_or_unknown_packages(client, package):
    assert client.get("/package/tok-old/links").status_code == 410
    assert client.get("/package/nope/links").status_code == 404


def test_zip_contains_every_photo_intact_and_in_order(client, package):
    r = client.get(f"/package/{package}/all")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    assert zf.namelist() == [p.rsplit("/", 1)[-1] for p in PATHS]
    for i, name in enumerate(zf.namelist()):
        assert zf.read(name) == f"photo-{i}".encode() * 1000


def test_zip_skips_a_missing_photo_but_keeps_the_rest(client, package):
    S3.delete_object(Bucket="test-bucket", Key=PATHS[2])
    zf = zipfile.ZipFile(io.BytesIO(client.get(f"/package/{package}/all").content))
    assert zf.testzip() is None and len(zf.namelist()) == len(PATHS) - 1


def test_page_offers_save_to_photos(client, package):
    html = client.get(f"/package/{package}").text
    assert 'id="save-photos"' in html and "navigator.share" in html and "/package/tok-live/links" in html


def test_download_page_escapes_the_order_location(client, main_module):
    from datetime import datetime, timedelta, timezone
    exp = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    main_module._store_token("tokxss1", {"order_id": 7, "location": '<script>alert(1)</script>', "date": "2026-10-18",
                                         "paths": ["images/2026-10-18/x/A.jpg"], "filenames": "A.jpg", "expires": exp})
    html = client.get("/download/tokxss1").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_regenerated_link_page_escapes_the_customers_name(admin_client, main_module, monkeypatch):
    from datetime import datetime, timedelta, timezone
    exp = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    rec = {"order_id": 8, "customer": '<img src=x onerror=alert(1)>', "email": "a@x.com", "paths": ["images/x/A.jpg"], "expires": exp}
    monkeypatch.setattr(main_module, "_get_token_by_order_id", lambda oid: ("oldtok", dict(rec)))
    html = admin_client.get("/admin/regen/8").text
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
