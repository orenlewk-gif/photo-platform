"""Photo pricing rules: which tiers a folder shows, and the shop's best price for a number of photos.
Settings are read through the load_* functions the web app passes in."""
import re

from core.text import folder_key

ZIP_LOCS_SET = {"adventure zip", "adventure zip line", "adventure zipline", "nature zip", "nature zip line", "nature zipline"}


def best_tier_price(count: int, tiers: list) -> float:
    """The price the shop shows for `count` photos (same rule as calcPriceFor in
    index.html / checkout.html): the cheapest mix of single photos and multi-photo
    tiers, capped by an "Up to N" (max) tier, or by the All Photos price however
    many photos are picked."""
    if not tiers or count <= 0:
        return 0.0
    def num(t):                     # "All Photos" covers any number of photos
        return float("inf") if t.get("count") == "all" else t.get("count")
    all_tier = next((t for t in tiers if t.get("count") == "all"), None)
    max_tiers = sorted((t for t in tiers if t.get("max")), key=lambda t: num(t))
    single = next((t for t in tiers if t.get("count") == 1), None)
    unit = float(single["price"]) if single else 25.0
    best = count * unit
    bundles = sorted((t for t in tiers if not t.get("max") and isinstance(t.get("count"), int)
                      and not isinstance(t.get("count"), bool) and 1 < t["count"] <= count),
                     key=lambda t: -t["count"])
    for t in bundles:
        best = min(best, (count // t["count"]) * float(t["price"]) + (count % t["count"]) * unit)
    if max_tiers:
        cap = next((t for t in max_tiers if count <= num(t)), None)
        if cap and float(cap["price"]) < best:
            best = float(cap["price"])
    elif all_tier and best > float(all_tier["price"]):
        best = float(all_tier["price"])
    return round(best, 2)


def pricing_for(location: str = None, date: str = None, family: str = None, *,
                load_pricing, load_folder_meta, load_zip_pricing, load_items_bundles) -> dict:
    """The pricing tiers the shop shows for a folder (location, date, family): zip group
    pricing, date overrides, category pricing, the location's own pricing, then its
    item list. Checkout prices with the same tiers, so what's shown is what's charged."""
    # Zip group size pricing — looks up folder's assigned group size
    if location and date and family:
        loc_lower = location.strip().lower()
        if loc_lower in ZIP_LOCS_SET:
            try:
                fm = load_folder_meta()
                fk = folder_key(date, location.strip(), family.strip())
                group_size = fm.get(fk, {}).get("group_size")
                # Fallback: try old "Zip Line" key variants for uploads done before rename
                if not group_size:
                    _ZIP_ALIASES = {"Nature Zip": "Nature Zip Line", "Adventure Zip": "Adventure Zip Line"}
                    alt_loc = _ZIP_ALIASES.get(location.strip())
                    if alt_loc:
                        alt_fk = folder_key(date, alt_loc, family.strip())
                        group_size = fm.get(alt_fk, {}).get("group_size")
                if group_size:
                    zp = load_zip_pricing()
                    for t in zp.get("tiers", []):
                        if str(t.get("people")) == str(group_size):
                            tiers = []
                            if t.get("one_photo"):    tiers.append({"label": "1 Photo",    "count": 1,     "price": t["one_photo"]})
                            if t.get("two_photos"):   tiers.append({"label": "2 Photos",   "count": 2,     "price": t["two_photos"]})
                            if t.get("three_photos"): tiers.append({"label": "3 Photos",   "count": 3,     "price": t["three_photos"]})
                            if t.get("all_photos"):   tiers.append({"label": "All Photos", "count": "all", "price": t["all_photos"]})
                            if tiers:
                                return {"tiers": tiers, "combos": [], "zip_group_size": group_size}
            except Exception as e:
                print(f"zip pricing lookup error: {e}")
    pricing = load_pricing()
    combos = pricing.get("combos", [])
    # Build activity_groups: category name → list of its acts (individual location names).
    # Explicit entries in pricing["activity_groups"] override the derived ones.
    _derived_groups = {
        cat["name"]: cat.get("acts", [])
        for cat in pricing.get("activity_categories", [])
        if cat.get("name")
    }
    _activity_groups = {**_derived_groups, **pricing.get("activity_groups", {})}
    if location and date:
        date_overrides = pricing.get("date_overrides", {})
        date_ov = date_overrides.get(date, {})
        ov_key = next((k for k in date_ov if k.lower() == location.strip().lower()), None)
        if ov_key:
            result = dict(date_ov[ov_key])
            result["combos"] = combos
            result["activity_groups"] = _activity_groups
            return result
    if location:
        activities = pricing.get("activities", {})

        def _act_result(act_data):
            """Return activity pricing in tiers format, converting per_photo/all_photos if needed."""
            result = dict(act_data)
            result["combos"] = combos
            result["activity_groups"] = _activity_groups
            if "per_photo" in result and "tiers" not in result:
                per   = float(result.get("per_photo", 25))
                all_p = result.get("all_photos")
                tiers = [{"label": "1 Photo", "count": 1, "price": per}]
                if all_p:
                    tiers.append({"label": "All Photos", "count": 999, "price": float(all_p), "max": True})
                result["tiers"] = tiers
            # If tiers is empty but flags indicate an action gallery, inject defaults inline
            # (guards against stale empty-tiers state in R2 without requiring a restart)
            if not result.get("tiers") and result.get("flags", {}).get("time_search"):
                result["tiers"] = [
                    {"label": "1 Photo",    "count": 1,  "price": 25},
                    {"label": "All Photos", "count": 30, "price": 115, "max": True},
                ]
            return result

        # Category pricing wins: locations inherit their category's tiers
        for act_name, group_locs in _activity_groups.items():
            if location.strip().lower() in [l.lower() for l in group_locs]:
                pk_act = next((k for k in activities if k.lower() == act_name.lower()), None)
                if pk_act:
                    result = dict(activities[pk_act])
                    # Merge in location-level flags (time_search, clip, etc.) if present
                    loc_key = next((k for k in activities if k.lower() == location.strip().lower()), None)
                    if loc_key and activities[loc_key].get("flags"):
                        result = dict(result)
                        result["flags"] = {**activities[loc_key]["flags"], **result.get("flags", {})}
                    return _act_result(result)
        # Fall back to direct location match (location-specific pricing)
        act_key = next((k for k in activities if k.lower() == location.strip().lower()), None)
        if act_key:
            return _act_result(activities[act_key])
        # Fall back to item_list name when location has no direct pricing
        if date:
            try:
                fm = load_folder_meta()
                fk = folder_key(date, location.strip(), family.strip() if family else "")
                item_list_name = fm.get(fk, {}).get("item_list", "")
                if not item_list_name:
                    # Try prefix match when family is not in the request
                    prefix = f"{date}|{location.strip()}|"
                    for k, v in fm.items():
                        if k.startswith(prefix) and v.get("item_list"):
                            item_list_name = v["item_list"]
                            break
                if item_list_name:
                    bundles_data = load_items_bundles()
                    bundle = next((b for b in bundles_data.get("bundles", [])
                                   if b.get("name") == item_list_name), None)
                    if bundle:
                        pk = bundle.get("pricing_key", "")
                        if not pk:
                            bname = bundle.get("name", "")
                            candidates = [bname] + [
                                re.sub(r'\s+(Photos?|Images?|Portraits?)\s*$', '', bname, flags=re.IGNORECASE).strip()
                            ]
                            for candidate in candidates:
                                pk_act = next((k for k in activities if k.lower() == candidate.lower()), None)
                                if pk_act:
                                    return _act_result(activities[pk_act])
                        else:
                            pk_act = next((k for k in activities if k.lower() == pk.lower()), None)
                            if pk_act:
                                return _act_result(activities[pk_act])
            except Exception as e:
                print(f"item_list pricing fallback error: {e}")
        return {"tiers": [], "combos": combos, "activity_groups": _activity_groups}
    return {"tiers": [], "combos": combos, "activity_groups": _activity_groups}
