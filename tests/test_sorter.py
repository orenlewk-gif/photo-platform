"""Close-call sorter (routes/sorter.py): which photos it asks about, and how answers are saved."""
import pytest

D, L = "2026-12-22", "El Dorado Action"


def photo(n, jacket, pants, sport="ski", scores=None, **kw):
    return {"path": f"images/{D}/el-dorado-action/C{n}.jpg", "date": D, "location": L, "group": "", "last_name": "",
            "sport": sport, "jacket_colors": jacket, "pants_colors": pants,
            "model_labels": {"model": "model_v6", "sport": sport, "jacket": list(jacket), "pants": list(pants),
                             **({"scores": scores} if scores else {})}, **kw}


TORN = {"jacket": [["red", 0.7], ["orange", 0.45]], "pants": [["black", 0.9], ["gray", 0.25]],
        "sport": {"ski": 0.6, "snowboard": 0.4}}
SURE = {"jacket": [["blue", 0.98], ["teal", 0.05]], "pants": [["black", 0.97]], "sport": {"ski": 0.99}}


def test_a_close_call_is_the_label_and_the_other_guess_above_the_line(main_module):
    from routes.sorter import close_call
    it = photo(1, ["red"], ["black"], scores=TORN)
    assert close_call(it, "jacket") == {"a": "red", "b": "orange", "a_score": 0.7, "b_score": 0.45}
    assert close_call(it, "pants") == {"a": "black", "b": "gray", "a_score": 0.9, "b_score": 0.25}
    assert close_call(it, "sport") == {"a": "ski", "b": "snowboard", "a_score": 0.6, "b_score": 0.4}
    sure = photo(2, ["blue"], ["black"], scores=SURE)
    assert [close_call(sure, p) for p in ("jacket", "pants", "sport")] == [None, None, None]
    assert close_call(photo(3, ["red"], ["black"]), "jacket") is None               # no kept guesses: never asked


def test_a_second_color_already_in_the_label_isnt_a_close_call(main_module):
    from routes.sorter import close_call
    it = photo(1, ["red", "orange"], ["black"], scores=TORN)
    assert close_call(it, "jacket") is None
    teal = photo(2, ["teal"], ["black"], scores={"jacket": [["teal", 0.6], ["blue", 0.5]]})
    assert close_call(teal, "jacket") is None                                     # teal is blue in the guesses


def test_queue_skips_checked_folders_hand_fixed_photos_and_answered_parts(main_module, admin_client, live_photos):
    live_photos([photo(1, ["red"], ["black"], scores=TORN),
                 photo(2, ["red"], ["black"], scores={**TORN, "jacket": [["red", 0.6], ["orange", 0.55]]}),
                 photo(3, ["red"], ["black"], scores=TORN, manually_corrected=True),
                 photo(4, ["red"], ["black"], scores=TORN, sorted={"jacket": {"answer": ["red"], "was": ["red"]}}),
                 photo(5, ["blue"], ["black"], scores=SURE)])
    fm = main_module._load_folder_meta(); fk = main_module._folder_key(D, L, "")
    fm.pop(fk, None); main_module._save_folder_meta(fm)
    d = admin_client.get("/api/admin/close-calls?part=jacket").json()
    assert [p["file"] for p in d["photos"]] == ["C2.jpg", "C1.jpg"]                 # most torn first
    assert d["counts"] == {"jacket": 2, "pants": 3, "sport": 3}
    assert d["photos"][0]["labels"] == {"jacket": ["red"], "pants": ["black"], "sport": "ski"}
    fm[fk] = {"reviewed": True}; main_module._save_folder_meta(fm)
    try:
        assert admin_client.get("/api/admin/close-calls?part=jacket").json()["counts"] == {"jacket": 0, "pants": 0, "sport": 0}
    finally:
        fm.pop(fk, None); main_module._save_folder_meta(fm)


def test_an_answer_becomes_the_label_and_is_kept_and_undo_puts_it_back(main_module, admin_client, live_photos):
    live_photos([photo(1, ["red"], ["black"], scores=TORN)])
    path = main_module.data[0]["path"]
    r = admin_client.post("/api/admin/close-calls/answer", json={"path": path, "part": "jacket", "answer": ["orange"], "how": "b"})
    assert r.status_code == 200 and r.json()["now"] == ["orange"]
    it = main_module.data[0]
    assert it["jacket_colors"] == ["orange"] and it["sorted"]["jacket"]["was"] == ["red"]
    assert it["sorted"]["jacket"]["answer"] == ["orange"] and it["sorted"]["jacket"]["how"] == "b"
    assert it["model_labels"]["jacket"] == ["red"]                                  # the model's own answer is kept
    assert "manually_corrected" not in it                                           # still asked about its pants
    assert admin_client.get("/api/admin/close-calls?part=jacket").json()["counts"]["jacket"] == 0
    # answering again (after a mistake) still remembers the label from before the first answer
    admin_client.post("/api/admin/close-calls/answer", json={"path": path, "part": "jacket", "answer": ["red", "orange"]})
    assert it["sorted"]["jacket"]["was"] == ["red"]
    r = admin_client.post("/api/admin/close-calls/answer", json={"path": path, "part": "jacket", "undo": True})
    assert r.json()["now"] == ["red"] and "sorted" not in it
    assert admin_client.post("/api/admin/close-calls/answer", json={"path": path, "part": "jacket", "undo": True}).status_code == 409
    admin_client.post("/api/admin/close-calls/answer", json={"path": path, "part": "sport", "answer": "snowboard"})
    assert it["sport"] == "snowboard" and it["sorted"]["sport"]["was"] == "ski"


@pytest.mark.parametrize("part,answer", [("jacket", []), ("jacket", ["plaid"]), ("jacket", "red"), ("pants", ["a", "b", "c", "d"]),
                                         ("sport", "unclear"), ("hat", ["red"])])
def test_bad_answers_are_refused(main_module, admin_client, live_photos, part, answer):
    live_photos([photo(1, ["red"], ["black"], scores=TORN)])
    r = admin_client.post("/api/admin/close-calls/answer", json={"path": main_module.data[0]["path"], "part": part, "answer": answer})
    assert r.status_code == 400 and main_module.data[0]["jacket_colors"] == ["red"] and "sorted" not in main_module.data[0]


def test_the_sorter_is_for_the_studio_only(client):
    assert client.get("/api/admin/close-calls").status_code == 401
    assert client.post("/api/admin/close-calls/answer", json={"path": "x", "part": "jacket", "answer": ["red"]}).status_code == 401
    assert client.get("/admin/sorter", follow_redirects=False).status_code in (302, 303, 307)


def test_the_sorter_page_and_its_studio_menu_item(admin_client):
    r = admin_client.get("/admin/sorter")
    assert r.status_code == 200 and "/api/admin/close-calls" in r.text
    studio = admin_client.get("/admin/dashboard/sorter").text
    assert 'id="nav-sorter"' in studio and "/admin/sorter" in studio


def test_once_answered_a_photo_shows_only_under_the_answer(main_module, admin_client, live_photos):
    from core.search import search_colors, search_sports
    live_photos([photo(1, ["red"], ["black"], scores=TORN)])
    it = main_module.data[0]
    assert search_colors(it, "jacket") == ["red", "orange"] and search_sports(it) == {"ski", "snowboard"}
    for part, answer in (("jacket", ["red"]), ("sport", "ski")):
        admin_client.post("/api/admin/close-calls/answer", json={"path": it["path"], "part": part, "answer": answer})
    assert search_colors(it, "jacket") == ["red"] and search_sports(it) == {"ski"}
    assert search_colors(it, "pants") == ["black", "gray"]                        # pants not answered yet
