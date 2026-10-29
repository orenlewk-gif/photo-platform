"""The color model's decision rules (tuned 2026-10-18, core/classify.py), checked with a stand-in for the model."""
import numpy as np
import pytest

COLORS = ["red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "black", "gray",
          "brown", "tan", "teal", "camo", "none", "unclear"]
SPORTS = ["ski", "snowboard", "unclear"]


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


class _FakeModel:
    def __init__(self, jacket: dict, pants: dict, sport="ski"):
        self.j = np.full(16, -8.0); self.p = np.full(16, -8.0); self.s = np.zeros(3)
        for c, pr in jacket.items():
            self.j[COLORS.index(c)] = _logit(pr)
        for c, pr in pants.items():
            self.p[COLORS.index(c)] = _logit(pr)
        self.s[SPORTS.index(sport)] = 5

    def run(self, names, feeds):
        assert feeds["pixel_values"].shape == (1, 3, 224, 224)
        return [self.j[None], self.p[None], self.s[None]]


@pytest.fixture()
def model(main_module, monkeypatch):
    def use(**kw):
        model = main_module._color_model
        monkeypatch.setattr(model, "session", _FakeModel(**kw))
        monkeypatch.setattr(model, "meta", {"all_colors": COLORS, "sports": SPORTS})
        monkeypatch.setattr(model, "load", lambda: True)
        from io import BytesIO
        from PIL import Image
        buf = BytesIO(); Image.new("RGB", (1200, 800), (40, 90, 200)).save(buf, "JPEG")
        return main_module.classify_photo_colors(buf.getvalue())
    return use


def test_unsure_model_still_gives_its_best_color(model):
    out = model(jacket={"green": 0.30, "blue": 0.10}, pants={"pink": 0.20})
    assert out["jacket_colors"] == ["green"] and out["pants_colors"] == ["pink"]   # used to be "none"


def test_second_jacket_color_only_when_sure_and_close(model):
    assert model(jacket={"pink": 0.80, "white": 0.70}, pants={"black": .9})["jacket_colors"] == ["pink", "white"]
    assert model(jacket={"pink": 0.90, "white": 0.60}, pants={"black": .9})["jacket_colors"] == ["pink"]   # too far apart
    assert model(jacket={"pink": 0.55, "white": 0.45}, pants={"black": .9})["jacket_colors"] == ["pink"]   # second too unsure


def test_pants_get_one_color_and_teal_reads_as_blue(model):
    out = model(jacket={"teal": 0.9}, pants={"black": 0.8, "gray": 0.75}, sport="snowboard")
    assert out == {"sport": "snowboard", "jacket_colors": ["blue"], "pants_colors": ["black"]}


def test_labels_are_stamped_with_the_model_version(main_module):
    assert main_module.MODEL_LABEL == "model_v5a"


def test_camo_reads_as_green(model):
    out = model(jacket={"camo": 0.8, "green": 0.75}, pants={"camo": 0.9})
    assert out["jacket_colors"] == ["green"] and out["pants_colors"] == ["green"]      # one green, not two


def test_studio_shows_and_saves_camo_as_green(admin_client, live_photos, main_module):
    path = "images/2026-12-20/el-dorado-action/C1.jpg"
    live_photos([{"path": path, "date": "2026-12-20", "location": "El Dorado Action", "group": "", "last_name": "",
                  "jacket_colors": ["camo", "black"], "pants_colors": ["camo"]}])
    photos = admin_client.get("/api/admin/folder-photos", params={"date": "2026-12-20", "location": "El Dorado Action"}).json()["photos"]
    assert photos[0]["jacket"] == ["green", "black"] and photos[0]["pants"] == ["green"]
    admin_client.post("/api/admin/set-photo-colors", json={"path": path, "jacket_colors": ["camo"], "pants_colors": ["camo", "green"]})
    item = next(i for i in main_module.data if i["path"] == path)
    assert item["jacket_colors"] == ["green"] and item["pants_colors"] == ["green"]
