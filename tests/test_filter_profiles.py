"""The gallery's filters only offer choices that still lead to photos, in any order: the
folder's filter profiles must agree exactly with what /api/search returns."""
import itertools

import pytest

D, L = "2026-12-20", "El Dorado Action"


def _p(n, sport, jackets, pants, instructor=False):
    return {"path": f"images/{D}/el-dorado-action/P{n}.jpg", "date": D, "location": L, "group": "", "last_name": "",
            "sport": sport, "jacket_colors": jackets, "pants_colors": pants, "ski_instructor": instructor}


PHOTOS = [_p(1, "ski", ["red"], ["black"]), _p(2, "ski", ["red", "white"], ["black"]),
          _p(3, "snowboard", ["blue"], ["black"]), _p(4, "snowboard", ["navy"], ["gray"]),   # navy counts as blue
          _p(5, "ski", ["green"], ["green"], instructor=True), _p(6, "unclear", ["red"], ["gray"]),
          _p(7, "ski", ["camo"], ["camo"])]                                  # camo counts as green


@pytest.fixture()
def folder(client, live_photos, main_module):
    live_photos(PHOTOS)
    main_module._search_limiter._calls.clear()
    return client.get("/api/folder-colors", params={"date": D, "location": L}).json()


def _matches(pr, jackets, pants, sport, instructor):
    return ((not sport or pr["s"] == sport) and (not instructor or pr["i"])
            and (not pants or pants in pr["p"]) and all(j in pr["j"] for j in jackets))


def test_profiles_agree_with_search_for_every_combination(client, folder, main_module):
    profiles = folder["profiles"]
    assert sum(pr["n"] for pr in profiles) == len(PHOTOS)
    combos = itertools.product([[], ["red"], ["blue"], ["red", "white"], ["green"]], [None, "black", "gray", "green"],
                               [None, "ski", "snowboard"], [False, True])
    for jackets, pants, sport, instructor in combos:
        params = [("date", D), ("location", L)] + [("jacket_color", j) for j in jackets]
        if pants: params.append(("pants_color", pants))
        if sport: params.append(("sport", sport))
        if instructor: params.append(("ski_instructor", "true"))
        if len(params) == 2:
            continue                                              # search needs at least one filter
        main_module._search_limiter._calls.clear()
        found = client.get("/api/search", params=params).json().get("photos", [])
        expected = sum(pr["n"] for pr in profiles if _matches(pr, jackets, pants, sport, instructor))
        assert len(found) == expected, (jackets, pants, sport, instructor)


def test_snowboard_narrows_the_jacket_colors(folder):
    snow = [pr for pr in folder["profiles"] if pr["s"] == "snowboard"]
    assert sorted({j for pr in snow for j in pr["j"]}) == ["blue"]   # red/white/green only on skiers


def test_each_photo_names_its_profile_for_the_time_filter(folder):
    """The time filter joins on file names: every photo maps to the profile that describes it."""
    files, numbers = folder["photo_files"], folder["photo_profile"]
    assert sorted(files) == sorted(p["path"].rsplit("/", 1)[1] for p in PHOTOS)
    by_file = {p["path"].rsplit("/", 1)[1]: p for p in PHOTOS}
    for fn, no in zip(files, numbers):
        pr, photo = folder["profiles"][no], by_file[fn]
        assert pr["s"] == (photo["sport"] if photo["sport"] in ("ski", "snowboard") else "")
        assert pr["i"] == photo["ski_instructor"]


def test_camo_is_part_of_green(client, folder, main_module):
    assert "camo" not in folder["jacket"] and "camo" not in folder["pants"] and "green" in folder["jacket"]
    main_module._search_limiter._calls.clear()
    found = client.get("/api/search", params={"date": D, "location": L, "jacket_color": "green"}).json()["photos"]
    assert sorted(p["path"].rsplit("/", 1)[-1] for p in found) == ["P5.jpg", "P7.jpg"]
