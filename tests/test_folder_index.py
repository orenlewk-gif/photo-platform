"""The day/location/folder lists come from an index that refreshes whenever the photo list is saved."""
import json

import pytest


def _photo(date, loc, name, **kw):
    slug = loc.lower().replace(" ", "-")
    return {"path": f"images/{date}/{slug}/{name}.jpg", "date": date, "location": loc,
            "last_name": "", "group": "", **kw}


@pytest.fixture()
def photos(main_module):
    saved = main_module.data
    main_module.data = [_photo("2026-11-27", "El Dorado Action", f"A{i}", jacket_colors=["blue"]) for i in range(3)]
    main_module._index_state["version"] += 1
    yield main_module.data
    main_module.data = saved
    main_module._index_state["version"] += 1


def _save_photo_list(main_module):
    """Save images.json the way the site does (through its own storage connection)."""
    main_module.s3.put_object(Bucket=main_module.R2_BUCKET, Key="images.json", Body=json.dumps(main_module.data).encode())


def _days(client):
    return {d["date"]: d for d in client.get("/api/days").json()["days"]}


def test_lists_are_built_once_not_per_request(client, photos, main_module, monkeypatch):
    builds = []
    real = main_module._build_folder_index
    monkeypatch.setattr(main_module, "_build_folder_index", lambda items: builds.append(1) or real(items))
    for _ in range(5):
        client.get("/api/days"); client.get("/api/locations", params={"date": "2026-11-27"})
        client.get("/api/browse", params={"date": "2026-11-27", "location": "El Dorado Action", "dims": 0})
    assert len(builds) <= 1


def test_saving_the_photo_list_refreshes_the_lists(client, photos, main_module):
    assert "2026-11-28" not in _days(client)
    photos.append(_photo("2026-11-28", "Ambush Action", "B1"))
    assert "2026-11-28" not in _days(client)                 # not saved yet
    _save_photo_list(main_module)
    assert _days(client)["2026-11-28"]["folder_count"] == 1
    r = client.get("/api/browse", params={"date": "2026-11-28", "location": "Ambush Action", "dims": 0}).json()
    assert r["count"] == 1


def test_drafts_appear_only_once_published(client, photos, main_module):
    photos.append(_photo("2026-11-29", "Ambush Action", "D1", draft=True))
    _save_photo_list(main_module)
    assert "2026-11-29" not in _days(client)
    del photos[-1]["draft"]
    _save_photo_list(main_module)
    assert "2026-11-29" in _days(client)


def test_replacing_the_photo_list_is_picked_up(client, photos, main_module):
    main_module.data = [_photo("2026-12-01", "Mr K Action", "R1")]
    assert list(_days(client)) == ["2026-12-01"]


def test_label_edits_show_in_colour_search_right_away(client, photos):
    q = [("date", "2026-11-27"), ("location", "El Dorado Action"), ("jacket_color", "red")]
    assert client.get("/api/search", params=q).json()["count"] == 0
    photos[0]["jacket_colors"] = ["red"]                     # a correction, no save needed to be searchable
    assert client.get("/api/search", params=q).json()["count"] == 1


def test_unsaved_changes_still_show_up_after_the_safety_interval(client, photos, main_module, monkeypatch):
    _days(client)                                            # index built
    photos.append(_photo("2026-11-30", "Ambush Action", "U1"))
    assert "2026-11-30" not in _days(client)
    monkeypatch.setattr(main_module, "_INDEX_MAX_AGE", 0)
    assert "2026-11-30" in _days(client)


def test_name_lookup_uses_the_index(client, photos, main_module):
    photos.append(_photo("2026-11-27", "Lone Peak Portraits", "S1", last_name="Smith"))
    _save_photo_list(main_module)
    main_module._lastname_limiter._calls.clear()
    r = client.get("/api/last-name-search", params={"q": "smi"}).json()["results"]
    assert [x["last_name"] for x in r] == ["Smith"] and r[0]["previews"] == [photos[-1]["path"]]
