"""The folder index: day list, folders, POS dates and name lookups, built in one pass over the photo list."""
from core.text import clean_location

# Locations whose groups (trails) can be found by name in the gallery's name search
SEARCHABLE_GROUP_LOCATIONS = {"mountain biking"}


def build_folder_index(items: list) -> dict:
    by_date, folders, date_locs = {}, {}, {}
    name_keys, name_previews = [], {}
    for item in items:
        loc = clean_location(item["location"])
        path = item.get("path", "")
        # Name lookups (like last_name_search, drafts included)
        ln = item.get("last_name", "").strip()
        if ln:
            key = ("ln", ln.lower(), item["date"], loc)
            if key not in name_previews:
                name_previews[key] = []
                name_keys.append((key, {"last_name": ln, "date": item["date"], "location": loc, "type": "family"}))
            if path and len(name_previews[key]) < 4:
                name_previews[key].append(path)
        if loc.lower() in SEARCHABLE_GROUP_LOCATIONS:
            g = item.get("group", "").strip()
            if g:
                key = ("grp", g.lower(), item["date"], loc)
                if key not in name_previews:
                    name_previews[key] = []
                    name_keys.append((key, {"group": g, "date": item["date"], "location": loc, "type": "group"}))
                if path and len(name_previews[key]) < 4:
                    name_previews[key].append(path)
        if item.get("draft"):
            continue
        by_date.setdefault(item["date"], []).append(item)
        folders.setdefault((item["date"], loc), []).append(item)
        if item.get("date", "").strip() and loc:
            date_locs.setdefault(item.get("date", "").strip(), set()).add(loc)

    # /api/days
    days = []
    for date in sorted(by_date, reverse=True):
        locs, galleries, loc_previews = set(), set(), {}
        for item in by_date[date]:
            loc = clean_location(item["location"])
            locs.add(loc)
            galleries.add((loc, item.get("last_name", "")))
            loc_previews.setdefault(loc, []).append(item["path"])
        # One photo per folder first, then fill remaining slots round-robin
        loc_lists = list(loc_previews.values())
        previews, i = [], 0
        while len(previews) < 4:
            added = False
            for lst in loc_lists:
                if i < len(lst):
                    previews.append(lst[i])
                    added = True
                    if len(previews) == 4:
                        break
            if not added:
                break
            i += 1
        days.append({"date": date, "previews": previews,
                     "folder_count": len(locs), "gallery_count": len(galleries)})

    pos_dates = [{"date": d, "locations": sorted(date_locs[d])}
                 for d in sorted(date_locs, reverse=True)[:90]]
    return {"by_date": by_date, "folders": folders, "days": {"days": days},
            "pos_dates": {"dates": pos_dates}, "name_keys": name_keys, "name_previews": name_previews}
