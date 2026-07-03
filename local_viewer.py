"""
local_viewer.py
---------------
Standalone local photo viewer for the storefront.
No watermarks. No cart. No pricing. No internet required.

Run:
    python3 local_viewer.py

Then open http://localhost:8080 in a browser.
Photos are served directly from the local `images/` folder.
"""

import os
import re
import json
import urllib.request
import urllib.parse
from io import BytesIO

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from PIL import Image, ImageOps
from rapidfuzz import fuzz
import uvicorn

LIVE_API = "https://photos.bigskyphotos.com"

app = FastAPI()

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")

# Portrait locations use last_name sub-folders (family/session name)
PORTRAIT_LOCATIONS = {"lone peak portraits", "explorer gondola", "ramcharger portraits",
                      "adventure zip", "adventure zip line", "adventure zipline",
                      "nature zip", "nature zip line", "nature zipline"}
# Activity locations whose groups are searchable (trail name + date)
SEARCHABLE_GROUP_LOCATIONS = {"mountain biking"}


def _safe_path(rel: str):
    """Resolve relative path and ensure it stays inside IMAGES_DIR."""
    rel = rel.lstrip("/")
    if not rel.startswith("images/"):
        return None
    subpath = rel[len("images/"):]
    full = os.path.normpath(os.path.join(IMAGES_DIR, subpath))
    # Must remain inside IMAGES_DIR
    if not full.startswith(os.path.normpath(IMAGES_DIR) + os.sep):
        return None
    return full


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def clean_location(raw: str) -> str:
    cleaned = re.sub(r'^[\d\-_\s]+', '', raw)
    return cleaned.replace('-', ' ').replace('_', ' ').strip().title()


def fix_orientation(img: Image.Image) -> Image.Image:
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


def natural_sort_key(path: str):
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


# ─────────────────────────────────────────
# LOAD images.json
# ─────────────────────────────────────────

_IMAGES_JSON = os.path.join(BASE_DIR, "images.json")
if os.path.exists(_IMAGES_JSON):
    with open(_IMAGES_JSON, "r") as f:
        _json_data = json.load(f)
    print(f"Loaded {len(_json_data)} photos from images.json.")
else:
    _json_data = []
    print("images.json not found — last-name search will scan folders directly.")


# ─────────────────────────────────────────
# BROWSE — scan folder structure
# ─────────────────────────────────────────

def _scan_images() -> list:
    """
    Scan the images/ directory for photos.
    Structure: images/YYYY-MM-DD/location-name/[sub-name/]filename.jpg

    For PORTRAIT_LOCATIONS (Lone Peak Portraits, Explorer Gondola):
        sub-folder name → last_name  (family name)
    For all other locations (Mountain Biking, Zip Line, etc.):
        sub-folder name → group      (trail/time-slot group)

    Returns list of dicts: path, date, location, last_name, group, filename
    """
    photos = []
    if not os.path.isdir(IMAGES_DIR):
        return photos

    for date_entry in sorted(os.listdir(IMAGES_DIR)):
        date_dir = os.path.join(IMAGES_DIR, date_entry)
        if not os.path.isdir(date_dir):
            continue
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_entry):
            continue

        for loc_entry in sorted(os.listdir(date_dir)):
            loc_dir = os.path.join(date_dir, loc_entry)
            if not os.path.isdir(loc_dir):
                continue

            loc_display  = clean_location(loc_entry)
            is_portrait  = loc_display.lower() in PORTRAIT_LOCATIONS

            direct_images = [
                f for f in sorted(os.listdir(loc_dir))
                if f.lower().endswith(('.jpg', '.jpeg', '.png'))
            ]
            subdirs = [
                e for e in sorted(os.listdir(loc_dir))
                if os.path.isdir(os.path.join(loc_dir, e)) and not e.startswith('.')
            ]

            for fname in direct_images:
                rel = f"images/{date_entry}/{loc_entry}/{fname}"
                photos.append({
                    "path":      rel,
                    "date":      date_entry,
                    "location":  loc_entry,
                    "last_name": "",
                    "group":     "",
                    "filename":  fname,
                })

            for sub_entry in subdirs:
                sub_dir = os.path.join(loc_dir, sub_entry)
                sub_images = sorted([
                    f for f in os.listdir(sub_dir)
                    if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                ])
                for fname in sub_images:
                    rel = f"images/{date_entry}/{loc_entry}/{sub_entry}/{fname}"
                    photos.append({
                        "path":      rel,
                        "date":      date_entry,
                        "location":  loc_entry,
                        "last_name": sub_entry if is_portrait else "",
                        "group":     "" if is_portrait else sub_entry,
                        "filename":  fname,
                    })

    return photos


def _pick_four(photos: list) -> list:
    n = len(photos)
    if n <= 4:
        return photos
    step = n / 4
    return [photos[int(i * step)] for i in range(4)]


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

@app.get("/api/days")
def get_days():
    photos = _scan_images()
    days: dict = {}
    for item in photos:
        d = item["date"]
        if d not in days:
            days[d] = {"locations": set(), "galleries": set(), "loc_previews": {}}
        loc = clean_location(item["location"])
        days[d]["locations"].add(loc)
        days[d]["galleries"].add((loc, item.get("last_name", "")))
        if loc not in days[d]["loc_previews"]:
            days[d]["loc_previews"][loc] = []
        days[d]["loc_previews"][loc].append(item["path"])

    result = []
    for date in sorted(days.keys(), reverse=True):
        d = days[date]
        loc_lists = list(d["loc_previews"].values())
        previews = []
        i = 0
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
        result.append({
            "date":          date,
            "previews":      previews,
            "folder_count":  len(d["locations"]),
            "gallery_count": len(d["galleries"]),
        })
    return {"days": result}


@app.get("/api/locations")
def get_locations(date: str = Query(None)):
    photos = _scan_images()
    if date:
        photos = [p for p in photos if p["date"] == date]
    loc_map: dict = {}
    all_previews: dict = {}
    for item in photos:
        display = clean_location(item["location"])
        loc_map[display] = item["location"]
        if display not in all_previews:
            all_previews[display] = []
        all_previews[display].append(item["path"])

    locations = []
    for display in sorted(loc_map.keys()):
        locations.append({
            "name":     display,
            "previews": _pick_four(all_previews[display]),
        })
    return {"locations": locations}


@app.get("/api/families")
def get_families(date: str, location: str):
    """Legacy endpoint — kept for compatibility. Prefer /api/subfolders."""
    data = get_subfolders(date, location)
    # Return in old shape so any old callers still work
    return {
        "families":    [{"name": s["name"], "previews": s["previews"]} for s in data["subfolders"]],
        "has_families": data["has_subfolders"],
    }


@app.get("/api/subfolders")
def get_subfolders(date: str, location: str):
    """
    Unified sub-folder listing for a date+location.
    Portrait locations → type='portrait', keyed by last_name.
    Activity locations → type='group',    keyed by group name.
    """
    def pick_four(lst):
        n = len(lst)
        if n <= 4:
            return lst
        step = n / 4
        return [lst[int(i * step)] for i in range(4)]

    photos    = _scan_images()
    loc_lower = location.lower()
    is_portrait = loc_lower in PORTRAIT_LOCATIONS
    field       = "last_name" if is_portrait else "group"

    subs: dict = {}
    for item in photos:
        if item["date"] != date:
            continue
        if clean_location(item["location"]) != location:
            continue
        key = item.get(field, "").strip()
        if not key:
            continue
        if key not in subs:
            subs[key] = []
        subs[key].append(item["path"])

    result = []
    for name in sorted(subs.keys()):
        result.append({"name": name, "previews": pick_four(subs[name])})

    subfolder_type = "portrait" if is_portrait else "group"
    return {"subfolders": result, "has_subfolders": len(result) > 0, "type": subfolder_type}


@app.get("/api/browse")
def browse(date: str, location: str, family: str = Query(None), group: str = Query(None)):
    photos = _scan_images()
    pool = [
        p for p in photos
        if p["date"] == date
        and clean_location(p["location"]) == location
        and (not family or p.get("last_name", "").strip().lower() == family.lower())
        and (not group  or p.get("group",     "").strip().lower() == group.lower())
    ]
    pool.sort(key=lambda x: natural_sort_key(x["path"]))
    results = [
        {
            "path":      p["path"],
            "date":      p["date"],
            "location":  clean_location(p["location"]),
            "last_name": p.get("last_name", ""),
            "group":     p.get("group", ""),
            "filename":  os.path.basename(p["path"]),
        }
        for p in pool
    ]
    return {"count": len(results), "photos": results}


@app.get("/api/last-name-search")
def last_name_search(q: str = Query("")):
    """
    Fuzzy autocomplete using images.json (or folder scan as fallback).
    Searches last_name for portrait locations and group name for
    searchable activity locations (Mountain Biking).
    """
    q_lower = q.strip().lower()
    if len(q_lower) < 2:
        return {"results": []}

    source = _json_data if _json_data else _scan_images()
    seen = set()
    results = []
    for item in source:
        loc = clean_location(item["location"])
        loc_lower = loc.lower()
        is_portrait  = loc_lower in PORTRAIT_LOCATIONS
        is_searchable_group = loc_lower in SEARCHABLE_GROUP_LOCATIONS

        if is_portrait:
            val = item.get("last_name", "").strip()
            field = "last_name"
        elif is_searchable_group:
            val = item.get("group", "").strip()
            field = "group"
        else:
            continue  # browse-only location — not searchable

        if not val:
            continue
        val_lower = val.lower()
        if not val_lower.startswith(q_lower) and fuzz.partial_ratio(q_lower, val_lower) < 75:
            continue
        key = (field, val_lower, item["date"], loc)
        if key not in seen:
            seen.add(key)
            entry = {"date": item["date"], "location": loc}
            if field == "last_name":
                entry["last_name"] = val
            else:
                entry["group"] = val
            results.append(entry)

    results.sort(key=lambda x: (
        not (x.get("last_name") or x.get("group", "")).lower().startswith(q_lower),
        (x.get("last_name") or x.get("group", "")).lower(),
        x["date"],
    ))
    return {"results": results[:20]}


def _canonical_location(loc: str) -> str:
    """Map local folder name variants to the canonical name used in folder_meta keys on Railway."""
    l = loc.strip().lower()
    if l in {"nature zip", "nature zipline"}:
        return "Nature Zip Line"
    if l in {"adventure zipline"}:
        return "Adventure Zip Line"
    return loc


@app.get("/api/pricing")
def proxy_pricing(location: str = Query(None), date: str = Query(None), family: str = Query(None)):
    """Proxy to live pricing API so the local viewer shows current pricing without CORS issues."""
    try:
        params = {}
        if location: params["location"] = _canonical_location(location)
        if date:     params["date"]     = date
        if family:   params["family"]   = family
        url = LIVE_API + "/api/pricing"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "CrystalImagesLocalViewer/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
        return JSONResponse(content=data)
    except Exception:
        return JSONResponse(content={"tiers": [], "combos": []})


@app.get("/api/search")
def search(last_name: str = Query(None), date: str = Query(None), location: str = Query(None)):
    """Simple last-name + optional date/location filter (no CLIP)."""
    if not last_name:
        return JSONResponse(status_code=400, content={"error": "Provide last_name"})

    ln_filter = last_name.strip().lower()
    source = _json_data if _json_data else _scan_images()
    results = []
    for item in source:
        if date and item["date"] != date:
            continue
        if location and clean_location(item["location"]) != location:
            continue
        item_ln = item.get("last_name", "").strip().lower()
        if not item_ln or fuzz.partial_ratio(ln_filter, item_ln) < 80:
            continue
        results.append(item)

    results.sort(key=lambda x: natural_sort_key(x["path"]))
    photos = [
        {
            "path":      p["path"],
            "date":      p["date"],
            "location":  clean_location(p["location"]),
            "last_name": p.get("last_name", ""),
            "filename":  os.path.basename(p["path"]),
        }
        for p in results
    ]
    return {"count": len(photos), "photos": photos}


@app.get("/api/photo")
def get_photo(path: str, size: str = Query("thumb")):
    """
    size=thumb  → 500px  q80  (gallery thumbnails, cached 24h)
    size=medium → 1800px q90  (lightbox)
    size=full   → 2400px q92  (download-quality)
    """
    SIZE_MAP = {
        "thumb":  (500,  80),
        "medium": (1800, 90),
        "full":   (2400, 92),
    }
    max_px, quality = SIZE_MAP.get(size, SIZE_MAP["thumb"])
    cache_secs = 86400 if size == "thumb" else 3600

    full_path = _safe_path(path)
    if full_path is None or not os.path.isfile(full_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})

    try:
        img = Image.open(full_path).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="image/jpeg",
            headers={"Cache-Control": f"public, max-age={cache_secs}"},
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    template = os.path.join(BASE_DIR, "templates", "viewer.html")
    with open(template, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("\n  Crystal Images — Local Viewer")
    print("  ──────────────────────────────────")
    print("  Open http://localhost:8080 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
