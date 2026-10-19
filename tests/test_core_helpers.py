"""The small shared helpers in core/ (moved out of main.py unchanged)."""
from core.images import admin_thumb_r2_key, grid_thumb_r2_key, nowm_thumb_r2_key, thumb_r2_key
from core.text import clean_location, folder_key, item_group_from_path, natural_sort_key, pick_four, to_r2_key


def test_clean_location():
    assert clean_location("03-el-dorado_action") == "El Dorado Action"
    assert clean_location("Ambush Action ") == "Ambush Action"


def test_natural_sort_puts_numbers_in_counting_order():
    names = ["IMG_10.jpg", "IMG_2.jpg", "img_1.JPG"]
    assert sorted(names, key=natural_sort_key) == ["img_1.JPG", "IMG_2.jpg", "IMG_10.jpg"]


def test_pick_four_spreads_previews_evenly():
    assert pick_four([1, 2, 3]) == [1, 2, 3]
    assert pick_four(list(range(8))) == [0, 2, 4, 6]


def test_group_from_path_and_keys():
    assert item_group_from_path({"path": "images/2026-12-01/mountain-biking/trail-a/IMG_1.jpg"}) == "trail-a"
    assert item_group_from_path({"path": "images/2026-12-01/mountain-biking/IMG_1.jpg"}) == ""
    assert folder_key("2026-12-01", "Lone Peak Portraits", "Smith") == "2026-12-01|Lone Peak Portraits|Smith"
    assert to_r2_key("/Users/x/photos/images/2026-12-01/a.jpg") == "images/2026-12-01/a.jpg"


def test_thumbnail_storage_names():
    k = "images/2026-12-01/el-dorado-action/A.JPG"
    assert thumb_r2_key(k) == "thumbs/v2/2026-12-01/el-dorado-action/A.JPG"
    assert nowm_thumb_r2_key(k) == "thumbs/nowm/2026-12-01/el-dorado-action/A.JPG"
    assert grid_thumb_r2_key(k) == "thumbs/grid/2026-12-01/el-dorado-action/A.JPG.webp"
    assert admin_thumb_r2_key(k) == "admin_thumbs/2026-12-01/el-dorado-action/A.JPG"
