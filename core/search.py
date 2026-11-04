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


# Forgiving search: a photo also counts for a color the labeling model nearly picked. The model's
# guesses are kept with each photo it labeled (model_labels.scores, core/classify.decide); one at
# or above the line makes the photo show for that color too, in its usual place (shot order).
# Photos without kept guesses (labeled before they were kept, or by hand) match their labels only.
CLOSE_LINE = {"jacket": 0.20, "pants": 0.20, "sport": 0.20}


def search_colors(item: dict, part: str) -> list:
    """The colors a search finds this photo under for "jacket" or "pants": its labels, then the
    model's close guesses."""
    out = list(item.get(f"{part}_colors") or [])
    for c, score in ((item.get("model_labels") or {}).get("scores") or {}).get(part) or []:
        if score >= CLOSE_LINE[part] and c not in out:
            out.append(c)
    return out


def search_sports(item: dict) -> set:
    """The sports a search finds this photo under: its label, and the other one if the model was close."""
    out = {item.get("sport")} if item.get("sport") else set()
    for sp, score in (((item.get("model_labels") or {}).get("scores") or {}).get("sport") or {}).items():
        if sp in ("ski", "snowboard") and score >= CLOSE_LINE["sport"]:
            out.add(sp)
    return out


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
        item_jc = search_colors(item, "jacket")
        if jacket_colors and not all(any(c in color_family(jf) for c in item_jc) for jf in jacket_colors):
            continue
        if pants_color and not any(c in color_family(pants_color) for c in search_colors(item, "pants")):
            continue
        if sport and sport not in search_sports(item):
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
