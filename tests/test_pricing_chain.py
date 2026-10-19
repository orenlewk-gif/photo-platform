"""Pins down how the shop picks a folder's prices (_pricing_for), rule by rule, before any
of this code is moved. Made-up settings; each test names the rule it covers."""
import json

import boto3
import pytest

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"

CAT = [{"count": 1, "price": 25}, {"count": "all", "price": 115, "max": True}]
OWN = [{"count": 1, "price": 30}, {"count": 3, "price": 70}]


def _put(key, payload):
    S3.put_object(Bucket=B, Key=key, Body=json.dumps(payload).encode())


@pytest.fixture()
def settings(main_module):
    """Write pricing settings into the fake bucket and clear the caches; cleaned up after."""
    keys = []

    def use(pricing=None, folder_meta=None, zip_pricing=None, items_bundles=None):
        for key, payload in (("meta/pricing.json", pricing), ("meta/folder_meta.json", folder_meta),
                             ("meta/zip_pricing.json", zip_pricing), ("meta/items_bundles.json", items_bundles)):
            if payload is not None:
                _put(key, payload); keys.append(key)
        main_module._pricing_cache["data"] = None
        main_module._folder_meta_cache["data"] = None
    yield use
    for key in keys:
        S3.delete_object(Bucket=B, Key=key)
    main_module._pricing_cache["data"] = None
    main_module._folder_meta_cache["data"] = None


def P(main_module, *args):
    return main_module._pricing_for(*args)


# ── Zip line group-size pricing ──────────────────────────────────────────────

ZIP = {"tiers": [{"people": 2, "one_photo": 20, "two_photos": 35, "all_photos": 60},
                 {"people": 4, "one_photo": 25, "three_photos": 55, "all_photos": 80}]}


def test_zip_folder_priced_by_its_group_size(main_module, settings):
    settings(pricing={"activities": {}}, zip_pricing=ZIP,
             folder_meta={"2026-07-01|Nature Zip Line|Smith": {"group_size": 4}})
    r = P(main_module, "Nature Zip Line", "2026-07-01", "Smith")
    assert r == {"tiers": [{"label": "1 Photo", "count": 1, "price": 25},
                           {"label": "3 Photos", "count": 3, "price": 55},
                           {"label": "All Photos", "count": "all", "price": 80}],
                 "combos": [], "zip_group_size": 4}


def test_zip_folder_uploaded_under_old_location_name_still_found(main_module, settings):
    settings(pricing={"activities": {}}, zip_pricing=ZIP,
             folder_meta={"2026-07-01|Nature Zip Line|Smith": {"group_size": 2}})
    assert P(main_module, "Nature Zip", "2026-07-01", "Smith")["zip_group_size"] == 2


def test_zip_folder_without_group_size_falls_through_to_activity_pricing(main_module, settings):
    settings(pricing={"activities": {"Nature Zip Line": {"tiers": OWN}}}, zip_pricing=ZIP, folder_meta={})
    r = P(main_module, "Nature Zip Line", "2026-07-01", "Smith")
    assert r["tiers"] == OWN and "zip_group_size" not in r


def test_zip_group_size_with_no_matching_tier_falls_through(main_module, settings):
    settings(pricing={"activities": {"Nature Zip Line": {"tiers": OWN}}}, zip_pricing=ZIP,
             folder_meta={"2026-07-01|Nature Zip Line|Smith": {"group_size": 9}})
    assert P(main_module, "Nature Zip Line", "2026-07-01", "Smith")["tiers"] == OWN


# ── Date overrides, categories, a location's own pricing ─────────────────────

BASE = {
    "combos": [{"name": "C1"}],
    "activity_categories": [{"name": "Winter Action", "acts": ["El Dorado Action", "Mr K Action"]}],
    "activities": {
        "Winter Action": {"tiers": CAT, "flags": {"clip": True}},
        "El Dorado Action": {"tiers": OWN, "flags": {"time_search": True, "clip": False}},
        "Lone Peak Portraits": {"tiers": OWN},
        "Legacy Spot": {"per_photo": 20, "all_photos": 50},
        "Legacy No All": {"per_photo": 20},
        "Empty Action": {"tiers": [], "flags": {"time_search": True}},
        "Empty Plain": {"tiers": []},
    },
    "date_overrides": {"2026-12-24": {"el dorado action": {"tiers": [{"count": 1, "price": 5}]}}},
}


def test_date_override_wins_and_matches_location_in_any_case(main_module, settings):
    settings(pricing=BASE)
    r = P(main_module, "El Dorado Action", "2026-12-24")
    assert r["tiers"] == [{"count": 1, "price": 5}] and r["combos"] == [{"name": "C1"}]
    assert r["activity_groups"] == {"Winter Action": ["El Dorado Action", "Mr K Action"]}


def test_category_pricing_wins_over_the_locations_own(main_module, settings):
    settings(pricing=BASE)
    r = P(main_module, "El Dorado Action", "2026-12-01")
    assert r["tiers"] == CAT
    # location flags merged in, the category's flags win where both are set
    assert r["flags"] == {"time_search": True, "clip": True}


def test_location_in_a_category_matches_in_any_case(main_module, settings):
    settings(pricing=BASE)
    assert P(main_module, "mr k action", "2026-12-01")["tiers"] == CAT


def test_explicit_activity_groups_override_the_categories(main_module, settings):
    settings(pricing={**BASE, "activity_groups": {"Winter Action": ["Mr K Action"]}})
    assert P(main_module, "El Dorado Action", "2026-12-01")["tiers"] == OWN     # no longer in the group
    assert P(main_module, "Mr K Action", "2026-12-01")["tiers"] == CAT


def test_location_without_a_category_uses_its_own_pricing(main_module, settings):
    settings(pricing=BASE)
    r = P(main_module, "Lone Peak Portraits", "2026-12-01", "Smith")
    assert r["tiers"] == OWN and r["combos"] == [{"name": "C1"}]


def test_old_per_photo_settings_become_tiers(main_module, settings):
    settings(pricing=BASE)
    assert P(main_module, "Legacy Spot")["tiers"] == [{"label": "1 Photo", "count": 1, "price": 20.0},
                                                      {"label": "All Photos", "count": 999, "price": 50.0, "max": True}]
    assert P(main_module, "Legacy No All")["tiers"] == [{"label": "1 Photo", "count": 1, "price": 20.0}]


def test_action_gallery_with_empty_tiers_gets_the_default_action_prices(main_module, settings):
    settings(pricing=BASE)
    assert P(main_module, "Empty Action")["tiers"] == [{"label": "1 Photo", "count": 1, "price": 25},
                                                       {"label": "All Photos", "count": 30, "price": 115, "max": True}]
    assert P(main_module, "Empty Plain")["tiers"] == []


# ── Item-list fallback (folders whose location has no pricing of its own) ────

ITEMS = {"bundles": [{"name": "Lone Peak Portraits"}, {"name": "Zipline Photos", "pricing_key": ""},
                     {"name": "Keyed", "pricing_key": "lone peak portraits"}, {"name": "Nothing Matches"}]}


@pytest.mark.parametrize("item_list,expected", [
    ("Lone Peak Portraits", OWN),      # list name matches a priced activity
    ("Keyed", OWN),                    # list points at an activity by pricing_key (any case)
    ("Nothing Matches", []),           # no priced activity found
    ("Not A List", []),                # list name not in the item lists
])
def test_item_list_fallback(main_module, settings, item_list, expected):
    settings(pricing=BASE, items_bundles=ITEMS,
             folder_meta={"2026-12-01|Base Area|Jones": {"item_list": item_list}})
    assert P(main_module, "Base Area", "2026-12-01", "Jones")["tiers"] == expected


def test_item_list_name_with_photos_suffix_matches_the_activity(main_module, settings):
    pricing = {**BASE, "activities": {**BASE["activities"], "Zipline": {"tiers": CAT}}}
    settings(pricing=pricing, items_bundles=ITEMS,
             folder_meta={"2026-12-01|Base Area|Jones": {"item_list": "Zipline Photos"}})
    assert P(main_module, "Base Area", "2026-12-01", "Jones")["tiers"] == CAT


def test_item_list_found_from_any_folder_that_day_when_no_family_given(main_module, settings):
    settings(pricing=BASE, items_bundles=ITEMS,
             folder_meta={"2026-12-01|Base Area|Jones": {"item_list": "Lone Peak Portraits"}})
    assert P(main_module, "Base Area", "2026-12-01")["tiers"] == OWN


def test_unknown_location_or_no_location_has_no_prices(main_module, settings):
    settings(pricing=BASE, folder_meta={})
    assert P(main_module, "Somewhere New", "2026-12-01", "X")["tiers"] == []
    assert P(main_module)["tiers"] == [] and P(main_module)["combos"] == [{"name": "C1"}]
