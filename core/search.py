"""Search over photo records: colour / sport filters and forgiving last-name matching."""
from rapidfuzz import fuzz

from core.text import clean_location, natural_sort_key

COLOR_ENUM = [
    "red", "orange", "yellow", "green", "blue", "purple", "pink",
    "white", "black", "gray", "brown", "teal", "none", "unclear",
]

# Colors folded into another one (too few photos to be a filter of their own). Photos already
# labeled with them count as the other color everywhere; they're never stored again.
MERGED_COLORS = {"camo": "green", "tan": "brown"}


def merged_colors(colors) -> list | None:
    """A photo's colors with the folded ones replaced (order kept, no repeats); None stays None."""
    if colors is None:
        return None
    return list(dict.fromkeys(MERGED_COLORS.get(c, c) for c in colors))

# Color families — similar shades treated as equivalent during filtering
COLOR_FAMILY: dict[str, set] = {
    "blue":   {"blue", "navy"},         # navy kept for existing labeled photos
    "brown":  {"brown", "tan"},     # tan/khaki is part of brown (MERGED_COLORS)
    "tan":    {"brown", "tan"},
    "red":    {"red", "maroon"},
    "maroon": {"maroon", "red"},
    "green":  {"green", "camo"},     # camo is part of green (MERGED_COLORS)
    "teal":   {"teal"},
    "gray":   {"gray"},
    "purple": {"purple", "violet"},
    "violet": {"violet", "purple"},
}


def color_family(color: str) -> set:
    return COLOR_FAMILY.get(color, {color})


def filter_by_labels(pool, date, location, group, jacket_colors, pants_color, sport, ski_instructor) -> list:
    """Live photos matching the colour chips / sport (every chosen jacket colour must match),
    in filename order. location is the cleaned folder name."""
    results = []
    for item in pool:
        if item.get("draft"):
            continue
        if date and item["date"] != date:
            continue
        if location and clean_location(item["location"]) != location:
            continue
        if group and item.get("group", "").strip().lower() != group.lower():
            continue
        item_jc = item.get("jacket_colors") or []
        if jacket_colors and not all(any(c in color_family(jf) for c in item_jc) for jf in jacket_colors):
            continue
        if pants_color and not any(c in color_family(pants_color) for c in (item.get("pants_colors") or [])):
            continue
        if sport and item.get("sport") != sport:
            continue
        if ski_instructor and not item.get("ski_instructor"):
            continue
        results.append(item)
    results.sort(key=lambda it: natural_sort_key(it["path"]))
    return results


def match_last_name(items, last_name, date, location, group) -> list:
    """[(score boost, photo)] whose last name is a close spelling of last_name, in filename order."""
    ln_filter = last_name.strip().lower() if last_name else ""
    results = []
    for item in items:
        if date and item["date"] != date:
            continue
        if location and clean_location(item["location"]) != location:
            continue
        if group and item.get("group", "").strip().lower() != group.lower():
            continue
        item_ln = item.get("last_name", "").strip().lower()
        if not item_ln or fuzz.partial_ratio(ln_filter, item_ln) < 80:
            continue
        boost = (fuzz.partial_ratio(ln_filter, item_ln) / 100) * 0.15
        results.append((boost, item))
    results.sort(key=lambda x: natural_sort_key(x[1]["path"]))
    return results
