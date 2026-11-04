"""Forgiving search: a photo also shows for a color (or sport) the labeling model nearly picked,
in its usual place in shot order, and the gallery's filters offer what the search finds."""
import pytest

from core import search

D, L = "2026-12-22", "El Dorado Action"


def _p(n, jacket, pants, sport="ski", scores=None):
    item = {"path": f"images/{D}/el-dorado-action/F{n:02d}.jpg", "date": D, "location": L, "group": "", "last_name": "",
            "jacket_colors": jacket, "pants_colors": pants, "sport": sport}
    if scores is not None:
        item["model_labels"] = {"model": "model_v6", "jacket": jacket, "pants": pants, "sport": sport, "scores": scores}
    return item


PHOTOS = [
    _p(1, ["red"], ["black"]),                                                   # labeled by hand: labels only
    _p(2, ["orange"], ["black"], scores={"jacket": [["orange", 0.7], ["red", 0.45]], "pants": [["black", 0.9]],
                                         "sport": {"ski": 0.97}}),               # nearly red
    _p(3, ["orange"], ["black"], scores={"jacket": [["orange", 0.9], ["red", 0.05]], "pants": [["black", 0.9]],
                                         "sport": {"ski": 0.99}}),               # surely orange
    _p(4, ["red"], ["gray"], scores={"jacket": [["red", 0.9]], "pants": [["gray", 0.5], ["black", 0.42]],
                                     "sport": {"ski": 0.6, "snowboard": 0.4}}),  # pants nearly black, nearly a snowboarder
]


@pytest.fixture()
def photos(live_photos):
    live_photos(PHOTOS)


def _names(r):
    return [p["filename"] for p in r.json()["photos"]]


def test_a_close_call_shows_for_the_color_the_model_nearly_picked(client, photos):
    r = client.get("/api/search", params={"date": D, "location": L, "jacket_color": "red"})
    assert _names(r) == ["F01.jpg", "F02.jpg", "F04.jpg"]          # shot order, the close call in its place
    r = client.get("/api/search", params={"date": D, "location": L, "jacket_color": "orange"})
    assert _names(r) == ["F02.jpg", "F03.jpg"]                      # still under its own label too
    r = client.get("/api/search", params={"date": D, "location": L, "pants_color": "black"})
    assert _names(r) == ["F01.jpg", "F02.jpg", "F03.jpg", "F04.jpg"]
    r = client.get("/api/search", params={"date": D, "location": L, "sport": "snowboard"})
    assert _names(r) == ["F04.jpg"]


def test_the_line_decides_what_counts_as_close(client, photos, monkeypatch):
    monkeypatch.setitem(search.CLOSE_LINE, "jacket", 0.5)
    r = client.get("/api/search", params={"date": D, "location": L, "jacket_color": "red"})
    assert _names(r) == ["F01.jpg", "F04.jpg"]


def test_the_filters_offer_what_the_search_finds(client, photos):
    d = client.get("/api/folder-colors", params={"date": D, "location": L}).json()
    assert d["has_snowboard"] and "black" in d["combos"]["jacket_to_pants"]["red"]
    f2 = d["photo_profile"][d["photo_files"].index("F02.jpg")]
    assert set(d["profiles"][f2]["j"]) == {"orange", "red"}
    f4 = d["photo_profile"][d["photo_files"].index("F04.jpg")]
    assert d["profiles"][f4]["s"] == "ski" and d["profiles"][f4]["s2"] == ["snowboard"]
