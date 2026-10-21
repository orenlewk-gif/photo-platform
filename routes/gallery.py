"""Routes: gallery. Moved from main.py unchanged; shared helpers still live in main.py."""
import json
import os
from PIL import Image
from core.checkout import CheckoutError
from core.images import fix_orientation
from core.pricing import ZIP_LOCS_SET
from core.search import COLOR_ENUM
from core.text import clean_location
from core.text import folder_key as _folder_key
from core.text import item_group_from_path as _item_group_from_path
from core.text import natural_sort_key
from core.text import pick_four as _pick_four
from core.text import to_r2_key
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from io import BytesIO
from rapidfuzz import fuzz

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/days")
def get_days():
    return main._folder_index()["days"]


@router.get("/api/dates")
def get_dates():
    dates = sorted(set(item["date"] for item in main.data if not item.get("partner")), reverse=True)
    return {"dates": dates}


@router.get("/api/locations")
def get_locations(date: str = Query(None)):
    if date:
        items = main._folder_index()["by_date"].get(date, [])
    else:
        items = [item for item in main.data if not item.get("draft") and not item.get("partner")]
    loc_map = {}
    all_previews = {}
    for item in items:
        display = clean_location(item["location"])
        loc_map[display] = item["location"]
        if display not in all_previews:
            all_previews[display] = []
        all_previews[display].append(item["path"])

    locations = []
    for display in sorted(loc_map.keys()):
        locations.append({
            "name":     display,
            "previews": _pick_four(all_previews[display])
        })
    return {"locations": locations}


@router.get("/api/subfolders")
def get_subfolders(date: str, location: str):
    """Return sub-folders for a location — last names for portrait locations, groups for activities."""
    is_portrait = main._is_portrait_location(location)
    subfolders = {}
    for item in main._folder_photos(date, location):
        key = (item.get("last_name", "") or item.get("group", "") if is_portrait else item.get("group", "")).strip()
        # Fallback: if group/last_name field is missing, parse the sub-folder from the path
        if not key:
            parts = item.get("path", "").replace("\\", "/").split("/")
            try:
                img_idx = next(i for i, p in enumerate(parts) if p == "images")
                if len(parts) > img_idx + 4:   # images/date/location/subfolder/filename
                    key = parts[img_idx + 3]
            except StopIteration:
                pass
        if not key:
            continue
        if key not in subfolders:
            subfolders[key] = []
        subfolders[key].append(item["path"])

    items = [
        {"name": name, "previews": _pick_four(subfolders[name])}
        for name in sorted(subfolders.keys())
    ]
    return {
        "type":      "portrait" if is_portrait else "group",
        "items":     items,
        "has_items": len(items) > 0
    }


@router.get("/api/last-name-search")
def last_name_search(request: Request, q: str = Query("")):
    """Autocomplete: return unique (last_name, date, location) combos matching q."""
    ip = main._client_ip(request)
    if not main._lastname_limiter.is_allowed(ip):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})
    q_lower = q.strip().lower()
    if len(q_lower) < 2:
        return {"results": []}

    index = main._folder_index()
    previews = index["name_previews"]   # key -> up to 4 paths
    results = []
    for key, entry in index["name_keys"]:
        kind, name_lower = key[0], key[1]
        if name_lower.startswith(q_lower) or (kind == "grp" and fuzz.partial_ratio(q_lower, name_lower) >= 75):
            results.append({**entry, "_key": key})

    def sort_key(x):
        name = x.get("last_name") or x.get("group", "")
        return (not name.lower().startswith(q_lower), name.lower(), x["date"])

    results.sort(key=sort_key)
    results = results[:20]
    for r in results:
        k = r.pop("_key")
        r["previews"] = previews.get(k, [])
    return {"results": results}


@router.get("/api/browse")
def browse(request: Request, date: str, location: str, family: str = Query(None), group: str = Query(None),
           dims: bool = Query(True)):
    """Photos in a folder. dims=0 skips measuring each photo's width/height — the POS
    doesn't use them, and measuring uncached photos can take seconds on first open."""
    def group_matches(item):
        if not group:
            return True
        g = item.get("group", "").strip()
        if not g:
            g = _item_group_from_path(item)
        return g.lower() == group.lower()

    # The POS and studio also see partner photos (Eagle Mount); the public gallery doesn't
    pool = [item for item in main._folder_photos(date, location, pos=main._nowm_allowed(request))
            if (not family or item.get("last_name","").strip().lower() == family.lower()
                 or (family and not item.get("last_name","").strip() and item.get("group","").strip().lower() == family.lower()))
            and group_matches(item)]
    pool.sort(key=lambda x: natural_sort_key(x["path"]))
    paths = [item["path"] for item in pool]
    sizes = main._dims_for_paths(paths) if dims else {}
    results = []
    for item in pool:
        w, h = sizes.get(item["path"], (0, 0))
        results.append({
            "path":      item["path"],
            "date":      item["date"],
            "location":  clean_location(item["location"]),
            "last_name": item.get("last_name", ""),
            "filename":  os.path.basename(item["path"]),
            "w": w,
            "h": h,
        })
        if item.get("partner"):                 # only the POS/studio ever get these
            results[-1]["partner"] = item["partner"]
    # Attach pose group index from folder_meta when available
    if family and results:
        fk = _folder_key(date, location, family)
        fm = main._load_folder_meta()
        folder_data = fm.get(fk, {})
        poses = folder_data.get("poses", [])
        if poses:
            fn_to_pose: dict = {}
            for pi, pose in enumerate(poses):
                for fn in pose.get("files", []):
                    fn_to_pose[fn] = pi
            for r in results:
                fn = os.path.basename(r["path"])
                r["pose_group"] = fn_to_pose.get(fn, -1)
    return {"count": len(results), "photos": results}


@router.get("/api/search")
def search(
    request:      Request,
    last_name:    str  = Query(None),
    date:         str  = Query(None),
    location:     str  = Query(None),
    group:        str  = Query(None),
    time_from:    int  = Query(None),
    time_to:      int  = Query(None),
    jacket_color: list[str] = Query(None),
    pants_color:  str       = Query(None),
    sport:        str       = Query(None),
    ski_instructor: bool      = Query(None),
):
    ip = main._client_ip(request)
    if not main._search_limiter.is_allowed(ip):
        return JSONResponse(status_code=429, content={"error": "Too many requests — please wait a moment"})
    try:
        return main._search(last_name, date, location, group, time_from, time_to,
                       jacket_color=jacket_color, pants_color=pants_color, sport=sport,
                       ski_instructor=ski_instructor)
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(e)})


@router.get("/api/pricing")
def get_pricing(request: Request, location: str = Query(None), date: str = Query(None), family: str = Query(None)):
    return main._pricing_for(location, date, family)


@router.get("/api/photo")
def get_photo(request: Request, path: str, size: str = Query("medium"), nowm: bool = Query(False)):
    """
    size=thumb  → 700px  (gallery thumbnails — a redirect to the stored thumbnail when there is one)
    size=medium → 1800px q90  (lightbox)
    size=full   → 2400px q92  (download-quality)
    nowm=1      → skip watermark (POS / studio only; others get the watermarked photo)
    """
    from fastapi.responses import StreamingResponse as SR
    SIZE_MAP = {
        "thumb":  (700,  80),
        "medium": (1800, 90),
        "full":   (2400, 92),
    }
    max_px, quality = SIZE_MAP.get(size, SIZE_MAP["medium"])
    cache_secs = 86400 if size == "thumb" else 3600
    if nowm and not main._nowm_allowed(request):
        nowm = False

    try:
        key = to_r2_key(path)
        if not key.startswith("images/"):
            return JSONResponse(status_code=400, content={"error": "Invalid path"})
        if os.getenv("R2_ENDPOINT_URL"):
            # Thumbs: cached copy if there is one, otherwise make it now (and the
            # other variants in the background) from one download of the original
            if size == "thumb":
                if nowm:
                    variant, link = "pos", main._stored_thumb_link(key, "pos")
                else:
                    variant, link = "grid", main._stored_thumb_link(key, "grid")   # full pattern; older ones are remade
                if link:
                    return main._thumb_redirect(link)
                data = main._thumb_on_demand(key, variant)
                return SR(BytesIO(data), media_type=main._THUMB_MEDIA_TYPES[main._THUMB_VARIANTS[variant][5]],
                          headers={"Cache-Control": f"{'private' if nowm else 'public'}, max-age={cache_secs}"})

            obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=key)
            img = Image.open(obj["Body"]).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            if not nowm:
                img = main.apply_watermark(img, size)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)

            buf.seek(0)
            return SR(buf, media_type="image/jpeg",
                      headers={"Cache-Control": f"public, max-age={cache_secs}"})
        elif os.path.exists(path):
            img = Image.open(path).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            if not nowm:
                img = main.apply_watermark(img, size)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            buf.seek(0)
            return SR(buf, media_type="image/jpeg")
        else:
            return JSONResponse(status_code=404, content={"error": "File not found"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/folder-colors")
def get_folder_colors(date: str = Query(""), location: str = Query(""), group: str = Query("")):
    """Return distinct jacket/pants colors, sport types, and valid combos for a folder."""
    jacket, pants = set(), set()
    has_ski = has_snowboard = has_instructor = False
    j_to_p: dict = {}
    p_to_j: dict = {}
    j_to_j: dict = {}
    loc_lower = location.strip().lower()
    grp_lower = group.strip().lower()
    for item in main.data:
        if item.get("draft") or item.get("partner"):
            continue
        if date and item.get("date") != date:
            continue
        if loc_lower and clean_location(item.get("location", "")).lower() != loc_lower:
            continue
        if grp_lower and (item.get("group", "") or item.get("last_name", "")).strip().lower() != grp_lower:
            continue
        _valid = set(COLOR_ENUM) - {"none", "unclear"}
        jcs = [c for c in (item.get("jacket_colors") or []) if c in _valid]
        pcs = [c for c in (item.get("pants_colors")  or []) if c in _valid]
        for c in jcs: jacket.add(c)
        for c in pcs: pants.add(c)
        if item.get("ski_instructor"): has_instructor = True
        for jc in jcs:
            j_to_p.setdefault(jc, set()).update(pcs)
            for pc in pcs:
                p_to_j.setdefault(pc, set()).add(jc)
        for i, jc1 in enumerate(jcs):
            for jc2 in jcs:
                if jc1 != jc2:
                    j_to_j.setdefault(jc1, set()).add(jc2)
        sp = item.get("sport")
        if sp == "ski":        has_ski = True
        elif sp == "snowboard": has_snowboard = True
    order = ["red","orange","yellow","green","blue","purple","pink","white","black","gray","brown","tan","teal"]
    def sort_colors(s):
        return sorted(s, key=lambda c: order.index(c) if c in order else 99)
    return {
        "jacket": sort_colors(jacket),
        "pants":  sort_colors(pants),
        "has_ski": has_ski, "has_snowboard": has_snowboard, "has_instructor": has_instructor,
        "combos": {
            "jacket_to_pants":  {jc: sort_colors(ps) for jc, ps in j_to_p.items()},
            "pants_to_jacket":  {pc: sort_colors(js) for pc, js in p_to_j.items()},
            "jacket_to_jacket": {jc: sort_colors(js) for jc, js in j_to_j.items()},
        },
    }


@router.get("/api/folder-times")
def get_folder_times(date: str = Query(...), location: str = Query(...)):
    """Return EXIF capture times (minutes since midnight) for photos in a time-search folder.
    Checks upload-time cache first, then a persisted R2 key, then reads EXIF from each photo."""
    cache_key = (date, location.strip().lower())
    if cache_key in main._folder_times_cache:
        return {"times": main._folder_times_cache[cache_key]}

    # Check persisted times written during upload (avoids per-photo R2 reads on cold start)
    try:
        obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=main._times_r2_key(date, location))
        persisted = json.loads(obj["Body"].read())
        if persisted:
            main._folder_times_cache[cache_key] = persisted
            return {"times": persisted}
    except Exception:
        pass

    loc_lower = location.strip().lower()
    paths = [
        item["path"] for item in main.data
        if not item.get("draft") and not item.get("partner")
        and item.get("date") == date
        and (item.get("location") or "").strip().lower() == loc_lower
    ]

    times: dict = {}
    EXIF_DATETIME_ORIGINAL = 36867
    EXIF_DATETIME          = 306

    for path in paths:
        fname = os.path.basename(path)
        try:
            key = to_r2_key(path)
            if os.getenv("R2_ENDPOINT_URL"):
                obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=key, Range="bytes=0-131071")
                buf = BytesIO(obj["Body"].read())
            else:
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as f:
                    buf = BytesIO(f.read(131072))
            img  = Image.open(buf)
            exif = img._getexif()
            if exif:
                dt_str = exif.get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
                if dt_str:
                    parts = str(dt_str).split(" ")
                    if len(parts) >= 2:
                        t = parts[1].split(":")
                        if len(t) >= 2:
                            h, m = int(t[0]), int(t[1])
                            if 0 <= h < 24 and 0 <= m < 60:
                                times[fname] = h * 60 + m
        except Exception:
            pass

    if times:
        main._folder_times_cache[cache_key] = times
    return {"times": times}


@router.get("/api/frames")
def get_frames():
    try:
        variations = main._frame_variations()
    except CheckoutError:
        return JSONResponse(status_code=502, content={"error": "WooCommerce unavailable"})
    frames = []
    for size_idx, label in enumerate(main._FRAME_SIZE_LABELS):
        entry = {"size": label, "landscape": None, "portrait": None}
        for orient in ("landscape", "portrait"):
            var_id = main.FRAME_VARIATION_MAP.get((size_idx, orient))
            if var_id is None:
                continue
            v = variations.get(var_id, {})
            entry[orient] = {
                "variation_id": var_id,
                "size":         label,
                "orientation":  orient,
                "price":        f"{v.get('price', 0):g}",
                "stock_status": v.get("stock_status", "instock"),
            }
        frames.append(entry)
    return {"frames": frames}


@router.get("/api/shop-prices")
def api_shop_prices():
    """Print, frame, shipping and logo prices for the gallery and checkout pages, from
    the Pricing settings and WooCommerce, so the pages show what checkout charges."""
    pp = main._load_print_pricing()
    sizes = pp.get("sizes", list(main._DEFAULT_PRINT_SIZES))
    try:
        variations = main._frame_variations()
    except CheckoutError:
        variations = {}
    def frame(i, orient):
        v = variations.get(main.FRAME_VARIATION_MAP.get((i, orient)))
        return v["price"] if v else None
    return {
        "print_sizes": [{"label": s.get("label", ""), "price": float(s.get("price", 0)),
                         "ship_print": float(s.get("ship_print", 8)), "ship_framed": float(s.get("ship_framed", 15))}
                        for s in sizes],
        "free_ship_threshold": int(pp.get("free_ship_threshold", 3)),
        "frame_prices": [{"landscape": frame(i, "landscape"), "portrait": frame(i, "portrait")} for i in range(len(sizes))],
        "logo_fee": float(main._load_pricing().get("logo_fee", 2) or 0),
    }


@router.get("/api/frame-photos")
def get_frame_photos():
    frames_dir = "frames"
    if not os.path.exists(frames_dir):
        return {"photos": {}}
    SIZE_LABELS = ["5x7", "8x10", "10x13", "16x20"]
    result = {}
    for size in SIZE_LABELS:
        photos = sorted([
            f"/frames/{f}" for f in os.listdir(frames_dir)
            if f.startswith(f"frame_{size}_") and f.lower().endswith((".jpg", ".jpeg", ".png"))
        ])
        result[size] = photos
    return {"photos": result}


@router.get("/api/upload/locations")
def api_upload_locations():
    """Public list of locations available in the upload tool — portrait + activities."""
    main._load_portrait_locations()
    pricing = main._load_pricing()
    activity_names = sorted(pricing.get("activities", {}).keys(), key=str.lower)
    portrait_set = set(l.lower() for l in main.PORTRAIT_LOCATIONS)

    # Build category list for the layered upload UI
    raw_cats = pricing.get("activity_categories", [])
    categories = []
    for cat in raw_cats:
        name = cat.get("name", "")
        acts = [a for a in (cat.get("acts") or []) if a]
        active = cat.get("active", True)
        categories.append({"name": name, "acts": acts, "active": active})
        # Portrait-type categories also expand the portrait detection set
        if name.strip().lower() in main._PORTRAIT_CATEGORY_NAMES:
            for act in acts:
                portrait_set.add(act.strip().lower())

    return {
        "portrait":   sorted(portrait_set, key=str.lower),
        "activities": activity_names,
        "categories": categories,
    }


@router.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("templates/index.html").read())


@router.head("/", include_in_schema=False)
def index_head():
    """Uptime monitors check the site with HEAD requests (headers only, no page)."""
    return Response(status_code=200, media_type="text/html")


@router.get("/api/zip-pricing")
def api_get_zip_pricing(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return main._load_zip_pricing()


@router.get("/api/viewer/zip-pricing")
def viewer_zip_pricing(request: Request):
    """Local-only: return zip pricing tiers for the viewer group size picker."""
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "Local only"})
    return main._load_zip_pricing()


@router.post("/api/viewer/set-group-size")
async def viewer_set_group_size(request: Request):
    """Local-only: set zip group size for a folder so viewer shows correct pricing."""
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "Local only"})
    body = await request.json()
    fk         = body.get("folder_key")
    group_size = body.get("group_size")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    folder_meta = main._load_folder_meta()
    if fk not in folder_meta:
        folder_meta[fk] = {}
    folder_meta[fk]["group_size"] = group_size
    main._save_folder_meta(folder_meta)
    return {"status": "ok"}


@router.post("/api/zip-pricing")
async def api_save_zip_pricing(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    main._save_zip_pricing(body)
    return {"status": "ok"}


@router.get("/api/zip-folders")
def api_get_zip_folders(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = main._load_folder_meta()
    folders = {}
    for item in main.data:
        loc = item.get("location", "").strip().lower()
        if loc not in ZIP_LOCS_SET:
            continue
        date = item.get("date", "")
        location = item.get("location", "").strip()
        last_name = (item.get("last_name") or item.get("group") or "").strip()
        if not last_name:
            continue
        fk = _folder_key(date, location, last_name)
        if fk not in folders:
            folders[fk] = {
                "folder_key": fk,
                "date": date,
                "location": location,
                "last_name": last_name,
                "photo_count": 0,
                "draft_count": 0,
                "group_size": folder_meta.get(fk, {}).get("group_size", None),
            }
        folders[fk]["photo_count"] += 1
        if item.get("draft"):
            folders[fk]["draft_count"] += 1
    result = sorted(folders.values(), key=lambda x: (x["date"], x["last_name"]), reverse=True)
    return {"folders": result}


@router.post("/api/zip-folder/set-group-size")
async def api_set_group_size(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk = body.get("folder_key")
    group_size = body.get("group_size")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    folder_meta = main._load_folder_meta()
    if fk not in folder_meta:
        folder_meta[fk] = {}
    folder_meta[fk]["group_size"] = group_size
    main._save_folder_meta(folder_meta)
    return {"status": "ok"}


@router.get("/api/zip-preview/{date}/{last_name}")
def api_zip_preview(request: Request, date: str, last_name: str):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = main._load_folder_meta()
    zip_pricing = main._load_zip_pricing()
    photos = []
    location = ""
    for item in main.data:
        loc = item.get("location", "").strip().lower()
        if loc not in ZIP_LOCS_SET:
            continue
        if item.get("date") != date:
            continue
        ln = (item.get("last_name") or item.get("group") or "").strip()
        if ln.lower() != last_name.lower():
            continue
        location = item.get("location", "").strip()
        photos.append({"path": item["path"], "filename": os.path.basename(item["path"])})
    fk = _folder_key(date, location, last_name)
    group_size = folder_meta.get(fk, {}).get("group_size")
    tiers = zip_pricing.get("tiers", [])
    tier_data = None
    for t in tiers:
        if str(t.get("people")) == str(group_size):
            tier_data = t
            break
    thumb_urls = []
    for p in photos[:50]:
        try:
            r2_key = to_r2_key(p["path"])
            url = main._presigned_get(r2_key, expires=3600)
            thumb_urls.append({"filename": p["filename"], "url": url})
        except:
            pass
    return {
        "date": date, "last_name": last_name, "location": location,
        "group_size": group_size, "tier": tier_data,
        "photo_count": len(photos), "photos": thumb_urls,
    }


@router.get("/api/print-pricing")
def api_print_pricing():
    return main._load_print_pricing()


@router.get("/api/portrait-pricing")
def api_portrait_pricing():
    return main._load_portrait_pricing()


@router.get("/api/seasons")
def api_get_seasons():
    return {"seasons": main._load_seasons()}


@router.get("/api/private-session-pricing")
def api_get_private_session_pricing():
    return main._load_private_session_pricing()


@router.get("/api/items-bundles")
def api_get_items_bundles():
    return main._load_items_bundles()
