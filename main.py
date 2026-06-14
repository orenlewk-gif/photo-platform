import threading
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import json
import torch
import re
import hmac
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from transformers import CLIPProcessor, CLIPModel
from rapidfuzz import fuzz
import os
import base64
from io import BytesIO
from PIL import Image, ImageOps
from functools import lru_cache
import boto3
import requests as http_requests
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

if os.path.exists("frames"):
    app.mount("/frames", StaticFiles(directory="frames"), name="frames")

# ─────────────────────────────────────────
# R2 CLIENT
# ─────────────────────────────────────────

from botocore.config import Config as BotocoreConfig
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    config=BotocoreConfig(signature_version="s3v4"),
)
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "crystal-images")

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

# Locations that use last-name sub-folders (portrait style)
PORTRAIT_LOCATIONS = {"lone peak portraits", "explorer gondola", "ramcharger portraits"}
# Locations whose group/sub-folder names are searchable
SEARCHABLE_GROUP_LOCATIONS = {"mountain biking"}

def clean_location(raw):
    cleaned = re.sub(r'^[\d\-_\s]+', '', raw)
    return cleaned.replace('-', ' ').replace('_', ' ').strip().title()

def fix_orientation(img):
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

WATERMARK_OPACITY = 0.40
_watermark_cache  = None
_watermark_lock   = threading.Lock()

def get_watermark():
    global _watermark_cache
    if _watermark_cache is not None:
        return _watermark_cache
    with _watermark_lock:
        if _watermark_cache is not None:
            return _watermark_cache
        if not os.path.exists("watermark.png"):
            return None
        try:
            wm = Image.open("watermark.png").convert("RGBA")
            # Resize to 500px max BEFORE flood fill — BFS on small image is instant
            wm.thumbnail((500, 500), Image.LANCZOS)
            w, h = wm.size
            print(f"Processing watermark at {w}x{h}...")
            px = wm.load()
            THRESH = 230

            # Pass 1: flood fill from edges → removes outer white background
            bg = set()
            queue = []
            for x in range(w):
                for y in (0, h - 1):
                    if (x, y) not in bg:
                        r, g, b, a = px[x, y]
                        if r > THRESH and g > THRESH and b > THRESH:
                            bg.add((x, y)); queue.append((x, y))
            for y in range(h):
                for x in (0, w - 1):
                    if (x, y) not in bg:
                        r, g, b, a = px[x, y]
                        if r > THRESH and g > THRESH and b > THRESH:
                            bg.add((x, y)); queue.append((x, y))
            while queue:
                cx, cy = queue.pop()
                px[cx, cy] = (px[cx, cy][0], px[cx, cy][1], px[cx, cy][2], 0)
                for dx, dy in ((-1,0),(1,0),(0,-1),(0,1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in bg:
                        r, g, b, a = px[nx, ny]
                        if r > THRESH and g > THRESH and b > THRESH:
                            bg.add((nx, ny)); queue.append((nx, ny))

            # Pass 2: large enclosed white regions = donut hole → transparent
            # Small white regions = text → keep
            seen = set()
            hole_threshold = (w * h) * 0.005
            for sy in range(h):
                for sx in range(w):
                    if (sx, sy) in bg or (sx, sy) in seen:
                        continue
                    r, g, b, a = px[sx, sy]
                    if not (r > THRESH and g > THRESH and b > THRESH and a > 0):
                        continue
                    component = []
                    q2 = [(sx, sy)]
                    seen.add((sx, sy))
                    while q2:
                        cx, cy = q2.pop()
                        component.append((cx, cy))
                        for dx, dy in ((-1,0),(1,0),(0,-1),(0,1)):
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in bg and (nx, ny) not in seen:
                                r2, g2, b2, a2 = px[nx, ny]
                                if r2 > THRESH and g2 > THRESH and b2 > THRESH and a2 > 0:
                                    seen.add((nx, ny)); q2.append((nx, ny))
                    if len(component) > hole_threshold:
                        for cx, cy in component:
                            px[cx, cy] = (px[cx, cy][0], px[cx, cy][1], px[cx, cy][2], 0)

            data = list(wm.getdata())
            wm.putdata([(r, g, b, int(a * WATERMARK_OPACITY)) for r, g, b, a in data])
            _watermark_cache = wm
            print("Watermark ready.")
        except Exception as e:
            print(f"Watermark load failed: {e} — photos will serve without watermark")
            _watermark_cache = False  # False = tried and failed, don't retry
        return _watermark_cache if _watermark_cache else None

def apply_watermark(img, size="medium"):
    wm = get_watermark()
    if wm is None:
        return img
    img_rgba = img.convert("RGBA")

    if size == "thumb":
        # Single watermark centred, 60% of shorter dimension
        wm_size = int(min(img.width, img.height) * 0.60)
        wm_scaled = wm.resize((wm_size, wm_size), Image.LANCZOS)
        x = (img.width  - wm_size) // 2
        y = (img.height - wm_size) // 2
        img_rgba.paste(wm_scaled, (x, y), wm_scaled)
    else:
        # Two watermarks side by side for enlarged view
        wm_size = int(min(img.width, img.height) * 0.40)
        gap     = int(wm_size * 0.20)
        total_w = wm_size * 2 + gap
        wm_scaled = wm.resize((wm_size, wm_size), Image.LANCZOS)
        y  = (img.height - wm_size) // 2
        x1 = (img.width  - total_w) // 2
        x2 = x1 + wm_size + gap
        img_rgba.paste(wm_scaled, (x1, y), wm_scaled)
        img_rgba.paste(wm_scaled, (x2, y), wm_scaled)

    return img_rgba.convert("RGB")

def image_to_base64(img, max_size=1200):
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=87)
    return base64.b64encode(buf.getvalue()).decode()

# ─────────────────────────────────────────
# LOAD MODEL & DATA (once on startup)
# ─────────────────────────────────────────

model     = None
processor = None

def get_model():
    global model, processor
    if model is None:
        print("Loading CLIP model...")
        model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        print("CLIP model loaded.")
    return model, processor

# Pre-load CLIP in background so it's ready before the first search request
threading.Thread(target=get_model, daemon=True).start()

if os.path.exists("images.json"):
    with open("images.json", "r") as f:
        data = json.load(f)
else:
    print("images.json not found locally — downloading from R2...")
    obj = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
    data = json.loads(obj["Body"].read().decode("utf-8"))
print(f"Loaded {len(data)} photos.")


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

@app.get("/api/days")
def get_days():
    days = {}
    for item in data:
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
        # One photo per folder first, then fill remaining slots round-robin
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
            "gallery_count": len(d["galleries"])
        })
    return {"days": result}


@app.get("/api/dates")
def get_dates():
    dates = sorted(set(item["date"] for item in data), reverse=True)
    return {"dates": dates}


@app.get("/api/locations")
def get_locations(date: str = Query(None)):
    if date:
        items = [item for item in data if item["date"] == date]
    else:
        items = data
    loc_map = {}
    all_previews = {}
    for item in items:
        display = clean_location(item["location"])
        loc_map[display] = item["location"]
        if display not in all_previews:
            all_previews[display] = []
        all_previews[display].append(item["path"])

    def pick_four(photos):
        n = len(photos)
        if n <= 4:
            return photos
        step = n / 4
        return [photos[int(i * step)] for i in range(4)]

    locations = []
    for display in sorted(loc_map.keys()):
        locations.append({
            "name":     display,
            "previews": pick_four(all_previews[display])
        })
    return {"locations": locations}


@app.get("/api/subfolders")
def get_subfolders(date: str, location: str):
    """Return sub-folders for a location — last names for portrait locations, groups for activities."""
    def pick_four(photos):
        n = len(photos)
        if n <= 4:
            return photos
        step = n / 4
        return [photos[int(i * step)] for i in range(4)]

    is_portrait = location.lower() in PORTRAIT_LOCATIONS
    subfolders = {}
    for item in data:
        if item["date"] != date:
            continue
        if clean_location(item["location"]) != location:
            continue
        key = (item.get("last_name", "") if is_portrait else item.get("group", "")).strip()
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
        {"name": name, "previews": pick_four(subfolders[name])}
        for name in sorted(subfolders.keys())
    ]
    return {
        "type":      "portrait" if is_portrait else "group",
        "items":     items,
        "has_items": len(items) > 0
    }


@app.get("/api/families")
def get_families(date: str, location: str):
    """Return last-name subfolders for a location, plus preview photos for each."""
    families = {}
    for item in data:
        if item["date"] != date:
            continue
        if clean_location(item["location"]) != location:
            continue
        ln = item.get("last_name", "").strip()
        if not ln:
            continue
        if ln not in families:
            families[ln] = []
        families[ln].append(item["path"])

    def pick_four(photos):
        n = len(photos)
        if n <= 4:
            return photos
        step = n / 4
        return [photos[int(i * step)] for i in range(4)]

    result = []
    for name in sorted(families.keys()):
        result.append({"name": name, "previews": pick_four(families[name])})
    return {"families": result, "has_families": len(result) > 0}


@app.get("/api/last-name-search")
def last_name_search(q: str = Query("")):
    """Autocomplete: return unique (last_name, date, location) combos matching q."""
    q_lower = q.strip().lower()
    if len(q_lower) < 2:
        return {"results": []}

    seen = set()
    results = []
    for item in data:
        loc = clean_location(item["location"])

        # Last-name search (portrait locations)
        ln = item.get("last_name", "").strip()
        if ln:
            ln_lower = ln.lower()
            if ln_lower.startswith(q_lower) or fuzz.partial_ratio(q_lower, ln_lower) >= 75:
                key = ("ln", ln_lower, item["date"], loc)
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "last_name": ln,
                        "date":      item["date"],
                        "location":  loc,
                        "type":      "family",
                    })

        # Group/trail search (Mountain Biking only)
        if loc.lower() in SEARCHABLE_GROUP_LOCATIONS:
            g = item.get("group", "").strip()
            if g:
                g_lower = g.lower()
                if g_lower.startswith(q_lower) or fuzz.partial_ratio(q_lower, g_lower) >= 75:
                    key = ("grp", g_lower, item["date"], loc)
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "group":    g,
                            "date":     item["date"],
                            "location": loc,
                            "type":     "group",
                        })

    def sort_key(x):
        name = x.get("last_name") or x.get("group", "")
        return (not name.lower().startswith(q_lower), name.lower(), x["date"])

    results.sort(key=sort_key)
    return {"results": results[:20]}


def natural_sort_key(path):
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]

def _item_group_from_path(item):
    """Fallback: parse the sub-folder (group) from the path when the group field is empty."""
    parts = item.get("path", "").replace("\\", "/").split("/")
    try:
        img_idx = next(i for i, p in enumerate(parts) if p == "images")
        if len(parts) > img_idx + 4:
            return parts[img_idx + 3]
    except StopIteration:
        pass
    return ""

@app.get("/api/browse")
def browse(date: str, location: str, family: str = Query(None), group: str = Query(None)):
    def group_matches(item):
        if not group:
            return True
        g = item.get("group", "").strip()
        if not g:
            g = _item_group_from_path(item)
        return g.lower() == group.lower()

    pool = [item for item in data
            if item["date"] == date
            and clean_location(item["location"]) == location
            and (not family or item.get("last_name","").strip().lower() == family.lower())
            and group_matches(item)]
    pool.sort(key=lambda x: natural_sort_key(x["path"]))
    results = []
    for item in pool:
        results.append({
            "path":      item["path"],
            "date":      item["date"],
            "location":  clean_location(item["location"]),
            "last_name": item.get("last_name", ""),
            "filename":  os.path.basename(item["path"])
        })
    return {"count": len(results), "photos": results}


@app.get("/api/search")
def search(
    query:     str  = Query(None),
    last_name: str  = Query(None),
    date:      str  = Query(None),
    location:  str  = Query(None),
    group:     str  = Query(None),
):
    try:
        return _search(query, last_name, date, location, group)
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(e)})

def _search(query, last_name, date, location, group=None):
    if not query and not last_name:
        return JSONResponse(status_code=400, content={"error": "Provide query or last_name"})

    # Text embedding
    text_embedding = None
    if query:
        m, p = get_model()
        inputs = p(text=[query], return_tensors="pt", padding=True)
        with torch.no_grad():
            # Use text_model + text_projection directly — avoids API differences
            # across transformers versions where get_text_features may return
            # a BaseModelOutputWithPooling object instead of a plain tensor.
            text_out  = m.text_model(**inputs)
            pooled    = text_out.pooler_output          # (batch, hidden_dim)
            feat      = m.text_projection(pooled)       # (batch, 512)
            text_embedding = feat[0].float()            # (512,)

    ln_filter = last_name.strip().lower() if last_name else ""

    results = []
    for item in data:
        # Date filter
        if date and item["date"] != date:
            continue
        # Location filter
        if location and clean_location(item["location"]) != location:
            continue
        # Group filter — only return photos from this trail/time slot
        if group and item.get("group","").strip().lower() != group.lower():
            continue
        # Last name filter — only return photos that belong to this family
        if ln_filter:
            item_ln = item.get("last_name", "").strip().lower()
            if not item_ln or fuzz.partial_ratio(ln_filter, item_ln) < 80:
                continue

        # Score — skip photos with no CLIP embedding during descriptive search
        if text_embedding is not None:
            if item.get("embedding") is None:
                continue
            img_emb = torch.tensor(item["embedding"]).float().reshape(-1)
            t = text_embedding.reshape(-1)
            # Pad / trim so shapes always match (both should be 512)
            if t.shape[0] != img_emb.shape[0]:
                min_dim = min(t.shape[0], img_emb.shape[0])
                t = t[:min_dim]
                img_emb = img_emb[:min_dim]
            t_norm = t.norm()
            i_norm = img_emb.norm()
            if t_norm == 0 or i_norm == 0:
                similarity = 0.0
            else:
                similarity = (t @ img_emb / (t_norm * i_norm)).item()
        else:
            similarity = 0.0

        boost = 0.0
        if ln_filter:
            item_ln = item.get("last_name", "").strip().lower()
            if item_ln:
                boost += (fuzz.partial_ratio(ln_filter, item_ln) / 100) * 0.15

        results.append((similarity + boost, item))

    # When no text query (last name only), sort by filename; otherwise sort by score
    if text_embedding is not None:
        results.sort(reverse=True, key=lambda x: x[0])
    else:
        results.sort(key=lambda x: natural_sort_key(x[1]["path"]))

    photos = []
    for score, item in results:
        photos.append({
            "path":      item["path"],
            "date":      item["date"],
            "location":  clean_location(item["location"]),
            "last_name": item.get("last_name", ""),
            "filename":  os.path.basename(item["path"]),
            "score":     round(score, 4)
        })
    return {"count": len(photos), "photos": photos}


@app.get("/api/pricing")
def get_pricing(location: str = Query(None)):
    default = {"tiers": [
        {"label": "1 Photo",    "count": 1,     "price": 25},
        {"label": "3 Photos",   "count": 3,     "price": 60},
        {"label": "All Photos", "count": "all", "price": 90}
    ]}
    if not os.path.exists("pricing.json"):
        return default
    with open("pricing.json", "r") as f:
        pricing = json.load(f)
    combos = pricing.get("combos", [])
    if location and location in pricing.get("activities", {}):
        result = dict(pricing["activities"][location])
        result["combos"] = combos
        return result
    result = dict(pricing.get("default", default))
    result["combos"] = combos
    return result


def to_r2_key(path: str) -> str:
    # Convert absolute local path to R2 key (relative, starting from 'images/')
    idx = path.find("images/")
    return path[idx:] if idx >= 0 else path

def _thumb_r2_key(r2_key: str) -> str:
    """maps images/foo/bar.jpg → thumbs/foo/bar.jpg"""
    if r2_key.startswith("images/"):
        return "thumbs/" + r2_key[len("images/"):]
    return "thumbs/" + r2_key

@app.get("/api/photo")
def get_photo(path: str, size: str = Query("medium")):
    """
    size=thumb  → 450px  q72  (gallery thumbnails — served from pre-generated R2 thumb when available)
    size=medium → 1800px q90  (lightbox)
    size=full   → 2400px q92  (download-quality)
    """
    from fastapi.responses import StreamingResponse as SR
    SIZE_MAP = {
        "thumb":  (450,  72),
        "medium": (1800, 90),
        "full":   (2400, 92),
    }
    max_px, quality = SIZE_MAP.get(size, SIZE_MAP["medium"])
    cache_secs = 86400 if size == "thumb" else 3600

    try:
        key = to_r2_key(path)
        if os.getenv("R2_ENDPOINT_URL"):
            # For thumbs: try pre-generated version first (much faster — no processing)
            if size == "thumb":
                tkey = _thumb_r2_key(key)
                try:
                    obj = s3.get_object(Bucket=R2_BUCKET, Key=tkey)
                    buf = BytesIO(obj["Body"].read())
                    buf.seek(0)
                    return SR(buf, media_type="image/jpeg",
                              headers={"Cache-Control": f"public, max-age={cache_secs}"})
                except s3.exceptions.NoSuchKey:
                    pass  # fall through to generate on the fly
                except Exception:
                    pass  # fall through to generate on the fly

            obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
            img = Image.open(obj["Body"]).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            img = apply_watermark(img, size)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)

            # Cache the newly generated thumb to R2 for next time
            if size == "thumb":
                try:
                    buf.seek(0)
                    s3.put_object(
                        Bucket=R2_BUCKET,
                        Key=_thumb_r2_key(key),
                        Body=buf.read(),
                        ContentType="image/jpeg",
                        CacheControl="public, max-age=31536000",
                    )
                except Exception:
                    pass  # non-fatal

            buf.seek(0)
            return SR(buf, media_type="image/jpeg",
                      headers={"Cache-Control": f"public, max-age={cache_secs}"})
        elif os.path.exists(path):
            img = Image.open(path).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            img = apply_watermark(img, size)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            buf.seek(0)
            return SR(buf, media_type="image/jpeg")
        else:
            return JSONResponse(status_code=404, content={"error": "File not found"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────
# WOOCOMMERCE CHECKOUT
# ─────────────────────────────────────────

RELOAD_TOKEN = os.getenv("RELOAD_TOKEN", "")

@app.post("/api/reload")
def reload_index(token: str = Query("")):
    global data
    if RELOAD_TOKEN and token != RELOAD_TOKEN:
        return Response(status_code=401)
    try:
        obj  = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
        data = json.loads(obj["Body"].read().decode("utf-8"))
        return {"status": "ok", "count": len(data)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


WC_BASE                  = "https://bigskyphotos.com/wp-json/wc/v3"
WC_KEY                   = os.getenv("WC_CONSUMER_KEY", "")
WC_SECRET                = os.getenv("WC_CONSUMER_SECRET", "")
WC_DIGITAL_PRODUCT_ID   = 1152
WC_PRINT_PRODUCT_ID     = 1156
WC_FRAME_PARENT_ID      = 1034

# Frame variation IDs: (size_idx, orientation) -> variation_id
FRAME_VARIATION_MAP = {
    (0, "landscape"): 1057, (0, "portrait"): 1058,
    (1, "landscape"): 1059, (1, "portrait"): 1060,
    (2, "landscape"): 1061, (2, "portrait"): 1062,
    (3, "landscape"): 1063, (3, "portrait"): 1064,
}

@app.get("/api/frames")
def get_frames():
    resp = http_requests.get(
        f"{WC_BASE}/products/{WC_FRAME_PARENT_ID}/variations",
        params={"per_page": 100},
        auth=(WC_KEY, WC_SECRET),
        timeout=15
    )
    if resp.status_code != 200:
        return JSONResponse(status_code=502, content={"error": "WooCommerce unavailable"})

    # Build lookup by variation ID from WC response
    wc_by_id = {v["id"]: v for v in resp.json()}

    SIZE_LABELS = ["5x7", "8x10", "10x13", "16x20"]
    frames = []
    for size_idx, label in enumerate(SIZE_LABELS):
        entry = {"size": label, "landscape": None, "portrait": None}
        for orient in ("landscape", "portrait"):
            var_id = FRAME_VARIATION_MAP.get((size_idx, orient))
            if var_id is None:
                continue
            wc = wc_by_id.get(var_id, {})
            entry[orient] = {
                "variation_id": var_id,
                "size":         label,
                "orientation":  orient,
                "price":        wc.get("price") or wc.get("regular_price", "0"),
                "stock_status": wc.get("stock_status", "instock"),
            }
        frames.append(entry)
    return {"frames": frames}


@app.get("/api/frame-photos")
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


@app.get("/api/validate-coupon")
def validate_coupon(code: str = Query("")):
    if not code.strip():
        return JSONResponse(status_code=400, content={"valid": False, "error": "No code provided"})
    resp = http_requests.get(
        f"{WC_BASE}/coupons",
        params={"code": code.strip(), "per_page": 1},
        auth=(WC_KEY, WC_SECRET),
        timeout=10
    )
    if not resp.ok:
        return JSONResponse(status_code=502, content={"valid": False, "error": "Could not reach store"})
    coupons = resp.json()
    if not coupons:
        return JSONResponse(status_code=404, content={"valid": False, "error": "Invalid coupon code"})
    coupon = coupons[0]
    # Check expiry
    expiry = coupon.get("date_expires")
    if expiry:
        exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > exp_dt:
            return JSONResponse(status_code=400, content={"valid": False, "error": "This coupon has expired"})
    # Check usage limit
    usage_limit = coupon.get("usage_limit")
    if usage_limit:
        if int(coupon.get("usage_count", 0)) >= int(usage_limit):
            return JSONResponse(status_code=400, content={"valid": False, "error": "Coupon usage limit reached"})
    return {
        "valid":         True,
        "code":          coupon.get("code", code).lower(),
        "discount_type": coupon.get("discount_type", "percent"),
        "amount":        float(coupon.get("amount", 0)),
        "description":   coupon.get("description", ""),
    }


@app.post("/api/checkout")
async def create_checkout(request: Request):
    try:
        body = await request.json()

        digital_albums = body.get("digital_albums", [])
        # Fallback for legacy payloads
        digital_count = body.get("digital_count", sum(a.get("count", 0) for a in digital_albums))
        digital_price = body.get("digital_price", sum(a.get("price", 0) for a in digital_albums))
        filenames     = body.get("digital_filenames", [])
        prints        = body.get("prints", [])
        frames        = body.get("frames", [])
        location      = body.get("location", "")
        date          = body.get("date", "")
        last_name        = body.get("last_name", "").strip()
        coupon_code      = body.get("coupon_code", "").strip()
        coupon_discount  = float(body.get("coupon_discount", 0))

        line_items = []
        fee_lines  = []

        # ── Pre-calculate combo price adjustments ────────────────────────────
        # Instead of a negative discount fee line, each combo album gets its
        # share of the combo price (e.g. $95 / 2 locations = $47.50 each).
        _combos_cfg = []
        if os.path.exists("pricing.json"):
            with open("pricing.json") as _pf:
                _combos_cfg = json.load(_pf).get("combos", [])
        _combo_adj = {}  # index in digital_albums -> adjusted price
        for _combo in _combos_cfg:
            _clocs    = set(_combo.get("locations", []))
            _min_each = _combo.get("min_each", 1)
            _lc       = {}
            _idxs     = []
            for _i, _album in enumerate(digital_albums):
                _loc = _album.get("location", "")
                if _loc in _clocs:
                    _lc[_loc] = _lc.get(_loc, 0) + int(_album.get("count", 0))
                    _idxs.append(_i)
            if _idxs and all(_lc.get(l, 0) >= _min_each for l in _clocs):
                _combo_price = float(_combo.get("price", 0))
                _share       = round(_combo_price / len(_idxs), 2)
                for _j, _idx in enumerate(_idxs):
                    # Last album absorbs any rounding remainder
                    _combo_adj[_idx] = (
                        round(_combo_price - _share * (len(_idxs) - 1), 2)
                        if _j == len(_idxs) - 1 else _share
                    )
                break  # Only one active combo at a time

        if digital_albums:
            # One line item per album (adjusted price for combo albums)
            for _i, album in enumerate(digital_albums):
                count = int(album.get("count", 0))
                if count == 0:
                    continue
                album_price = str(_combo_adj.get(_i, float(album.get("price", 0))))
                album_name  = album.get("last_name") or album.get("location") or "Photos"
                line_items.append({
                    "product_id": WC_DIGITAL_PRODUCT_ID,
                    "quantity":   count,
                    "name":       f"Digital Photos — {album_name}",
                    "subtotal":   album_price,
                    "total":      album_price,
                    "meta_data":  [{"key": "Photos", "value": f"{count} digital photo{'s' if count != 1 else ''}"}],
                })
        elif digital_count > 0:
            # Legacy fallback: single combined line item
            price_str    = str(float(digital_price))
            display_name = last_name if last_name else location
            line_items.append({
                "product_id": WC_DIGITAL_PRODUCT_ID,
                "quantity":   digital_count,
                "name":       f"Digital Photos — {display_name}",
                "subtotal":   price_str,
                "total":      price_str,
                "meta_data":  [{"key": "Photos", "value": f"{digital_count} digital photo{'s' if digital_count != 1 else ''}"}],
            })

        for p in prints:
            print_price = str(float(p["price"]))
            line_items.append({
                "product_id": WC_PRINT_PRODUCT_ID,
                "quantity":   1,
                "name":       f"{p['size']} Print — {p['filename']}",
                "subtotal":   print_price,
                "total":      print_price,
                "meta_data":  [],
            })
            if p.get("frame") and p["frame"] != "No Frame":
                size_idx    = int(p.get("size_idx", 0))
                orientation = p.get("orientation", "landscape")
                var_id      = FRAME_VARIATION_MAP.get((size_idx, orientation), 1057)
                frame_price = str(float(p["frame_price"]))
                line_items.append({
                    "product_id":   WC_FRAME_PARENT_ID,
                    "variation_id": var_id,
                    "quantity":     1,
                    "name":         f"  └ Frame: {p['frame']}",
                    "subtotal":     frame_price,
                    "total":        frame_price,
                })

        for fr in frames:
            var_id    = int(fr["variation_id"])
            qty       = int(fr.get("quantity", 1))
            fr_price  = float(fr["price"]) * qty
            line_items.append({
                "product_id":   WC_FRAME_PARENT_ID,
                "variation_id": var_id,
                "quantity":     qty,
                "name":         f"Frame — {fr['size']} ({fr['orientation'].capitalize()})",
                "subtotal":     str(fr_price),
                "total":        str(fr_price),
            })

        # ── Shipping logic (mirrors frontend calcShipping) ───────────────────
        PRINT_SHIP = [8, 8, 13, 13]
        FRAME_SHIP = [15, 15, 25, 50]

        framed   = [p for p in prints if p.get("frame") and p["frame"] != "No Frame"]
        unframed = [p for p in prints if not p.get("frame") or p["frame"] == "No Frame"]

        # Collect all frame shipping rates: framed prints + standalone frames (expanded by qty)
        frame_rates = (
            [FRAME_SHIP[int(p.get("size_idx", 0))] for p in framed] +
            [FRAME_SHIP[int(fr.get("sizeIdx", 0))]
             for fr in frames for _ in range(int(fr.get("quantity", 1)))]
        )

        FREE_SHIP_PRINT_THRESHOLD = 3

        if frame_rates:
            frame_rates.sort(reverse=True)
            total_ship = frame_rates[0] + sum(r * 0.5 for r in frame_rates[1:])
            fee_lines.append({"name": "Shipping", "total": str(round(total_ship, 2))})
        elif unframed:
            # Free shipping when 3+ unframed prints and no framed prints
            if len(unframed) >= FREE_SHIP_PRINT_THRESHOLD and not frame_rates:
                pass  # free shipping — no fee added
            else:
                max_rate = max(PRINT_SHIP[int(p.get("size_idx", 0))] for p in unframed)
                fee_lines.append({"name": "Shipping", "total": str(max_rate)})

        # ── Coupon discount as negative fee line ─────────────────────────────
        if coupon_code and coupon_discount > 0:
            fee_lines.append({
                "name":  f"Coupon ({coupon_code.upper()})",
                "total": str(-round(coupon_discount, 2)),
            })

        # (Combo pricing is handled above by adjusting per-album line item prices)

        paths = body.get("digital_paths", [])
        meta = [
            {"key": "_photo_location", "value": location},
            {"key": "_photo_date",     "value": date},
            {"key": "_photo_files",    "value": ", ".join(filenames)},
            {"key": "_photo_paths",    "value": "|".join(paths)},
        ]

        customer_email = body.get("email", "")
        billing_data   = body.get("billing", {})
        shipping_data  = body.get("shipping", {})

        # Ensure email is always set in billing
        if billing_data:
            billing_data["email"] = customer_email
        else:
            billing_data = {"email": customer_email}

        order_data = {
            "status":     "pending",
            "line_items": line_items,
            "fee_lines":  fee_lines,
            "meta_data":  meta,
            "billing":    billing_data,
            "shipping":   shipping_data,
        }
        resp = http_requests.post(
            f"{WC_BASE}/orders",
            json=order_data,
            auth=(WC_KEY, WC_SECRET),
            timeout=15
        )

        if resp.status_code not in (200, 201):
            return JSONResponse(status_code=500,
                                content={"error": f"WooCommerce error: {resp.text}"})

        order = resp.json()
        pay_url = (f"https://bigskyphotos.com/checkout/order-pay/{order['id']}/"
                   f"?pay_for_order=true&key={order['order_key']}")
        return {"checkout_url": pay_url}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────
# WEBHOOK + DIGITAL DELIVERY
# ─────────────────────────────────────────

WC_WEBHOOK_SECRET  = os.getenv("WC_WEBHOOK_SECRET", "")
DOWNLOAD_EXPIRE_DAYS = 30
SITE_URL = "https://photos.bigskyphotos.com"

def _store_token(token: str, payload: dict):
    body = json.dumps(payload).encode()
    s3.put_object(Bucket=R2_BUCKET, Key=f"downloads/{token}.json",
                  Body=body, ContentType="application/json")

def _load_token(token: str):
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"downloads/{token}.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None

@app.post("/webhook")
async def wc_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-WC-Webhook-Signature", "")

    # Verify HMAC-SHA256 signature
    expected = base64.b64encode(
        hmac.new(WC_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    if WC_WEBHOOK_SECRET and not hmac.compare_digest(expected, sig):
        return Response(status_code=401)

    try:
        order = json.loads(body)
    except Exception:
        return Response(status_code=400)

    # Act on processing (manual/physical orders) or completed (auto-completed digital orders)
    if order.get("status") not in ("processing", "completed"):
        return Response(status_code=200)

    # Pull stored photo paths from order meta
    meta = {m["key"]: m["value"] for m in order.get("meta_data", [])}
    paths_raw = meta.get("_photo_paths", "")
    if not paths_raw:
        return Response(status_code=200)

    photo_paths = [p for p in paths_raw.split("|") if p]
    location    = meta.get("_photo_location", "")
    date        = meta.get("_photo_date", "")
    filenames   = meta.get("_photo_files", "")

    billing     = order.get("billing", {})
    customer_email = billing.get("email", "")
    customer_name  = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
    order_id    = order.get("id")
    order_key   = order.get("order_key", "")

    # Generate download token
    token = str(uuid.uuid4()).replace("-", "")
    expires = (datetime.now(timezone.utc) + timedelta(days=DOWNLOAD_EXPIRE_DAYS)).isoformat()

    _store_token(token, {
        "order_id":   order_id,
        "order_key":  order_key,
        "customer":   customer_name,
        "email":      customer_email,
        "paths":      photo_paths,
        "filenames":  filenames,
        "location":   location,
        "date":       date,
        "expires":    expires,
    })

    download_url = f"{SITE_URL}/download/{token}"

    # Send customer note via WooCommerce (triggers email to customer)
    note_text = (
        f"Your photos are ready to download! Click the link below to access your download page:\n\n"
        f"{download_url}\n\n"
        f"Love your photos? Here's 50% off on any unframed print order! Use code PRINTS50.\n\n"
        f"Your download link is valid for {DOWNLOAD_EXPIRE_DAYS} days. "
        f"If it expires, just contact us at info@bigskyphotos.com and we'll resend your photos anytime."
    )
    http_requests.post(
        f"{WC_BASE}/orders/{order_id}/notes",
        json={"note": note_text, "customer_note": True},
        auth=(WC_KEY, WC_SECRET),
        timeout=10
    )

    return Response(status_code=200)


@app.get("/download/{token}")
def download_page(token: str):
    rec = _load_token(token)
    if not rec:
        return HTMLResponse("<h2>Invalid or expired download link.</h2>", status_code=404)

    expires_dt = datetime.fromisoformat(rec["expires"])
    if datetime.now(timezone.utc) > expires_dt:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c0392b'>This download link has expired.</h2>"
            "<p style='font-family:sans-serif'>Please contact us at bigskyphotos.com to receive a new link.</p>",
            status_code=410
        )

    expire_str = expires_dt.strftime("%B %d, %Y")
    photo_rows = ""
    for i, path in enumerate(rec["paths"]):
        fname = os.path.basename(path)
        photo_rows += f"""
        <div style="display:flex;align-items:center;justify-content:space-between;
                    padding:0.65rem 0;border-bottom:1px solid rgba(255,255,255,0.08);">
          <span style="font-size:0.9rem;color:rgba(255,255,255,0.8)">{fname}</span>
          <a href="/download/{token}/{i}" download="{fname}"
             style="background:#F2C94C;color:#0d1f2d;padding:0.4rem 1rem;border-radius:6px;
                    font-weight:700;font-size:0.85rem;text-decoration:none;">
            Download
          </a>
        </div>"""

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Your Photos — Crystal Images</title>
<style>
  body{{margin:0;background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh;padding:2rem 1rem;box-sizing:border-box}}
  .wrap{{max-width:680px;margin:0 auto}}
  h1{{color:#F2C94C;margin-bottom:0.25rem}}
  .sub{{color:rgba(255,255,255,0.5);margin-bottom:2rem;font-size:0.9rem}}
  .card{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:1.5rem;margin-bottom:1.5rem}}
  .license-title{{font-weight:700;color:#F2C94C;margin-bottom:0.75rem;font-size:1rem}}
  .license-item{{display:flex;gap:0.5rem;margin-bottom:0.4rem;font-size:0.88rem;color:rgba(255,255,255,0.75)}}
  .expire{{font-size:0.82rem;color:rgba(255,255,255,0.4);margin-top:1rem}}
</style></head><body><div class="wrap">
  <h1>Your Photos Are Ready</h1>
  <div class="sub">Order #{rec['order_id']} &nbsp;·&nbsp; {rec['location']} &nbsp;·&nbsp; {rec['date']}</div>

  <div class="card">
    <div class="license-title">📋 Digital Photo License</div>
    <div class="license-item"><span>✓</span><span>Personal use permitted — print, frame, and share freely.</span></div>
    <div class="license-item"><span>✓</span><span>Commercial use permitted <strong>when you tag @crystalimagesbigsky on Instagram.</strong></span></div>
    <div class="license-item"><span>✗</span><span>No resale or redistribution of the original image files.</span></div>
    <div class="license-item"><span>✗</span><span>Do not sell or transfer these files to third parties.</span></div>
    <div class="license-item"><span>📸</span><span>We love seeing your memories — tag us when you share!</span></div>
    <div style="margin-top:0.75rem;font-size:0.8rem;color:rgba(255,255,255,0.4)">
      By downloading you agree to these terms.
      Links expire <strong style="color:rgba(255,255,255,0.6)">{expire_str}</strong> —
      contact us anytime at info@bigskyphotos.com to resend.
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;flex-wrap:wrap;gap:0.75rem;">
      <div class="license-title" style="margin-bottom:0;">⬇ Download Your Photos ({len(rec['paths'])} files)</div>
      <a href="/download/{token}/all" download="crystal-images-order-{rec['order_id']}.zip"
         style="background:#F2C94C;color:#0d1f2d;padding:0.5rem 1.25rem;border-radius:6px;
                font-weight:700;font-size:0.9rem;text-decoration:none;white-space:nowrap;">
        ⬇ Download All
      </a>
    </div>
    {photo_rows}
  </div>
</div></body></html>"""
    return HTMLResponse(html)


@app.get("/download/{token}/all")
def download_all(token: str):
    import zipfile
    rec = _load_token(token)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Invalid link"})
    expires_dt = datetime.fromisoformat(rec["expires"])
    if datetime.now(timezone.utc) > expires_dt:
        return JSONResponse(status_code=410, content={"error": "Link expired"})

    paths = rec["paths"]
    filename = f"crystal-images-order-{rec['order_id']}.zip"

    def iter_zip():
        buf = BytesIO()
        last_pos = 0
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True)
        for path in paths:
            key = to_r2_key(path)
            try:
                obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
                data = obj["Body"].read()
                zf.writestr(os.path.basename(path), data)
                # Yield only the new bytes written since last flush
                cur_pos = buf.tell()
                buf.seek(last_pos)
                chunk = buf.read(cur_pos - last_pos)
                last_pos = cur_pos
                if chunk:
                    yield chunk
            except Exception as e:
                print(f"  zip: skipped {key}: {e}")
        # Close writes the central directory — yield those final bytes
        zf.close()
        buf.seek(last_pos)
        remainder = buf.read()
        if remainder:
            yield remainder

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/download/{token}/{idx}")
def download_file(token: str, idx: int):
    rec = _load_token(token)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Invalid link"})

    expires_dt = datetime.fromisoformat(rec["expires"])
    if datetime.now(timezone.utc) > expires_dt:
        return JSONResponse(status_code=410, content={"error": "Link expired"})

    if idx < 0 or idx >= len(rec["paths"]):
        return JSONResponse(status_code=404, content={"error": "File not found"})

    key = to_r2_key(rec["paths"][idx])
    presigned = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key,
                "ResponseContentDisposition": f"attachment; filename={os.path.basename(key)}"},
        ExpiresIn=3600
    )
    return RedirectResponse(presigned)


# ─────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────

ADMIN_PASSWORD  = os.getenv("ADMIN_PASSWORD", "")
ADMIN_COOKIE    = "ci_admin_session"
ADMIN_SECRET    = os.getenv("ADMIN_PASSWORD", "fallback-secret")  # signs cookie
STRIPE_RATE     = 0.029
STRIPE_FIXED    = 0.30

def _make_session_token():
    raw = f"{ADMIN_SECRET}:{datetime.now(timezone.utc).date()}"
    return hmac.new(ADMIN_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()

def _admin_authed(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE, "")
    expected = _make_session_token()
    return hmac.compare_digest(token, expected)

ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Admin Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0c2336;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#0a1e2e;border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:2.5rem 2rem;width:100%;max-width:360px}}
h2{{color:#F5C518;font-size:1.3rem;margin-bottom:1.5rem;text-align:center}}
input{{width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.15);border-radius:8px;
  padding:.75rem 1rem;color:#fff;font-size:1rem;margin-bottom:1rem;outline:none}}
input:focus{{border-color:#F5C518}}
button{{width:100%;background:#F5C518;color:#0c2336;border:none;border-radius:8px;
  padding:.75rem;font-size:1rem;font-weight:700;cursor:pointer}}
.err{{color:#e53e3e;font-size:.85rem;text-align:center;margin-top:.5rem}}
</style></head><body>
<div class="card">
  <h2>Crystal Images Admin</h2>
  <form method="post" action="/admin/login">
    <input type="password" name="password" placeholder="Password" autofocus />
    <button type="submit">Sign In</button>
    {error}
  </form>
</div></body></html>"""

@app.get("/admin", response_class=HTMLResponse)
def admin_get(request: Request):
    if not _admin_authed(request):
        return HTMLResponse(ADMIN_LOGIN_HTML.format(error=""))
    return RedirectResponse("/admin/dashboard")

@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    pw   = form.get("password", "")
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw, ADMIN_PASSWORD):
        return HTMLResponse(ADMIN_LOGIN_HTML.format(
            error='<p class="err">Incorrect password.</p>'), status_code=401)
    token = _make_session_token()
    resp  = RedirectResponse("/admin/dashboard", status_code=303)
    resp.set_cookie(ADMIN_COOKIE, token, httponly=True, samesite="strict", max_age=86400)
    return resp

@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin")
    resp.delete_cookie(ADMIN_COOKIE)
    return resp

def _fetch_wc_orders(after: str = None, before: str = None, per_page: int = 100):
    params = {"status": "completed", "per_page": per_page, "orderby": "date", "order": "desc"}
    if after:  params["after"]  = after
    if before: params["before"] = before
    try:
        r = http_requests.get(f"{WC_BASE}/orders", params=params,
                              auth=(WC_KEY, WC_SECRET), timeout=15)
        return r.json() if r.ok else []
    except Exception:
        return []

def _list_tokens():
    """List all download tokens from R2."""
    tokens = {}
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="downloads/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]  # downloads/{token}.json
                token = key.replace("downloads/", "").replace(".json", "")
                tokens[token] = key
    except Exception:
        pass
    return tokens

def _get_token_by_order_id(order_id):
    """Find download token matching a WooCommerce order ID."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="downloads/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                token = key.replace("downloads/", "").replace(".json", "")
                rec = _load_token(token)
                if rec and str(rec.get("order_id")) == str(order_id):
                    return token, rec
    except Exception:
        pass
    return None, None

def _stripe_fee(total: float) -> float:
    return round(total * STRIPE_RATE + STRIPE_FIXED, 2)

def _order_row(order) -> dict:
    billing    = order.get("billing", {})
    total      = float(order.get("total", 0))
    discount   = float(order.get("discount_total", 0))
    stripe_fee = _stripe_fee(total)
    fee_lines  = order.get("fee_lines", [])
    shipping   = sum(float(f.get("total", 0)) for f in fee_lines if "ship" in f.get("name","").lower())
    ship_addr  = ""
    sa         = order.get("shipping", {})
    if sa.get("address_1"):
        parts = [sa.get("address_1",""), sa.get("address_2",""), sa.get("city",""),
                 sa.get("state",""), sa.get("postcode",""), sa.get("country","")]
        ship_addr = ", ".join(p for p in parts if p)
    meta         = {m["key"]: m["value"] for m in order.get("meta_data", [])}
    filenames    = meta.get("_photo_files", "")
    location     = meta.get("_photo_location", "")
    date_raw     = meta.get("_photo_date", "")
    line_items   = order.get("line_items", [])
    what_ordered = ", ".join(li.get("name","") for li in line_items)
    coupon_lines = order.get("coupon_lines", [])
    coupon_code  = ", ".join(c.get("code","") for c in coupon_lines) if coupon_lines else ""
    return {
        "order_id":    order.get("id"),
        "date":        order.get("date_created","")[:10],
        "first":       billing.get("first_name",""),
        "last":        billing.get("last_name",""),
        "email":       billing.get("email",""),
        "what":        what_ordered,
        "filenames":   filenames,
        "location":    location,
        "photo_date":  date_raw,
        "total":       total,
        "discount":    discount,
        "stripe_fee":  stripe_fee,
        "net":         round(total - stripe_fee - discount, 2),
        "shipping":    shipping,
        "ship_addr":   ship_addr,
        "coupon_code": coupon_code,
    }

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, days: int = 30,
                    date_from: str = "", date_to: str = ""):
    if not _admin_authed(request):
        return RedirectResponse("/admin")
    custom = bool(date_from and date_to)
    if custom:
        after  = f"{date_from}T00:00:00"
        before = f"{date_to}T23:59:59"
        orders = _fetch_wc_orders(after=after, before=before)
    else:
        after  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = _fetch_wc_orders(after=after)
    rows   = [_order_row(o) for o in orders]
    total_rev  = sum(r["total"]      for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    total_net  = sum(r["net"]        for r in rows)

    import json as _json
    def td(v, cls=""): return f'<td class="{cls}">{v}</td>'
    trs = ""
    for r in rows:
        first_file = (r['filenames'] or '').split(',')[0].strip() or '—'
        detail_data = _json.dumps({
            "name":     f"{r['first']} {r['last']}",
            "email":    r['email'],
            "what":     r['what'],
            "files":    r['filenames'] or '—',
            "total":    f"${r['total']:.2f}",
            "discount": f"-${r['discount']:.2f}" if r['discount'] else '—',
            "fee":      f"${r['stripe_fee']:.2f}",
            "net":      f"${r['net']:.2f}",
            "coupon":   r['coupon_code'] or '—',
            "shipping": f"${r['shipping']:.2f}" if r['shipping'] else '—',
            "address":  r['ship_addr'] or '—',
        }).replace("'", "&#39;").replace('"', '&quot;')
        trs += f"""<tr class="order-row" onclick="showDetail('{detail_data}')" style="cursor:pointer">
          {td(r['date'])}
          {td(f"{r['first']} {r['last']}")}
          {td(r['email'])}
          {td(r['what'])}
          {td(first_file, 'mono')}
          {td(f"${r['total']:.2f}")}
          {td(f"-${r['discount']:.2f}" if r['discount'] else '—', 'fee')}
          {td(f"${r['stripe_fee']:.2f}", 'fee')}
          {td(f"${r['net']:.2f}", 'net')}
          {td(r['coupon_code'] or '—')}
          {td(f"${r['shipping']:.2f}" if r['shipping'] else '—')}
          {td(r['ship_addr'] or '—')}
          {td(f'<a href="/admin/regen/{r["order_id"]}" class="regen-btn" onclick="event.stopPropagation()">↺ Regen Link</a>')}
        </tr>"""

    period_opts = "".join(
        f'<option value="{d}" {"selected" if d==days else ""}>{label}</option>'
        for d, label in [(7,"Last 7 days"),(14,"Last 14 days"),(30,"Last 30 days"),(60,"Last 60 days"),(90,"Last 90 days")]
    )
    export_qs = f"date_from={date_from}&date_to={date_to}" if custom else f"days={days}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Admin — Crystal Images</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0c2336;font-family:'Segoe UI',sans-serif;color:#fff;min-height:100vh}}
.topbar{{background:#0a1e2e;border-bottom:1px solid rgba(255,255,255,.08);padding:.9rem 2rem;
  display:flex;align-items:center;gap:1.25rem}}
.topbar h1{{color:#F5C518;font-size:1.1rem;font-weight:700;margin-right:auto}}
.topbar a{{color:rgba(255,255,255,.4);font-size:.85rem;text-decoration:none;white-space:nowrap}}
.topbar a:hover{{color:#fff}}
.container{{padding:1.5rem 2rem}}
.stats{{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}}
.stat{{background:#0a1e2e;border:1px solid rgba(255,255,255,.08);border-radius:12px;
  padding:1rem 1.5rem;flex:1;min-width:150px}}
.stat .label{{font-size:.72rem;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
.stat .val{{font-size:1.4rem;font-weight:700;color:#F5C518}}
.controls{{display:flex;gap:1rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap}}
select{{background:#0a1e2e;border:1px solid rgba(255,255,255,.15);color:#fff;
  padding:.5rem .9rem;border-radius:8px;font-size:.9rem;cursor:pointer}}
.export-btn{{background:#F5C518;color:#0c2336;border:none;border-radius:8px;
  padding:.5rem 1.2rem;font-weight:700;cursor:pointer;font-size:.9rem;text-decoration:none}}
.wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.82rem;min-width:1100px}}
th{{background:#0a1e2e;color:rgba(255,255,255,.5);font-weight:600;font-size:.72rem;
  letter-spacing:.8px;text-transform:uppercase;padding:.6rem .75rem;text-align:left;
  border-bottom:1px solid rgba(255,255,255,.08);white-space:nowrap}}
td{{padding:.65rem .75rem;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:top}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.mono{{font-family:monospace;font-size:.75rem;color:rgba(255,255,255,.55);max-width:220px;
  word-break:break-all;white-space:normal}}
.fee{{color:#e53e3e}}
.net{{color:#4ade80;font-weight:600}}
.regen-btn{{background:rgba(245,197,24,.12);border:1px solid rgba(245,197,24,.3);
  color:#F5C518;padding:.3rem .8rem;border-radius:6px;font-size:.78rem;
  text-decoration:none;white-space:nowrap}}
.regen-btn:hover{{background:rgba(245,197,24,.25)}}
.empty{{text-align:center;padding:3rem;color:rgba(255,255,255,.3)}}
.order-row:hover td{{background:rgba(255,255,255,.04);cursor:pointer}}
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;align-items:center;justify-content:center}}
.modal-overlay.open{{display:flex}}
.modal{{background:#0a1e2e;border:1px solid rgba(255,255,255,.12);border-radius:14px;padding:1.5rem;max-width:560px;width:90%;max-height:80vh;overflow-y:auto}}
.modal h2{{color:#F5C518;font-size:1rem;margin-bottom:1rem}}
.modal-row{{display:flex;gap:1rem;padding:.5rem 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:.85rem}}
.modal-row:last-child{{border-bottom:none}}
.modal-label{{color:rgba(255,255,255,.4);min-width:110px;flex-shrink:0}}
.modal-val{{color:#fff;word-break:break-word}}
.modal-close{{float:right;background:none;border:none;color:rgba(255,255,255,.4);font-size:1.2rem;cursor:pointer;margin-top:-4px}}
.modal-close:hover{{color:#fff}}
</style></head><body>
<div class="modal-overlay" id="detail-modal" onclick="closeDetail(event)">
  <div class="modal">
    <h2>Order Details <button class="modal-close" onclick="document.getElementById('detail-modal').classList.remove('open')">✕</button></h2>
    <div id="detail-body"></div>
  </div>
</div>
<script>
function showDetail(encoded) {{
  const d = JSON.parse(encoded.replace(/&quot;/g,'"').replace(/&#39;/g,"'"));
  const fields = [
    ['Name', d.name], ['Email', d.email], ['Ordered', d.what],
    ['Total', d.total], ['Discount', d.discount], ['Stripe Fee', d.fee],
    ['Net', d.net], ['Coupon', d.coupon], ['Shipping', d.shipping],
    ['Billing Address', d.address], ['Photo Files', d.files],
  ];
  document.getElementById('detail-body').innerHTML = fields.map(([l,v]) =>
    `<div class="modal-row"><span class="modal-label">${{l}}</span><span class="modal-val">${{v}}</span></div>`
  ).join('');
  document.getElementById('detail-modal').classList.add('open');
}}
function closeDetail(e) {{
  if (e.target === document.getElementById('detail-modal'))
    document.getElementById('detail-modal').classList.remove('open');
}}
</script>
<div class="topbar">
  <h1>Crystal Images — Admin</h1>
  <a href="/admin/cull">📷 Cull</a>
  <a href="/admin/trash">🗑 Trash</a>
  <a href="/admin/photographers">👥 Photographers</a>
  <a href="/admin/logout">Sign out</a>
</div>
<div class="container">
  <div class="stats">
    <div class="stat"><div class="label">Orders</div><div class="val">{len(rows)}</div></div>
    <div class="stat"><div class="label">Revenue</div><div class="val">${total_rev:.2f}</div></div>
    <div class="stat"><div class="label">Stripe Fees</div><div class="val" style="color:#e53e3e">${total_fees:.2f}</div></div>
    <div class="stat"><div class="label">Net</div><div class="val" style="color:#4ade80">${total_net:.2f}</div></div>
  </div>
  <div class="controls">
    <form method="get" style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;">
      <select name="days" onchange="this.form.submit();document.getElementById('custom').style.display='none'">
        {period_opts}
      </select>
      <span style="color:rgba(255,255,255,.3);font-size:.85rem;">or</span>
      <input type="date" name="date_from" value="{date_from}"
        style="background:#0a1e2e;border:1px solid rgba(255,255,255,.15);color:#fff;padding:.45rem .75rem;border-radius:8px;font-size:.85rem;"
        placeholder="From" />
      <input type="date" name="date_to" value="{date_to}"
        style="background:#0a1e2e;border:1px solid rgba(255,255,255,.15);color:#fff;padding:.45rem .75rem;border-radius:8px;font-size:.85rem;"
        placeholder="To" />
      <button type="submit" style="background:rgba(245,197,24,.15);border:1px solid rgba(245,197,24,.4);color:#F5C518;padding:.45rem 1rem;border-radius:8px;cursor:pointer;font-size:.85rem;">Apply</button>
    </form>
    <a class="export-btn" href="/admin/export?{export_qs}">↓ Export CSV</a>
  </div>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Date</th><th>Name</th><th>Email</th><th>Ordered</th>
      <th>Photo Files</th><th>Total</th><th>Discount</th><th>Stripe Fee</th><th>Net</th>
      <th>Coupon Code</th><th>Shipping $</th><th>Billing Address</th><th>Action</th>
    </tr></thead>
    <tbody>{trs if trs else '<tr><td colspan="11" class="empty">No completed orders in this period.</td></tr>'}</tbody>
  </table>
  </div>
</div></body></html>"""
    return HTMLResponse(html)

@app.get("/admin/export")
def admin_export(request: Request, days: int = 30,
                 date_from: str = "", date_to: str = ""):
    if not _admin_authed(request):
        return RedirectResponse("/admin")
    import csv, io
    if date_from and date_to:
        orders = _fetch_wc_orders(after=f"{date_from}T00:00:00", before=f"{date_to}T23:59:59")
    else:
        after  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = _fetch_wc_orders(after=after)
    rows   = [_order_row(o) for o in orders]
    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date","First Name","Last Name","Email","What Ordered",
                     "Photo Files","Location","Photo Date","Total","Discount",
                     "Stripe Fee","Net","Coupon Code","Shipping Cost","Shipping Address"])
    for r in rows:
        writer.writerow([r["date"], r["first"], r["last"], r["email"], r["what"],
                         r["filenames"], r["location"], r["photo_date"],
                         f"${r['total']:.2f}",
                         f"-${r['discount']:.2f}" if r["discount"] else "",
                         f"${r['stripe_fee']:.2f}", f"${r['net']:.2f}",
                         r["coupon_code"],
                         f"${r['shipping']:.2f}" if r["shipping"] else "",
                         r["ship_addr"]])
    filename = f"crystal-images-orders-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.get("/admin/regen/{order_id}")
def admin_regen(request: Request, order_id: int):
    if not _admin_authed(request):
        return RedirectResponse("/admin")
    token, rec = _get_token_by_order_id(order_id)
    if not rec:
        return HTMLResponse("<h2 style='font-family:sans-serif;color:#e53e3e;padding:2rem'>Order not found in download records.</h2>")
    new_token  = str(uuid.uuid4()).replace("-", "")
    new_expires = (datetime.now(timezone.utc) + timedelta(days=DOWNLOAD_EXPIRE_DAYS)).isoformat()
    rec["expires"] = new_expires
    _store_token(new_token, rec)
    new_url = f"{SITE_URL}/download/{new_token}"
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Link Regenerated</title>
<style>body{{background:#0c2336;font-family:'Segoe UI',sans-serif;color:#fff;padding:2rem;}}
.card{{background:#0a1e2e;border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:2rem;max-width:560px;}}
h2{{color:#4ade80;margin-bottom:1rem}}p{{color:rgba(255,255,255,.6);font-size:.9rem;margin-bottom:.75rem}}
.url{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:8px;
  padding:.75rem 1rem;font-family:monospace;font-size:.85rem;word-break:break-all;color:#F5C518;margin-bottom:1.25rem}}
a.back{{color:#F5C518;font-size:.9rem}}</style></head><body>
<div class="card">
  <h2>✓ Download Link Regenerated</h2>
  <p>Customer: <strong>{rec.get('customer','')} ({rec.get('email','')})</strong></p>
  <p>New link (valid 30 days):</p>
  <div class="url">{new_url}</div>
  <p>Copy and send this to the customer.</p>
  <a class="back" href="/admin/dashboard">← Back to dashboard</a>
</div></body></html>"""
    return HTMLResponse(html)


# ─────────────────────────────────────────
# PHOTO MANAGEMENT — HELPERS & DATA
# ─────────────────────────────────────────

LOCATIONS_LIST = [
    "Lone Peak Portraits",
    "Explorer Gondola",
    "Ramcharger Portraits",
    "Mountain Biking",
    "Adventure Zip Line",
]

def _r2_json_load(key, default=None):
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default if default is not None else []

def _r2_json_save(key, payload):
    s3.put_object(
        Bucket=R2_BUCKET, Key=key,
        Body=json.dumps(payload, default=str).encode(),
        ContentType="application/json",
    )

def _load_photographers():  return _r2_json_load("meta/photographers.json", [])
def _save_photographers(d): _r2_json_save("meta/photographers.json", d)
def _load_pending_meta():   return _r2_json_load("meta/pending_meta.json", [])
def _save_pending_meta(d):  _r2_json_save("meta/pending_meta.json", d)
def _load_clock_records():  return _r2_json_load("meta/clock_records.json", [])
def _save_clock_records(d): _r2_json_save("meta/clock_records.json", d)
def _load_trash_meta():     return _r2_json_load("meta/trash_meta.json", [])
def _save_trash_meta(d):    _r2_json_save("meta/trash_meta.json", d)

def _auth_photographer(pin: str):
    for p in _load_photographers():
        if p.get("pin") == pin:
            return p
    return None

def _presigned_get(key: str, expires: int = 3600) -> str:
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires,
        )
    except Exception:
        return ""

# ─────────────────────────────────────────
# PHOTO MANAGEMENT — API
# ─────────────────────────────────────────

# ── Photographer auth ──
@app.post("/api/photographer/auth")
async def photographer_auth(request: Request):
    body = await request.json()
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    records = _load_clock_records()
    active = next((r for r in reversed(records)
                   if r["photographer_id"] == p["id"] and not r.get("clock_out")), None)
    return {"photographer": {k: v for k, v in p.items() if k != "pin"},
            "clocked_in": active is not None, "active_record": active}

# ── Clock in / out ──
@app.post("/api/clock/in")
async def clock_in(request: Request):
    body = await request.json()
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    records = _load_clock_records()
    if any(r["photographer_id"] == p["id"] and not r.get("clock_out") for r in records):
        return JSONResponse(status_code=400, content={"error": "Already clocked in"})
    rec = {
        "id": str(uuid.uuid4()),
        "photographer_id": p["id"],
        "photographer_name": p["name"],
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "location": body.get("location", ""),
        "clock_in": datetime.now(timezone.utc).isoformat(),
        "clock_out": None,
    }
    records.append(rec)
    _save_clock_records(records)
    return {"record": rec}

@app.post("/api/clock/out")
async def clock_out(request: Request):
    body = await request.json()
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    records = _load_clock_records()
    for r in reversed(records):
        if r["photographer_id"] == p["id"] and not r.get("clock_out"):
            r["clock_out"] = datetime.now(timezone.utc).isoformat()
            _save_clock_records(records)
            ci = datetime.fromisoformat(r["clock_in"])
            co = datetime.fromisoformat(r["clock_out"])
            hours = round((co - ci).total_seconds() / 3600, 2)
            return {"record": r, "hours": hours}
    return JSONResponse(status_code=400, content={"error": "Not clocked in"})

@app.get("/api/clock/status")
def clock_status_ep(pin: str = Query(...)):
    p = _auth_photographer(pin)
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    records = _load_clock_records()
    active = next((r for r in reversed(records)
                   if r["photographer_id"] == p["id"] and not r.get("clock_out")), None)
    return {"clocked_in": active is not None, "record": active,
            "photographer": {k: v for k, v in p.items() if k != "pin"}}

# ── Upload ──
@app.post("/api/upload/presign")
async def upload_presign(request: Request):
    body = await request.json()
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    date     = body.get("date", datetime.now().strftime("%Y-%m-%d"))
    location = body.get("location", "")
    files    = body.get("files", [])
    if not location or not files:
        return JSONResponse(status_code=400, content={"error": "Missing location or files"})

    loc_slug     = location.lower().replace(" ", "-")
    pending_meta = _load_pending_meta()
    existing     = {m["key"] for m in pending_meta}
    new_meta     = []
    urls         = []

    for f in files:
        filename = os.path.basename(f["name"])
        key      = f"pending/{date}/{loc_slug}/{filename}"
        presigned = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": R2_BUCKET, "Key": key,
                    "ContentType": f.get("type", "image/jpeg")},
            ExpiresIn=7200,
        )
        urls.append({"key": key, "url": presigned, "filename": filename})
        if key not in existing:
            new_meta.append({
                "key": key, "filename": filename,
                "date": date, "location": location,
                "photographer_id": p["id"],
                "photographer_name": p["name"],
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending", "folder": None,
            })

    if new_meta:
        pending_meta.extend(new_meta)
        _save_pending_meta(pending_meta)

    return {"urls": urls}

# ── Pending (cull feed) ──
@app.get("/api/pending/dates")
def pending_dates():
    meta  = _load_pending_meta()
    dates = sorted({m["date"] for m in meta if m.get("status") == "pending"}, reverse=True)
    return {"dates": dates}

@app.get("/api/pending")
def get_pending(date: str = Query(None), location: str = Query(None)):
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    meta  = _load_pending_meta()
    items = [m for m in meta
             if m.get("status") == "pending" and m.get("date") == date
             and (not location or m.get("location","").lower() == location.lower())]
    for item in items:
        item["thumb_url"] = _presigned_get(item["key"])
    return {"items": items, "date": date}

@app.post("/api/cull/organize")
async def cull_organize(request: Request):
    body   = await request.json()
    keys   = body.get("keys", [])
    folder = body.get("folder", "").strip()
    if not keys or not folder:
        return JSONResponse(status_code=400, content={"error": "Missing keys or folder"})
    meta = _load_pending_meta()
    for m in meta:
        if m["key"] in keys:
            m["folder"] = folder
    _save_pending_meta(meta)
    return {"updated": len(keys)}

@app.post("/api/cull/reject")
async def cull_reject(request: Request):
    body = await request.json()
    keys = body.get("keys", [])
    if not keys:
        return JSONResponse(status_code=400, content={"error": "No keys"})
    meta       = _load_pending_meta()
    trash_meta = _load_trash_meta()
    now        = datetime.now(timezone.utc)
    purge_at   = (now + timedelta(days=7)).isoformat()
    count      = 0
    for m in meta:
        if m["key"] not in keys or m.get("status") != "pending":
            continue
        uid       = str(uuid.uuid4())[:8]
        trash_key = f"trash/{uid}_{m['filename']}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": m["key"]},
                           Key=trash_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=m["key"])
        except Exception as e:
            print(f"Trash move failed {m['key']}: {e}")
            continue
        trash_meta.append({
            "key": trash_key, "original_key": m["key"],
            "filename": m["filename"], "date": m["date"],
            "location": m["location"], "folder": m.get("folder"),
            "photographer_id": m.get("photographer_id"),
            "photographer_name": m.get("photographer_name", ""),
            "trashed_at": now.isoformat(), "purge_at": purge_at,
        })
        m["status"] = "trashed"
        count += 1
    _save_pending_meta(meta)
    _save_trash_meta(trash_meta)
    return {"trashed": count}

@app.post("/api/cull/golive")
async def cull_golive(request: Request):
    global data
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "")
    folder   = (body.get("folder") or "").strip()
    meta     = _load_pending_meta()
    to_pub   = [
        m for m in meta
        if m.get("status") == "pending"
        and m.get("date") == date
        and m.get("location", "").lower() == location.lower()
        and (m.get("folder") or "").lower() == folder.lower()
    ]
    if not to_pub:
        return JSONResponse(status_code=404, content={"error": "No pending photos found"})

    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    is_portrait = location.lower() in PORTRAIT_LOCATIONS
    existing    = {item["path"] for item in data}
    published   = []

    for m in to_pub:
        if folder_slug:
            img_key = f"images/{date}/{loc_slug}/{folder_slug}/{m['filename']}"
        else:
            img_key = f"images/{date}/{loc_slug}/{m['filename']}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": m["key"]},
                           Key=img_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=m["key"])
        except Exception as e:
            print(f"Go Live failed {m['key']}: {e}")
            continue
        m["status"] = "live"
        m["live_key"] = img_key
        if img_key not in existing:
            data.append({
                "path":            img_key,
                "date":            date,
                "location":        location,
                "last_name":       folder if is_portrait else "",
                "group":           "" if is_portrait else folder,
                "embedding":       None,
                "photographer_id": m.get("photographer_id"),
            })
            existing.add(img_key)
        published.append(img_key)

    _save_pending_meta(meta)
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(),
                  ContentType="application/json")
    return {"published": len(published)}

# ── Trash ──
@app.get("/api/trash")
def get_trash_ep(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    trash = _load_trash_meta()
    now   = datetime.now(timezone.utc)
    out   = []
    for t in trash:
        days_left = max(0, (datetime.fromisoformat(t["purge_at"]) - now).days)
        out.append({**t, "days_left": days_left,
                    "thumb_url": _presigned_get(t["key"])})
    return {"items": out}

@app.post("/api/trash/restore")
async def trash_restore(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body      = await request.json()
    trash_key = body.get("key", "")
    trash     = _load_trash_meta()
    item      = next((t for t in trash if t["key"] == trash_key), None)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    try:
        s3.copy_object(Bucket=R2_BUCKET,
                       CopySource={"Bucket": R2_BUCKET, "Key": trash_key},
                       Key=item["original_key"])
        s3.delete_object(Bucket=R2_BUCKET, Key=trash_key)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    pending = _load_pending_meta()
    for m in pending:
        if m["key"] == item["original_key"]:
            m["status"] = "pending"
            break
    else:
        pending.append({
            "key": item["original_key"], "filename": item["filename"],
            "date": item["date"], "location": item["location"],
            "photographer_id": item.get("photographer_id"),
            "photographer_name": item.get("photographer_name", ""),
            "uploaded_at": item.get("trashed_at", ""),
            "status": "pending", "folder": item.get("folder"),
        })
    _save_pending_meta(pending)
    _save_trash_meta([t for t in trash if t["key"] != trash_key])
    return {"restored": True}

@app.post("/api/trash/empty")
async def trash_empty(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    trash = _load_trash_meta()
    for t in trash:
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=t["key"])
        except Exception:
            pass
    _save_trash_meta([])
    return {"emptied": len(trash)}

@app.post("/api/trash/autopurge")
async def trash_autopurge():
    trash = _load_trash_meta()
    now   = datetime.now(timezone.utc)
    keep, purged = [], 0
    for t in trash:
        if datetime.fromisoformat(t["purge_at"]) <= now:
            try:
                s3.delete_object(Bucket=R2_BUCKET, Key=t["key"])
                purged += 1
            except Exception:
                keep.append(t)
        else:
            keep.append(t)
    _save_trash_meta(keep)
    return {"purged": purged}

# ── Photographer management (admin) ──
@app.get("/api/photographers")
def list_photographers(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"photographers": _load_photographers()}

@app.post("/api/photographers")
async def add_photographer(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    name = body.get("name", "").strip()
    pin  = body.get("pin", "").strip()
    if not name or not pin:
        return JSONResponse(status_code=400, content={"error": "Name and PIN required"})
    photographers = _load_photographers()
    if any(p["pin"] == pin for p in photographers):
        return JSONResponse(status_code=400, content={"error": "PIN already in use"})
    p = {"id": str(uuid.uuid4())[:8], "name": name, "pin": pin,
         "default_location": body.get("default_location", ""),
         "created_at": datetime.now(timezone.utc).isoformat()}
    photographers.append(p)
    _save_photographers(photographers)
    return {"photographer": p}

@app.put("/api/photographers/{pid}")
async def update_photographer(pid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    photographers = _load_photographers()
    for p in photographers:
        if p["id"] == pid:
            for field in ("name", "pin", "default_location"):
                if field in body:
                    p[field] = body[field]
            _save_photographers(photographers)
            return {"photographer": p}
    return JSONResponse(status_code=404, content={"error": "Not found"})

@app.delete("/api/photographers/{pid}")
async def delete_photographer(pid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    photographers = [p for p in _load_photographers() if p["id"] != pid]
    _save_photographers(photographers)
    return {"deleted": True}

@app.get("/api/photographers/commission")
def photographer_commission(request: Request,
                             date_from: str = Query(""),
                             date_to:   str = Query("")):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not date_from:
        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")
    photographers = _load_photographers()
    records       = _load_clock_records()
    pending_meta  = _load_pending_meta()
    result = []
    for p in photographers:
        pid    = p["id"]
        shifts = [r for r in records
                  if r["photographer_id"] == pid
                  and date_from <= r.get("date", "") <= date_to]
        hours  = 0.0
        for s in shifts:
            if s.get("clock_in") and s.get("clock_out"):
                ci = datetime.fromisoformat(s["clock_in"])
                co = datetime.fromisoformat(s["clock_out"])
                hours += (co - ci).total_seconds() / 3600
        uploads = [m for m in pending_meta
                   if m.get("photographer_id") == pid
                   and date_from <= m.get("date", "") <= date_to]
        result.append({
            "id": pid, "name": p["name"],
            "shifts": len(shifts), "hours": round(hours, 2),
            "photos_uploaded": len(uploads),
            "clock_records": shifts,
        })
    return {"commission": result, "date_from": date_from, "date_to": date_to}

# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("templates/index.html").read())

@app.get("/checkout", response_class=HTMLResponse)
def checkout_page():
    return HTMLResponse(open("templates/checkout.html").read())

@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    return HTMLResponse(open("templates/upload.html").read())

@app.get("/clockin", response_class=HTMLResponse)
def clockin_page():
    return HTMLResponse(open("templates/clockin.html").read())

@app.get("/admin/cull", response_class=HTMLResponse)
def cull_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin")
    return HTMLResponse(open("templates/cull.html").read())

@app.get("/admin/trash", response_class=HTMLResponse)
def trash_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin")
    return HTMLResponse(open("templates/trash.html").read())

@app.get("/admin/photographers", response_class=HTMLResponse)
def photographers_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin")
    return HTMLResponse(open("templates/photographers.html").read())
