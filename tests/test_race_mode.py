"""Race mode in the upload form: the start list is read from the timing company's PDF or pasted
text, and a race's photos go into one folder per racer in the chosen location (other action photos
still go straight into the location)."""
import pytest

from core.race import display_name, parse_start_list, title_of

DATE = "2026-12-05"
LOCATION = "Big Sky Ski School"

# The layout of a timing printout's text: two columns of racers side by side on one line
PRINTOUT = """LONE PEAK TEST SERIES SG 1
MEN SUPER-G
CONDENSED START LIST
Saturday 2/ 3/2024 Start Time 14:00
 St # Bib Name Class Points  St # Bib Name Class Points
1  1 Racer, Alpha U18 323.68 4  45 Racer, Delta U14 999.99
2  3 O'Neil-Smith, Bravo Jr U16 316.67 5 *40 Racer, Echo U14 999.99
3  13 Racer, Charlie U16 311.60
Page 1/1"""


def test_a_timing_printout_gives_the_racers_in_start_order():
    racers = parse_start_list(PRINTOUT)
    assert [(r["order"], r["bib"], r["display"], r["cls"]) for r in racers] == [
        (1, "1", "Alpha Racer", "U18"), (2, "3", "Bravo Jr O'Neil-Smith", "U16"), (3, "13", "Charlie Racer", "U16"),
        (4, "45", "Delta Racer", "U14"), (5, "40", "Echo Racer", "U14")]
    assert title_of(PRINTOUT) == "LONE PEAK TEST SERIES SG 1"


@pytest.mark.parametrize("text", [
    "12 Allmacher, Michael\n3 Talla, Russell U16\n7 Ty Carpenter",
    "1, 12, Allmacher, Michael\n2, 3, Talla, Russell\n3, 7, Ty Carpenter",
    "1\t12\tAllmacher, Michael\n2\t3\tTalla, Russell\n3\t7\tTy Carpenter",
])
def test_a_pasted_list_one_racer_a_line(text):
    racers = parse_start_list(text)
    assert [(r["order"], r["bib"], r["display"]) for r in racers] == [
        (1, "12", "Michael Allmacher"), (2, "3", "Russell Talla"), (3, "7", "Ty Carpenter")]


def test_names_read_first_name_first():
    assert display_name("Allmacher,  Michael") == "Michael Allmacher"
    assert display_name("Ty Carpenter") == "Ty Carpenter"


def test_a_runs_start_list_is_saved_from_text_or_a_pdf(admin_client, main_module, monkeypatch):
    main_module._save_races([])
    race = admin_client.post("/api/admin/races", json={"name": "Test Series SG 1", "date": DATE}).json()
    r = admin_client.post(f"/api/admin/races/{race['id']}/runs/1/start-list", content=b"12 Allmacher, Michael\n3 Talla, Russell")
    assert r.status_code == 200 and [x["bib"] for x in r.json()["runs"][0]["racers"]] == ["12", "3"]
    import routes.races
    monkeypatch.setattr(routes.races, "pdf_text", lambda data: PRINTOUT)
    r = admin_client.post(f"/api/admin/races/{race['id']}/runs/1/start-list", content=b"%PDF-1.4 ...")
    assert r.status_code == 200 and len(r.json()["runs"][0]["racers"]) == 5
    r = admin_client.post(f"/api/admin/races/{race['id']}/runs/1/start-list", content=b"nothing useful here")
    assert r.status_code == 400
    main_module._save_races([])


def test_only_the_studio_manages_races(client):
    assert client.post("/api/admin/races", json={"name": "X", "date": DATE}).status_code == 401
    assert client.get("/api/races").status_code == 401


@pytest.fixture(autouse=True)
def fresh_list(live_photos):
    live_photos([])


def test_a_race_keeps_a_folder_per_racer_and_stays_hidden(admin_client, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "_load_pricing", lambda: {"default": {"tiers": [{"count": 1, "price": 25}]}})
    monkeypatch.setattr(main_module, "_load_races", lambda: [{"id": "r1", "name": LOCATION, "date": DATE, "runs": []}])
    folder = "Allmacher Michael Bib 12"
    r = admin_client.post("/api/admin/upload/presign", json={"date": DATE, "location": LOCATION, "folder": folder, "race": True,
                                                             "files": [{"name": "AAE_6600.JPG", "type": "image/jpeg"}]})
    [u] = r.json()["urls"]
    assert u["key"] == f"images/{DATE}/big-sky-ski-school/allmacher-michael-bib-12/AAE_6600.JPG"
    r = admin_client.post("/api/admin/upload/index", json={"date": DATE, "location": LOCATION, "folder": folder,
                                                           "race": True, "keys": [u["key"]]})
    assert r.status_code == 200 and r.json()["indexed"] == 1
    photo = next(i for i in main_module.data if i["path"] == u["key"])
    assert photo["group"] == folder and photo["last_name"] == "" and photo.get("draft")
    # A race that isn't live stays hidden: push-live refuses it
    r = admin_client.post("/api/admin/push-live", json={"date": DATE, "location": LOCATION, "folder": folder})
    assert r.status_code == 400 and "hidden" in r.json()["error"]


def test_other_action_photos_still_ignore_the_folder(admin_client):
    r = admin_client.post("/api/admin/upload/presign", json={"date": DATE, "location": LOCATION, "folder": "Someone",
                                                             "files": [{"name": "DSC_1.JPG", "type": "image/jpeg"}]})
    assert r.json()["urls"][0]["key"] == f"images/{DATE}/big-sky-ski-school/DSC_1.JPG"


def test_a_racer_folder_name_cant_point_elsewhere(admin_client, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "_load_races", lambda: [{"id": "r1", "name": LOCATION, "date": DATE, "runs": []}])
    r = admin_client.post("/api/admin/upload/presign", json={"date": DATE, "location": LOCATION, "folder": "../meta", "race": True,
                                                             "files": [{"name": "A.JPG", "type": "image/jpeg"}]})
    assert r.status_code == 400
