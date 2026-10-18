"""Digital photo prices: checkout charges what the gallery shows.

The server prices each album with the same tiers the shop shows for that folder
(_pricing_for: date overrides, category pricing, the location's own pricing) and
the same best-price rule (_best_tier_price == calcPriceFor in the pages)."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")

WINTER = [{"label": "1 Photo", "count": 1, "price": 25},
          {"label": "All Photos", "count": "all", "price": 115, "max": True}]
BIKE = [{"label": "1 Photo", "count": 1, "price": 25}, {"label": "2 Photos", "count": 2, "price": 45},
        {"label": "3 Photos", "count": 3, "price": 60},
        {"label": "Up to 20 Photos", "count": 30, "price": 70, "max": True}]
PORTRAIT = [{"label": "1 Photo", "count": 1, "price": 45}, {"label": "2 Photos", "count": 2, "price": 75},
            {"label": "All Photos", "count": "all", "price": 95}]
DEFAULT = [{"count": 1, "price": 30}, {"count": 3, "price": 65}, {"count": 5, "price": 80},
           {"count": 10, "price": 90, "max": True}, {"count": 30, "price": 100, "max": True}]


@pytest.mark.parametrize("tiers,count,expected", [
    (WINTER, 1, 25), (WINTER, 2, 50), (WINTER, 4, 100), (WINTER, 5, 115),
    (WINTER, 40, 115), (WINTER, 928, 115),                     # All Photos caps any number of photos
    (BIKE, 3, 60), (BIKE, 4, 70), (BIKE, 6, 70), (BIKE, 30, 70),
    (BIKE, 31, 625),                                           # "Up to" tiers stop at their count
    (PORTRAIT, 2, 75), (PORTRAIT, 3, 95), (PORTRAIT, 12, 95),
    (DEFAULT, 2, 60), (DEFAULT, 4, 90), (DEFAULT, 5, 80), (DEFAULT, 6, 90),
    ([], 3, 0),
])
def test_best_price_matches_the_gallery(main_module, tiers, count, expected):
    assert main_module._best_tier_price(count, tiers) == expected


@pytest.fixture()
def live_like_pricing(main_module):
    S3.put_object(Bucket="test-bucket", Key="meta/pricing.json", Body=json.dumps({
        "default": {"tiers": DEFAULT},
        "activity_categories": [{"name": "Mountain Biking", "acts": ["Mountain Biking", "Dirt Days"]},
                                {"name": "Winter Action", "acts": ["El Dorado Action", "Mr K Action"]}],
        "activities": {
            "Mountain Biking": {"tiers": BIKE},
            "Dirt Days": {"tiers": [{"count": 1, "price": 20}, {"count": 2, "price": 30},
                                    {"count": 5, "price": 40, "max": True}]},
            "Winter Action": {"tiers": WINTER},
            "Mr K Action": {"tiers": []},
            "Lone Peak Portraits": {"tiers": PORTRAIT},
            "Big Sky Sign": {"tiers": []},
        },
        "date_overrides": {"2026-08-07": {"Lone Peak Portraits": {"tiers": [
            {"count": 1, "price": 25}, {"count": "all", "price": 30}]}}},
    }).encode())
    main_module._pricing_cache["data"] = None
    yield
    S3.delete_object(Bucket="test-bucket", Key="meta/pricing.json")
    main_module._pricing_cache["data"] = None


@pytest.mark.parametrize("location,date,count,expected", [
    ("El Dorado Action", "2026-06-07", 2, 50),       # category (Winter Action) pricing, not $115
    ("Mr K Action", "2026-06-07", 40, 115),          # empty own tiers -> category pricing, capped
    ("Mountain Biking", "2026-07-15", 4, 70),        # was charged $0
    ("Mountain Biking", "2026-07-15", 7, 70),        # was charged $0
    ("Dirt Days", "2026-07-15", 3, 60),              # its category's prices, as the gallery shows
    ("Lone Peak Portraits", "2026-07-15", 3, 95),
    ("Lone Peak Portraits", "2026-08-07", 3, 30),    # that day's override
])
def test_album_price_is_what_the_gallery_shows(main_module, live_like_pricing, location, date, count, expected):
    shown = main_module._best_tier_price(count, main_module._pricing_for(location, date, "Smith")["tiers"])
    charged = main_module._server_digital_price({"location": location, "count": count, "last_name": "Smith"}, date)
    assert shown == charged == expected


def test_album_with_no_prices_set_up_is_refused(main_module, live_like_pricing):
    with pytest.raises(ValueError, match="aren't set up"):
        main_module._server_digital_price({"location": "Big Sky Sign", "count": 2}, "2026-06-07")


class _WooOrders:
    def __init__(self):
        self.created = []

    def post(self, url, json=None, **kw):
        assert url.endswith("/orders"), url
        self.created.append(json)

        class R:
            status_code, ok, text = 201, True, "{}"
            def json(self_inner):
                return {"id": 77, "order_key": "k"}
        return R()


def _checkout(client, albums, date="2026-06-07"):
    return client.post("/api/checkout", json={"digital_albums": albums, "prints": [], "frames": [],
                                              "date": date, "email": "ann@example.com"})


def test_checkout_charges_each_album_by_its_own_folder_and_date(client, main_module, live_like_pricing,
                                                                monkeypatch):
    woo = _WooOrders()
    monkeypatch.setattr(main_module.http_requests, "post", woo.post)
    r = _checkout(client, [
        {"location": "El Dorado Action", "date": "2026-06-07", "count": 2, "last_name": "El Dorado Action",
         "filenames": ["A.jpg", "B.jpg"], "paths": ["images/a", "images/b"]},
        {"location": "Lone Peak Portraits", "date": "2026-08-07", "count": 3, "last_name": "Smith",
         "filenames": ["C.jpg"], "paths": ["images/c"]},
    ], date="2026-06-07")
    assert r.status_code == 200
    totals = [float(li["total"]) for li in woo.created[0]["line_items"]]
    assert totals == [50.0, 30.0]


def test_checkout_with_unpriced_photos_is_refused_not_free(client, main_module, live_like_pricing, monkeypatch):
    woo = _WooOrders()
    monkeypatch.setattr(main_module.http_requests, "post", woo.post)
    r = _checkout(client, [{"location": "Big Sky Sign", "count": 2, "last_name": "Big Sky Sign"}])
    assert r.status_code == 500 and "aren't set up" in r.json()["error"]
    assert woo.created == []


# Prices the live gallery's calcPriceFor showed for 1..45 photos (captured 2026-10-17 from
# photos.bigskyphotos.com). The server must charge exactly these.
GALLERY_TIER_SETS = {
    "WINTER": WINTER, "BIKE": BIKE, "PORTRAIT": PORTRAIT, "DEFAULT": DEFAULT,
    "DIRT": [{"count": 1, "price": 20}, {"count": 2, "price": 30}, {"count": 5, "price": 40, "max": True}],
    "ZIP": [{"count": 1, "price": 30}, {"count": 2, "price": 60}, {"count": 3, "price": 90},
            {"count": "all", "price": 130}],
    "NOSINGLE": [{"count": 3, "price": 65}, {"count": "all", "price": 90}],
}
GALLERY_PRICES = json.loads(
    '{"WINTER":[25,50,75,100,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115,115],'
    '"BIKE":[25,45,60,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,70,625,650,660,685,710,720,745,770,780,805,830,840,865,890,900],'
    '"PORTRAIT":[45,75,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95],'
    '"DEFAULT":[30,60,65,90,80,90,90,90,90,90,100,100,100,100,100,100,100,100,100,100,100,100,100,100,100,100,100,100,100,100,510,540,570,600,560,590,620,650,680,640,670,700,730,760,720],'
    '"DIRT":[20,30,40,40,40,90,110,120,140,150,170,180,200,210,230,240,260,270,290,300,320,330,350,360,380,390,410,420,440,450,470,480,500,510,530,540,560,570,590,600,620,630,650,660,680],'
    '"ZIP":[30,60,90,120,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130,130],'
    '"NOSINGLE":[25,50,65,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90]}')


@pytest.mark.parametrize("name", sorted(GALLERY_TIER_SETS))
def test_server_formula_matches_the_live_gallery_for_1_to_45_photos(main_module, name):
    charged = [main_module._best_tier_price(n, GALLERY_TIER_SETS[name]) for n in range(1, 46)]
    assert charged == GALLERY_PRICES[name]
