"""Partner programs (Eagle Mount): marked photos stay out of the public gallery but on the POS,
and participants download them free on a password-protected page; downloads are logged."""
import json
import time
from io import BytesIO

import boto3
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.text import to_r2_key

S3 = boto3.client("s3", region_name="us-east-1")
DAY1, DAY2 = "2026-12-05", "2026-12-06"
PW = "eagle-test"


def _jpeg(color=(30, 120, 200), taken=None) -> bytes:
    buf = BytesIO()
    exif = Image.Exif()
    if taken:
        exif[306] = taken                                # DateTime, "YYYY:MM:DD HH:MM:SS"
    Image.new("RGB", (1200, 800), color).save(buf, "JPEG", quality=85, exif=exif.tobytes())
    return buf.getvalue()


def _photo(name, date=DAY1, partner=None):
    item = {"path": f"images/{date}/big-sky-ski-school/{name}", "date": date, "location": "Big Sky Ski School",
            "last_name": "", "group": ""}
    if partner:
        item["partner"] = partner
    return item


PHOTOS = [_photo("DSC_0001.JPG"), _photo("DSC_0002.JPG", partner="eagle-mount"),
          _photo("DSC_0003.JPG", partner="eagle-mount"), _photo("DSC_0010.JPG", date=DAY2, partner="eagle-mount"),
          _photo("DSC_0020.JPG", date="2026-12-07", partner="eagle-mount")]   # a day with only Eagle Mount
MARKED = [bool(p.get("partner")) for p in PHOTOS]


def _partners(password=PW, **extra):
    S3.put_object(Bucket="test-bucket", Key="meta/partners.json", Body=json.dumps([
        {"id": "eagle-mount", "name": "Eagle Mount", "label": "Orange", "slug": "eaglemount",
         "password": password, "welcome": "Welcome!", **extra}]).encode())


TAKEN = {"DSC_0002.JPG": "2026:12:05 11:40:00", "DSC_0003.JPG": "2026:12:05 09:15:00"}   # 0003 was first


@pytest.fixture(autouse=True)
def setup(main_module, live_photos, monkeypatch):
    # Capture times are remembered with a background save; save right away here so no save
    # is left waiting when the next test starts
    monkeypatch.setattr(main_module, "_save_photo_list_soon", main_module._save_photo_list)
    for p, marked in zip(PHOTOS, MARKED):
        p.pop("taken", None)
        if marked:                                         # every test starts from the same marks
            p["partner"] = "eagle-mount"
        else:
            p.pop("partner", None)
        S3.put_object(Bucket="test-bucket", Key=p["path"], Body=_jpeg(taken=TAKEN.get(p["path"].rsplit("/", 1)[-1])))
    S3.delete_object(Bucket="test-bucket", Key="meta/partner_downloads/eagle-mount.json")
    main_module._partner_logs.clear()
    main_module._pin_fail_limiter._calls.clear()
    _partners()
    main_module._partners_doc.cache["data"] = None
    live_photos(PHOTOS)
    yield
    S3.delete_object(Bucket="test-bucket", Key="meta/partners.json")
    main_module._partners_doc.cache["data"] = None


@pytest.fixture()
def pos(main_module):
    """A POS iPad that has entered a photographer PIN."""
    S3.put_object(Bucket="test-bucket", Key="meta/photographers.json",
                  Body=json.dumps([{"id": "p1", "name": "Test Photog", "pin": "4821"}]).encode())
    c = TestClient(main_module.app)
    assert c.post("/api/photographer/auth", json={"pin": "4821"}).status_code == 200
    return c


@pytest.fixture()
def member(main_module):
    c = TestClient(main_module.app)
    r = c.post("/eaglemount", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/eaglemount"
    return c


def _names(photos):
    return sorted(p["path"].rsplit("/", 1)[-1] for p in photos)


# ── Hidden from the public, still on the POS ──────────────────────────────────

def test_public_gallery_doesnt_show_eagle_mount_photos(client):
    browse = client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert _names(browse["photos"]) == ["DSC_0001.JPG"]
    days = [d["date"] for d in client.get("/api/days").json()["days"]]
    assert DAY1 in days and DAY2 not in days and "2026-12-07" not in days
    assert "2026-12-07" not in client.get("/api/dates").json()["dates"]
    assert client.get("/api/locations", params={"date": DAY2}).json()["locations"] == []
    assert client.get("/api/folder-dates", params={"location": "Big Sky Ski School"}).json()["dates"] == [DAY1]


def test_the_pos_still_sees_them(pos, admin_client, main_module):
    browse = pos.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert _names(browse["photos"]) == ["DSC_0001.JPG", "DSC_0002.JPG", "DSC_0003.JPG"]
    pos_days = [d["date"] for d in admin_client.get("/api/pos-dates").json()["dates"]]
    assert DAY2 in pos_days and "2026-12-07" in pos_days


def test_they_cant_be_bought_online(main_module):
    assert PHOTOS[1]["path"] not in main_module._folder_index()["by_path"]
    assert PHOTOS[1]["path"] in main_module._folder_index()["all_by_path"]


# ── The password page ─────────────────────────────────────────────────────────

def test_page_needs_the_password(client, main_module):
    page = client.get("/eaglemount")
    assert page.status_code == 200 and 'type="password"' in page.text and "noindex" in page.text
    assert 'action="/eaglemount"' in page.text and 'class="custom-header"' in page.text     # the site header
    assert client.get("/api/partner/eaglemount/photos").status_code == 401
    bad = client.post("/eaglemount", data={"password": "nope"})
    assert bad.status_code == 401 and "isn't right" in bad.text
    assert client.get("/EagleMount").status_code == 200                      # any capitalization
    assert client.get("/partner/eaglemount").status_code == 404              # the old address is gone


def test_short_address_leaves_every_other_address_alone(client, main_module):
    # anything that isn't a partner keeps the normal not-found answer
    for path in ("/nothing-here", "/x.php", "/partner/nothing-here"):
        assert client.get(path).status_code == 404
    assert client.get("/nothing-here").json() == {"detail": "Not Found"}
    assert client.post("/nothing-here", data={"password": "x"}).status_code == 404
    # real pages registered before the partner page still answer at their own address
    assert client.get("/privacy").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_page_is_closed_until_a_password_is_set(client, main_module):
    _partners(password="")
    main_module._partners_doc.cache["data"] = None
    assert client.get("/eaglemount").status_code == 404
    assert client.post("/eaglemount", data={"password": ""}).status_code == 404


def test_signed_in_member_sees_their_photos_by_day(member):
    page = member.get("/eaglemount")
    assert page.status_code == 200 and "Welcome!" in page.text and "Select a range" in page.text and "Order prints" in page.text
    d = member.get("/api/partner/eaglemount/photos").json()
    assert [day["date"] for day in d["days"]] == ["2026-12-07", DAY2, DAY1]          # newest first
    assert [p["filename"] for p in d["days"][2]["photos"]] == ["DSC_0003.JPG", "DSC_0002.JPG"]   # by time taken


def test_changing_the_password_signs_everyone_out(member, main_module):
    _partners(password="new-season")
    main_module._partners_doc.cache["data"] = None
    assert member.get("/api/partner/eaglemount/photos").status_code == 401


def test_too_many_wrong_passwords_lock_out(client, main_module):
    for _ in range(main_module._pin_fail_limiter.max_calls):
        client.post("/eaglemount", data={"password": "nope"})
    assert client.post("/eaglemount", data={"password": PW}).status_code == 429


# ── Downloads and the donation log ────────────────────────────────────────────

def _log():
    return json.loads(S3.get_object(Bucket="test-bucket", Key="meta/partner_downloads/eagle-mount.json")["Body"].read())


def test_single_download_is_the_original_and_is_logged(member):
    r = member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    assert r.status_code == 307 and "DSC_0002.JPG" in r.headers["location"]
    member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    rec = _log()["photos"][PHOTOS[1]["path"]]
    assert rec["count"] == 2 and rec["photo_date"] == DAY1          # counted once for the report, twice downloaded


def test_only_their_own_photos_can_be_downloaded(member, client):
    assert member.get("/partner/eaglemount/file", params={"path": PHOTOS[0]["path"]}).status_code == 404
    assert client.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}).status_code == 404
    assert member.get("/partner/eaglemount/thumb", params={"path": PHOTOS[0]["path"]}).status_code == 404


def test_download_a_whole_day_as_a_zip(member):
    r = member.post("/partner/eaglemount/zip", data={"date": DAY1})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    import zipfile
    names = zipfile.ZipFile(BytesIO(r.content)).namelist()
    assert names == ["DSC_0003.JPG", "DSC_0002.JPG"]
    assert set(_log()["photos"]) == {PHOTOS[1]["path"], PHOTOS[2]["path"]}


def test_download_a_selection_skips_anything_not_theirs(member):
    import zipfile
    wanted = [PHOTOS[3]["path"], PHOTOS[0]["path"], "images/../meta/pricing.json"]
    r = member.post("/partner/eaglemount/zip", data={"paths": json.dumps(wanted)})
    assert zipfile.ZipFile(BytesIO(r.content)).namelist() == ["DSC_0010.JPG"]


def test_member_thumbnails_and_enlarged_view_are_unwatermarked(member, main_module, monkeypatch):
    # Which stored versions the page asks for (watching the watermark itself picked up thumbnail
    # work other tests left running in the background)
    asked = []
    real_make, real_demand = main_module._make_thumbs, main_module._thumb_on_demand
    monkeypatch.setattr(main_module, "_make_thumbs", lambda key, variants, *a: asked.append(tuple(variants)) or real_make(key, variants, *a))
    monkeypatch.setattr(main_module, "_thumb_on_demand", lambda key, v: asked.append(v) or real_demand(key, v))
    assert member.get("/partner/eaglemount/thumb", params={"path": PHOTOS[1]["path"]}).status_code == 200
    v = member.get("/partner/eaglemount/view", params={"path": PHOTOS[1]["path"]})
    assert v.status_code == 200 and v.headers["content-type"] == "image/jpeg"
    assert "pos" in asked and ("large",) in asked
    assert not main_module._THUMB_VARIANTS["pos"][3] and not main_module._THUMB_VARIANTS["large"][3]   # no watermark
    # the large version was stored, so the next view goes straight to it
    again = member.get("/partner/eaglemount/view", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    assert again.status_code == 302 and "nowm-large" in again.headers["location"]


def test_capture_times_are_read_once_and_remembered(member, main_module, monkeypatch):
    member.get("/api/partner/eaglemount/photos")
    assert PHOTOS[2]["taken"] == "2026:12:05 09:15:00" and PHOTOS[3]["taken"] == ""   # no EXIF time -> ""
    monkeypatch.setattr(main_module, "_photo_taken", lambda path: pytest.fail("read again"))
    member.get("/api/partner/eaglemount/photos")


def test_save_to_photos_links_are_logged_and_only_theirs(member, client):
    r = member.post("/partner/eaglemount/links", json={"paths": [PHOTOS[1]["path"], PHOTOS[0]["path"]]})
    assert r.status_code == 200 and [f["name"] for f in r.json()["files"]] == ["DSC_0002.JPG"]
    assert _log()["photos"][PHOTOS[1]["path"]]["how"] == "save"
    assert client.post("/partner/eaglemount/links", json={"paths": [PHOTOS[1]["path"]]}).status_code == 401


def test_owner_edits_partner_settings(admin_client, client, member, main_module):
    d = admin_client.get("/api/admin/partner-settings").json()
    p = d["partners"][0]
    p.update(welcome="Hi Eagle Mount!", family_code="eaglemount 25", family_code_pct=25,
             family_code_message="Families: {code} saves {pct}%")
    r = admin_client.post("/api/admin/partner-settings", json={"partners": [p]})
    assert r.status_code == 200 and r.json()["partners"][0]["family_code"] == "EAGLEMOUNT25"
    main_module._partners_doc.cache["data"] = None
    assert "Hi Eagle Mount!" in member.get("/eaglemount").text
    assert member.get("/api/partner/eaglemount/photos").json()["family_code_message"] == "Families: EAGLEMOUNT25 saves 25%"
    assert client.get("/api/admin/partner-settings").status_code == 401


@pytest.mark.parametrize("change,msg", [
    ({"password": "abc"}, "at least 6"), ({"name": " "}, "needs a name"), ({"label": ""}, "color label"),
    ({"print_discount_pct": 150}, "print discount"), ({"value_per_photo": "x"}, "must be a number"),
])
def test_bad_partner_settings_are_refused(admin_client, change, msg):
    p = admin_client.get("/api/admin/partner-settings").json()["partners"][0]
    r = admin_client.post("/api/admin/partner-settings", json={"partners": [{**p, **change}]})
    assert r.status_code == 400 and msg in r.json()["error"]


def test_standard_family_code_wording(main_module):
    msg = main_module._family_code_message({"family_code": "EAGLEMOUNT25", "family_code_pct": 25})
    assert msg == ("Family and friends: use code EAGLEMOUNT25 for 25% off photos and prints, "
                   "online in {gallery} and in our store.")   # {gallery}: a link on the page
    assert main_module._family_code_message({"family_code": ""}) == ""


def test_pos_browse_marks_eagle_mount_photos(pos, client):
    seen = pos.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()["photos"]
    assert {p["filename"]: p.get("partner") for p in seen} == {
        "DSC_0001.JPG": None, "DSC_0002.JPG": "eagle-mount", "DSC_0003.JPG": "eagle-mount"}
    public = client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()["photos"]
    assert all("partner" not in p for p in public)


# ── Marking photos on the Indexing page ───────────────────────────────────────

def test_indexing_page_marks_and_unmarks_eagle_mount(admin_client, client, main_module):
    path = PHOTOS[0]["path"]                                   # a regular photo
    r = admin_client.post("/api/admin/set-photos-partner", json={"paths": [path], "partner": "eagle-mount"})
    assert r.status_code == 200 and r.json()["paths"] == [path]
    browse = lambda: _names(client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()["photos"])
    assert browse() == []                                      # gone from the public gallery
    shown = admin_client.get("/api/admin/folder-photos", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert {p["file"]: p["partner"] for p in shown["photos"]}["DSC_0001.JPG"] == "eagle-mount"
    r = admin_client.post("/api/admin/set-photos-partner", json={"paths": [path, PHOTOS[1]["path"]], "partner": ""})
    assert r.json()["updated"] == 2
    assert browse() == ["DSC_0001.JPG", "DSC_0002.JPG"]        # back in the gallery


def test_marking_needs_login_a_real_partner_and_skips_portraits(admin_client, client, live_photos, main_module):
    assert client.post("/api/admin/set-photos-partner", json={"paths": [], "partner": "eagle-mount"}).status_code == 401
    assert admin_client.post("/api/admin/set-photos-partner", json={"paths": [], "partner": "nope"}).status_code == 400
    portrait = {"path": "images/2026-12-05/lone-peak-portraits/smith/P1.jpg", "date": DAY1,
                "location": "Lone Peak Portraits", "last_name": "Smith", "group": ""}
    live_photos(PHOTOS + [portrait])
    r = admin_client.post("/api/admin/set-photos-partner", json={"paths": [portrait["path"]], "partner": "eagle-mount"})
    assert r.json() == {"updated": 0, "skipped_portraits": 1, "paths": []} and "partner" not in portrait


def test_marking_answers_at_once_and_saves_in_the_background(admin_client, client, member, main_module, monkeypatch):
    # The whole photo list isn't saved while the studio waits: the save is queued, and the
    # gallery and the partner page change right away
    queued = []
    monkeypatch.setattr(main_module, "_save_photo_list_soon", lambda: queued.append(1))
    monkeypatch.setattr(main_module, "_save_photo_list", lambda: pytest.fail("saved while the studio waited"))
    monkeypatch.setattr(main_module, "_prepare_partner_photos", lambda items: None)
    r = admin_client.post("/api/admin/set-photos-partner", json={"paths": [PHOTOS[0]["path"]], "partner": "eagle-mount"})
    assert r.status_code == 200 and r.json()["updated"] == 1 and queued == [1]
    assert client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()["photos"] == []
    day = next(d for d in member.get("/api/partner/eaglemount/photos").json()["days"] if d["date"] == DAY1)
    assert "DSC_0001.JPG" in [p["filename"] for p in day["photos"]]


def test_marked_photos_get_their_large_version_made_ahead(main_module):
    main_module._prepare_partner_photos([PHOTOS[2]])
    for _ in range(50):                                        # made in the background
        if main_module._stored_thumb_link(to_r2_key(PHOTOS[2]["path"]), "large"):
            break
        time.sleep(0.05)
    assert main_module._stored_thumb_link(to_r2_key(PHOTOS[2]["path"]), "large")
    assert PHOTOS[2]["taken"] == "2026:12:05 09:15:00"


def test_studio_partner_folder_lists_every_marked_photo_by_day(admin_client, client, main_module):
    assert client.get("/api/admin/partner-photos", params={"partner": "eagle-mount"}).status_code == 401
    assert admin_client.get("/api/admin/partner-photos", params={"partner": "nope"}).status_code == 404
    PHOTOS[3]["draft"] = True                                  # drafts are in the studio's folder too
    try:
        d = admin_client.get("/api/admin/partner-photos", params={"partner": "eagle-mount"}).json()
    finally:
        PHOTOS[3].pop("draft")
    assert d["total"] == 4 and [x["date"] for x in d["days"]] == ["2026-12-07", DAY2, DAY1]
    assert [p["filename"] for p in d["days"][1]["photos"]] == ["DSC_0010.JPG"] and d["days"][1]["photos"][0]["is_draft"]
    assert admin_client.get("/api/admin/partners").json()["partners"][0]["count"] == 4
    folder = admin_client.get("/api/admin/folder/photos", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert {p["filename"]: p["partner"] for p in folder["photos"]} == {"DSC_0001.JPG": "", "DSC_0002.JPG": "eagle-mount",
                                                                       "DSC_0003.JPG": "eagle-mount"}


def test_site_header_links_to_the_partner_page(client):
    assert 'href="/eaglemount"' in client.get("/").text
    assert 'href="/eaglemount"' in client.get("/eaglemount").text


# ── Prints at the partner discount ────────────────────────────────────────────

def _quote(c, path, size_idx=1):
    return c.post("/api/checkout/quote", json={"prints": [{"path": path, "size_idx": size_idx, "filename": "x.jpg",
                                                          "size": "8x10"}]}).json()


def test_signed_in_members_get_40_percent_off_prints_at_checkout(member, client, main_module):
    full = float(main_module._load_print_pricing().get("sizes", main_module._DEFAULT_PRINT_SIZES)[1]["price"])
    assert _quote(member, PHOTOS[1]["path"])["subtotal"] == round(full * 0.6, 2)
    assert _quote(client, PHOTOS[1]["path"])["subtotal"] == full              # not signed in: full price
    assert _quote(member, PHOTOS[0]["path"])["subtotal"] == full              # not an Eagle Mount photo


def test_partner_print_discount_is_in_the_order(member, main_module):
    priced = main_module._price_order({"prints": [{"path": PHOTOS[1]["path"], "size_idx": 0, "filename": "a.jpg"}]},
                                      {PHOTOS[1]["path"]: (40, "Eagle Mount")})
    assert {"key": "Price", "value": "40% off (Eagle Mount photo)"} in priced["line_items"][0]["meta_data"]


def test_pos_browse_carries_the_print_discount(pos):
    seen = pos.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()["photos"]
    em = next(p for p in seen if p["filename"] == "DSC_0002.JPG")
    assert em["partner_name"] == "Eagle Mount" and em["partner_print_pct"] == 40


# ── The family code ───────────────────────────────────────────────────────────

def _codes():
    try:
        return json.loads(S3.get_object(Bucket="test-bucket", Key="meta/discount_codes.json")["Body"].read())
    except Exception:
        return []


@pytest.fixture()
def no_codes():
    S3.delete_object(Bucket="test-bucket", Key="meta/discount_codes.json")
    yield
    S3.delete_object(Bucket="test-bucket", Key="meta/discount_codes.json")


def _save_code(admin_client, main_module, code, pct=25):
    p = admin_client.get("/api/admin/partner-settings").json()["partners"][0]
    r = admin_client.post("/api/admin/partner-settings", json={"partners": [{**p, "family_code": code, "family_code_pct": pct}]})
    main_module._partners_doc.cache["data"] = None
    return r


def test_family_code_is_added_to_discounts_and_works(admin_client, client, main_module, no_codes):
    r = _save_code(admin_client, main_module, "EAGLEMOUNT25")
    assert "added to Discounts" in r.json()["notes"][0]
    [c] = _codes()
    assert (c["code"], c["type"], c["amount"], c["applies_to"], c["active"], c["partner_id"]) == \
        ("EAGLEMOUNT25", "percent", 25, "all", True, "eagle-mount")
    v = client.get("/api/validate-coupon", params={"code": "eaglemount25"}).json()
    assert v["valid"] and v["amount"] == 25                                   # online and POS both use this


def test_changing_the_percent_or_the_code(admin_client, main_module, no_codes):
    _save_code(admin_client, main_module, "EAGLEMOUNT25")
    _save_code(admin_client, main_module, "EAGLEMOUNT25", pct=30)
    assert [c["amount"] for c in _codes()] == [30]
    _save_code(admin_client, main_module, "EMFAMILY", pct=30)
    by_code = {c["code"]: c["active"] for c in _codes()}
    assert by_code == {"EAGLEMOUNT25": False, "EMFAMILY": True}               # the old code is turned off


def test_a_hand_made_code_with_the_same_name_is_left_alone(admin_client, main_module, no_codes):
    S3.put_object(Bucket="test-bucket", Key="meta/discount_codes.json", Body=json.dumps([
        {"id": "x1", "code": "EAGLEMOUNT25", "type": "fixed", "amount": 5, "active": True}]).encode())
    r = _save_code(admin_client, main_module, "EAGLEMOUNT25")
    assert "already exists" in r.json()["notes"][0]
    assert _codes() == [{"id": "x1", "code": "EAGLEMOUNT25", "type": "fixed", "amount": 5, "active": True}]


def test_discounts_arent_wiped_if_they_cant_be_read(admin_client, main_module, monkeypatch, no_codes):
    S3.put_object(Bucket="test-bucket", Key="meta/discount_codes.json", Body=json.dumps([{"id": "k", "code": "KEEP"}]).encode())
    real = main_module.s3.get_object

    def flaky(**kw):
        if kw.get("Key") == "meta/discount_codes.json":
            raise RuntimeError("storage hiccup")
        return real(**kw)
    monkeypatch.setattr(main_module.s3, "get_object", flaky)
    r = _save_code(admin_client, main_module, "EAGLEMOUNT25")
    monkeypatch.setattr(main_module.s3, "get_object", real)
    assert "Couldn't reach Discounts" in r.json()["notes"][0]
    assert _codes() == [{"id": "k", "code": "KEEP"}]


# ── The donation report ───────────────────────────────────────────────────────

def test_donation_report_counts_each_photo_once(member, admin_client, client):
    member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    member.post("/partner/eaglemount/zip", data={"date": DAY2})
    d = admin_client.get("/api/admin/partner-report", params={"partner": "eagle-mount"}).json()
    assert (d["photos"], d["downloads"], d["total_value"]) == (2, 3, 50.0)
    assert client.get("/api/admin/partner-report", params={"partner": "eagle-mount"}).status_code == 401
    csv_text = admin_client.get("/admin/partner-report.csv", params={"partner": "eagle-mount"}).text
    assert "Total donated value,$50.00" in csv_text and "DSC_0010.JPG" in csv_text


def test_donation_report_date_range(member, admin_client):
    member.get("/partner/eaglemount/file", params={"path": PHOTOS[1]["path"]}, follow_redirects=False)
    d = admin_client.get("/api/admin/partner-report", params={"partner": "eagle-mount", "date_from": "2030-01-01"}).json()
    assert d["photos"] == 0
    assert admin_client.get("/api/admin/partner-report", params={"partner": "eagle-mount", "date_from": "bad"}).status_code == 400


def test_studio_login_sees_the_gallery_like_the_public(admin_client):
    browse = admin_client.get("/api/browse", params={"date": DAY1, "location": "Big Sky Ski School"}).json()
    assert _names(browse["photos"]) == ["DSC_0001.JPG"]


def test_partner_prints_never_combine_with_a_code(member, main_module, no_codes, admin_client):
    _save_code(admin_client, main_module, "EAGLEMOUNT25")
    full = float(main_module._load_print_pricing().get("sizes", main_module._DEFAULT_PRINT_SIZES)[1]["price"])
    q = member.post("/api/checkout/quote", json={"coupon_code": "EAGLEMOUNT25", "prints": [
        {"path": PHOTOS[1]["path"], "size_idx": 1, "filename": "a.jpg"}]}).json()
    assert q["subtotal"] == round(full * 0.6, 2) and q["coupon"] is None and "can't be combined" in q["coupon_note"]
    # with a regular print in the same cart, the code takes 25% off only that one
    q = member.post("/api/checkout/quote", json={"coupon_code": "EAGLEMOUNT25", "prints": [
        {"path": PHOTOS[1]["path"], "size_idx": 1, "filename": "a.jpg"},
        {"path": PHOTOS[0]["path"], "size_idx": 1, "filename": "b.jpg"}]}).json()
    assert q["coupon"]["discount"] == round(full * 0.25, 2)
    assert q["total"] == round(full * 0.6 + full - full * 0.25 + q["shipping"], 2)


def test_framed_partner_print_frame_is_full_price_and_takes_the_code(member, main_module, no_codes, admin_client,
                                                                      monkeypatch):
    _save_code(admin_client, main_module, "EAGLEMOUNT25")
    vid = main_module.FRAME_VARIATION_MAP[(1, "landscape")]
    monkeypatch.setattr(main_module, "_frame_variations", lambda: {vid: {"price": 45.0, "stock_status": "instock"}})
    full = float(main_module._load_print_pricing().get("sizes", main_module._DEFAULT_PRINT_SIZES)[1]["price"])
    cart = {"prints": [{"path": PHOTOS[1]["path"], "size_idx": 1, "filename": "a.jpg",
                        "frame": "Rustic Big Sky Picture Frame", "orientation": "landscape"}]}
    q = member.post("/api/checkout/quote", json=cart).json()
    assert q["subtotal"] == round(full * 0.6 + 45.0, 2)                    # 40% off the print, frame full price
    q = member.post("/api/checkout/quote", json={**cart, "coupon_code": "EAGLEMOUNT25"}).json()
    assert q["coupon"]["discount"] == round(45.0 * 0.25, 2)                 # the code only touches the frame
