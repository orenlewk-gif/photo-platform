import threading
import zipfile
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
R2_BUCKET    = os.getenv("R2_BUCKET_NAME", "crystal-images")
PRICING_KEY  = "meta/pricing.json"

# ── Activity Pricing (R2) ─────────────────────────────────────────────────────
_pricing_cache: dict = {"data": None, "ts": 0.0}
_PRICING_TTL = 30  # seconds

def _load_pricing():
    import time
    now = time.monotonic()
    if _pricing_cache["data"] is not None and now - _pricing_cache["ts"] < _PRICING_TTL:
        return _pricing_cache["data"]
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=PRICING_KEY)
        d = json.loads(obj["Body"].read())
        if not isinstance(d, dict):
            raise ValueError("pricing.json is not a dict")
    except Exception:
        d = {"default": {"tiers": []}, "activities": {}, "combos": []}
    _pricing_cache["data"] = d
    _pricing_cache["ts"] = now
    return d

def _save_pricing(d):
    s3.put_object(Bucket=R2_BUCKET, Key=PRICING_KEY,
                  Body=json.dumps(d, indent=2).encode(), ContentType="application/json")
    _pricing_cache["data"] = d
    import time; _pricing_cache["ts"] = time.monotonic()

# Allow browser direct PUT uploads from any origin
try:
    s3.put_bucket_cors(
        Bucket=R2_BUCKET,
        CORSConfiguration={"CORSRules": [{
            "AllowedOrigins": ["*"],
            "AllowedMethods": ["GET", "PUT", "HEAD"],
            "AllowedHeaders": ["*"],
            "MaxAgeSeconds": 3600,
        }]}
    )
except Exception as _cors_err:
    print(f"R2 CORS setup warning: {_cors_err}")

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

# Locations that use last-name sub-folders (portrait style)
PORTRAIT_LOCATIONS = {"lone peak portraits", "explorer gondola", "ramcharger portraits", "adventure zip line", "adventure zip", "nature zip line", "nature zipline", "nature zip"}
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

WATERMARK_OPACITY = 0.50
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
        # Two watermarks for enlarged view — side by side on landscape, stacked on portrait
        wm_size = int(min(img.width, img.height) * 0.52)
        gap     = int(wm_size * 0.20)
        wm_scaled = wm.resize((wm_size, wm_size), Image.LANCZOS)
        if img.height > img.width:
            # Portrait: stack vertically
            total_h = wm_size * 2 + gap
            x  = (img.width  - wm_size) // 2
            y1 = (img.height - total_h) // 2
            y2 = y1 + wm_size + gap
            img_rgba.paste(wm_scaled, (x, y1), wm_scaled)
            img_rgba.paste(wm_scaled, (x, y2), wm_scaled)
        else:
            # Landscape: side by side
            total_w = wm_size * 2 + gap
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


if os.path.exists("images.json"):
    try:
        with open("images.json", "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"WARNING: local images.json corrupt ({e}), starting empty")
        data = []
else:
    print("images.json not found locally — downloading from R2...")
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
        data = json.loads(obj["Body"].read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"WARNING: R2 images.json corrupt ({e}), starting empty")
        data = []
    except Exception as e:
        print(f"WARNING: could not load images.json from R2 ({e}), starting empty")
        data = []
print(f"Loaded {len(data)} photos.")

# One-time migration: move group→last_name for Nature Zip Line entries
_NZ_LOCS = {"nature zip line", "nature zipline", "nature zip"}
_nz_fixed = 0
for _item in data:
    if ((_item.get("location") or "").strip().lower() in _NZ_LOCS
            and not _item.get("last_name") and _item.get("group")):
        _item["last_name"] = _item["group"]
        _item["group"] = ""
        _nz_fixed += 1
if _nz_fixed:
    print(f"Migrated {_nz_fixed} Nature Zip Line entries (group→last_name). Saving to R2…")
    try:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
        print("Migration saved to R2.")
    except Exception as _e:
        print(f"WARNING: could not save migration to R2: {_e}")

# One-time migration: push local pricing.json to R2 if R2 doesn't have it yet
try:
    s3.head_object(Bucket=R2_BUCKET, Key=PRICING_KEY)
    print("pricing.json already in R2.")
except Exception:
    if os.path.exists("pricing.json"):
        with open("pricing.json") as _pf:
            _local_pricing = json.load(_pf)
        if isinstance(_local_pricing, dict):
            s3.put_object(Bucket=R2_BUCKET, Key=PRICING_KEY,
                          Body=json.dumps(_local_pricing, indent=2).encode(),
                          ContentType="application/json")
            print("Migrated local pricing.json → R2.")
        else:
            print("WARNING: local pricing.json is not a dict, skipping migration.")
    else:
        print("No local pricing.json found, starting with empty pricing in R2.")


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

@app.get("/api/days")
def get_days():
    days = {}
    for item in data:
        if item.get("draft"):
            continue
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
        items = [item for item in data if item["date"] == date and not item.get("draft")]
    else:
        items = [item for item in data if not item.get("draft")]
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
        if item.get("draft"):
            continue
        if item["date"] != date:
            continue
        if clean_location(item["location"]) != location:
            continue
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
    previews: dict = {}  # key -> list of up to 4 paths
    results = []
    for item in data:
        loc = clean_location(item["location"])
        path = item.get("path", "")

        # Last-name search (portrait locations)
        ln = item.get("last_name", "").strip()
        if ln:
            ln_lower = ln.lower()
            if ln_lower.startswith(q_lower) or fuzz.partial_ratio(q_lower, ln_lower) >= 75:
                key = ("ln", ln_lower, item["date"], loc)
                if key not in seen:
                    seen.add(key)
                    previews[key] = []
                    results.append({
                        "last_name": ln,
                        "date":      item["date"],
                        "location":  loc,
                        "type":      "family",
                        "_key":      key,
                    })
                if path and len(previews[key]) < 4:
                    previews[key].append(path)

        # Group/trail search (Mountain Biking only)
        if loc.lower() in SEARCHABLE_GROUP_LOCATIONS:
            g = item.get("group", "").strip()
            if g:
                g_lower = g.lower()
                if g_lower.startswith(q_lower) or fuzz.partial_ratio(q_lower, g_lower) >= 75:
                    key = ("grp", g_lower, item["date"], loc)
                    if key not in seen:
                        seen.add(key)
                        previews[key] = []
                        results.append({
                            "group":    g,
                            "date":     item["date"],
                            "location": loc,
                            "type":     "group",
                            "_key":     key,
                        })
                    if path and len(previews[key]) < 4:
                        previews[key].append(path)

    def sort_key(x):
        name = x.get("last_name") or x.get("group", "")
        return (not name.lower().startswith(q_lower), name.lower(), x["date"])

    results.sort(key=sort_key)
    results = results[:20]
    for r in results:
        k = r.pop("_key")
        r["previews"] = previews.get(k, [])
    return {"results": results}


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
            if not item.get("draft")
            and item["date"] == date
            and clean_location(item["location"]) == location
            and (not family or item.get("last_name","").strip().lower() == family.lower()
                 or (family and not item.get("last_name","").strip() and item.get("group","").strip().lower() == family.lower()))
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
def get_pricing(request: Request, location: str = Query(None), date: str = Query(None), family: str = Query(None)):
    default = {"tiers": [
        {"label": "1 Photo",    "count": 1,     "price": 25},
        {"label": "3 Photos",   "count": 3,     "price": 60},
        {"label": "All Photos", "count": "all", "price": 90}
    ]}
    # Zip group size pricing — looks up folder's assigned group size
    if location and date and family:
        loc_lower = location.strip().lower()
        if loc_lower in ZIP_LOCS_SET:
            try:
                fm = _load_folder_meta()
                fk = _folder_key(date, location.strip(), family.strip())
                group_size = fm.get(fk, {}).get("group_size")
                # Fallback: try old "Zip Line" key variants for uploads done before rename
                if not group_size:
                    _ZIP_ALIASES = {"Nature Zip": "Nature Zip Line", "Adventure Zip": "Adventure Zip Line"}
                    alt_loc = _ZIP_ALIASES.get(location.strip())
                    if alt_loc:
                        alt_fk = _folder_key(date, alt_loc, family.strip())
                        group_size = fm.get(alt_fk, {}).get("group_size")
                if group_size:
                    zp = _load_zip_pricing()
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
    pricing = _load_pricing()
    combos = pricing.get("combos", [])
    if location:
        activities = pricing.get("activities", {})
        act_key = next((k for k in activities if k.lower() == location.strip().lower()), None)
        if act_key:
            result = dict(activities[act_key])
            result["combos"] = combos
            return result
        return {"tiers": [], "combos": combos}
    return {"tiers": [], "combos": combos}


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


@app.get("/api/admin/photo")
def admin_get_photo(request: Request, path: str, size: str = Query("thumb")):
    """Admin-only photo endpoint — no watermark. Caches to admin_thumbs/ in R2."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from fastapi.responses import StreamingResponse as SR
    SIZE_MAP = {"thumb": (450, 72), "medium": (1800, 90)}
    max_px, quality = SIZE_MAP.get(size, SIZE_MAP["thumb"])
    try:
        key = to_r2_key(path)
        if size == "thumb" and os.getenv("R2_ENDPOINT_URL"):
            cache_key = "admin_thumbs/" + key[len("images/"):] if key.startswith("images/") else "admin_thumbs/" + key
            try:
                obj = s3.get_object(Bucket=R2_BUCKET, Key=cache_key)
                buf = BytesIO(obj["Body"].read())
                buf.seek(0)
                return SR(buf, media_type="image/jpeg",
                          headers={"Cache-Control": "private, max-age=86400"})
            except Exception:
                pass
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        img = Image.open(obj["Body"]).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if size == "thumb" and os.getenv("R2_ENDPOINT_URL"):
            try:
                buf.seek(0)
                s3.put_object(Bucket=R2_BUCKET, Key=cache_key, Body=buf.read(),
                              ContentType="image/jpeg", CacheControl="private, max-age=86400")
            except Exception:
                pass
        buf.seek(0)
        return SR(buf, media_type="image/jpeg",
                  headers={"Cache-Control": "private, max-age=86400"})
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
        _combos_cfg = _load_pricing().get("combos", [])
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
        _pp = _load_print_pricing()
        _ppsizes = _pp.get("sizes", _DEFAULT_PRINT_SIZES)
        PRINT_SHIP = [s.get("ship_print", 8)   for s in _ppsizes]
        FRAME_SHIP = [s.get("ship_framed", 15)  for s in _ppsizes]

        framed   = [p for p in prints if p.get("frame") and p["frame"] != "No Frame"]
        unframed = [p for p in prints if not p.get("frame") or p["frame"] == "No Frame"]

        # Collect all frame shipping rates: framed prints + standalone frames (expanded by qty)
        frame_rates = (
            [FRAME_SHIP[int(p.get("size_idx", 0))] for p in framed] +
            [FRAME_SHIP[int(fr.get("sizeIdx", 0))]
             for fr in frames for _ in range(int(fr.get("quantity", 1)))]
        )

        FREE_SHIP_PRINT_THRESHOLD = _pp.get("free_ship_threshold", 3)

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
    raw = f"{ADMIN_SECRET}:admin-session"
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
input[type=password]{{width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.15);border-radius:8px;
  padding:.75rem 1rem;color:#fff;font-size:1rem;margin-bottom:1rem;outline:none}}
input[type=password]:focus{{border-color:#F5C518}}
button{{width:100%;background:#F5C518;color:#0c2336;border:none;border-radius:8px;
  padding:.75rem;font-size:1rem;font-weight:700;cursor:pointer}}
.err{{color:#e53e3e;font-size:.85rem;text-align:center;margin-top:.5rem}}
</style></head><body>
<div class="card">
  <h2>Crystal Images Admin</h2>
  <form method="post" action="/admin/login">
    <input type="hidden" name="next" value="{next}" />
    <input type="password" name="password" placeholder="Password" autofocus />
    <button type="submit">Sign In</button>
    {error}
  </form>
</div></body></html>"""

@app.get("/admin", response_class=HTMLResponse)
def admin_get(request: Request):
    if not _admin_authed(request):
        next_url = request.query_params.get("next", "/admin/dashboard")
        return HTMLResponse(ADMIN_LOGIN_HTML.format(error="", next=next_url))
    return RedirectResponse("/admin/dashboard")

@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    pw   = form.get("password", "")
    next_url = form.get("next", "/admin/dashboard") or "/admin/dashboard"
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw, ADMIN_PASSWORD):
        return HTMLResponse(ADMIN_LOGIN_HTML.format(
            error='<p class="err">Incorrect password.</p>', next=next_url), status_code=401)
    token = _make_session_token()
    resp  = RedirectResponse(next_url, status_code=303)
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

def _get_best_token_for_order(order_id):
    """Return (token, rec, expires_dt) for the most-recently-expiring token on this order."""
    best_token, best_rec, best_exp = None, None, None
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="downloads/"):
            for obj in page.get("Contents", []):
                key   = obj["Key"]
                token = key.replace("downloads/", "").replace(".json", "")
                rec   = _load_token(token)
                if rec and str(rec.get("order_id")) == str(order_id):
                    try:
                        exp = datetime.fromisoformat(rec["expires"])
                        if best_exp is None or exp > best_exp:
                            best_token, best_rec, best_exp = token, rec, exp
                    except Exception:
                        pass
    except Exception:
        pass
    return best_token, best_rec, best_exp

@app.get("/api/admin/order-link/{order_id}")
def api_admin_order_link(request: Request, order_id: int):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    token, rec, expires_dt = _get_best_token_for_order(order_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "No download record found"})
    expired = datetime.now(timezone.utc) > expires_dt
    return {"url": f"{SITE_URL}/download/{token}", "expired": expired,
            "expires": expires_dt.isoformat()}

@app.get("/api/admin/download-folder")
def admin_download_folder(
    request: Request,
    date: str = Query(...),
    location: str = Query(...),
    family: str = Query(None),
    group: str = Query(None),
):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    all_data = _r2_json_load("images.json", [])
    loc_lower = location.strip().lower()
    sub = (family or group or '').strip().lower()
    field = "last_name" if family else "group"
    matches = []
    for item in all_data:
        if item.get("date") != date:
            continue
        if (item.get("location") or "").strip().lower() != loc_lower:
            continue
        if sub and (item.get(field) or "").strip().lower() != sub:
            continue
        path = item.get("path", "")
        if path:
            matches.append(path)
    if not matches:
        return JSONResponse(status_code=404, content={"error": "No photos found"})
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for path in matches:
            idx = path.find("images/")
            r2_key = path[idx:].replace("\\", "/") if idx >= 0 else path
            try:
                obj = s3.get_object(Bucket=R2_BUCKET, Key=r2_key)
                zf.writestr(os.path.basename(r2_key), obj["Body"].read())
            except Exception:
                pass
    buf.seek(0)
    label = "_".join(filter(None, [date, location.replace(" ", "_"), family or group]))
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{label}.zip"'},
    )

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
    photo_paths  = meta.get("_photo_paths", "")
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
        "photo_paths": photo_paths,
    }

@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, days: int = 30,
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
    rows       = [_order_row(o) for o in orders]
    total_rev  = sum(r["total"]      for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    total_net  = sum(r["net"]        for r in rows)

    # Location breakdown
    loc_stats = {}
    for r in rows:
        loc = r["location"] or "Unknown"
        if loc not in loc_stats:
            loc_stats[loc] = {"count": 0, "revenue": 0.0, "net": 0.0}
        loc_stats[loc]["count"]   += 1
        loc_stats[loc]["revenue"] += r["total"]
        loc_stats[loc]["net"]     += r["net"]
    loc_stats = dict(sorted(loc_stats.items(), key=lambda x: x[1]["revenue"], reverse=True))
    now_utc    = datetime.now(timezone.utc)

    # Photographer prefix map
    _photogs    = _load_photographers()
    _prefix_map = {p.get("file_prefix","").strip().lower(): p["name"]
                   for p in _photogs if p.get("file_prefix","").strip()}
    def _detect_photog(filenames_str):
        if not filenames_str:
            return "Oren Lewkowski"
        names, seen = [], set()
        for f in filenames_str.split(','):
            f = f.strip()
            if not f: continue
            name = _prefix_map.get(f[:3].lower())
            if name and name not in seen:
                names.append(name); seen.add(name)
        return " & ".join(names) if names else "Oren Lewkowski"

    import json as _json
    def td(v, cls=""): return f'<td class="{cls}">{v}</td>'
    trs = ""
    for r in rows:
        first_file  = (r['filenames'] or '').split(',')[0].strip() or '—'
        photographer = _detect_photog(r['filenames'])
        detail_data = _json.dumps({
            "name":         f"{r['first']} {r['last']}",
            "email":        r['email'],
            "location":     r['location'] or '—',
            "photographer": photographer,
            "what":         r['what'],
            "files":        r['filenames'] or '—',
            "total":        f"${r['total']:.2f}",
            "discount":     f"-${r['discount']:.2f}" if r['discount'] else '—',
            "fee":          f"${r['stripe_fee']:.2f}",
            "net":          f"${r['net']:.2f}",
            "coupon":       r['coupon_code'] or '—',
            "shipping":     f"${r['shipping']:.2f}" if r['shipping'] else '—',
            "address":      r['ship_addr'] or '—',
            "photo_paths":  r['photo_paths'] or '',
            "date":         r['date'] or '',
            "last_name":    r['last'] or '',
        }).replace("'", "&#39;").replace('"', '&quot;')
        try:
            order_dt  = datetime.strptime(r['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
            is_recent = (now_utc - order_dt).days < DOWNLOAD_EXPIRE_DAYS
        except Exception:
            is_recent = False
        if is_recent:
            action_btn = f'<button class="copy-btn" onclick="copyLink({r["order_id"]},event)">Copy Link</button>'
        else:
            action_btn = f'<a href="/admin/regen/{r["order_id"]}" class="regen-btn" onclick="event.stopPropagation()">↺ Regen Link</a>'
        trs += f"""<tr class="order-row" onclick="showDetail('{detail_data}')" style="cursor:pointer">
          {td(r['date'])}
          {td(f"{r['first']} {r['last']}")}
          {td(r['location'] or '—')}
          {td(r['email'])}
          {td(r['what'])}
          {td(photographer)}
          {td(first_file, 'mono')}
          {td(f"${r['total']:.2f}")}
          {td(f"-${r['discount']:.2f}" if r['discount'] else '—', 'fee')}
          {td(f"${r['stripe_fee']:.2f}", 'fee')}
          {td(f"${r['net']:.2f}", 'net')}
          {td(r['coupon_code'] or '—')}
          {td(f"${r['shipping']:.2f}" if r['shipping'] else '—')}
          {td(r['ship_addr'] or '—')}
          {td(action_btn)}
        </tr>"""

    period_opts = "".join(
        f'<option value="{d}" {"selected" if d==days else ""}>{label}</option>'
        for d, label in [(7,"Last 7 days"),(14,"Last 14 days"),(30,"Last 30 days"),(60,"Last 60 days"),(90,"Last 90 days")]
    )
    export_qs = f"date_from={date_from}&date_to={date_to}" if custom else f"days={days}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Orders — Crystal Images</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f1117;font-family:'Segoe UI',sans-serif;color:#e8eaf0;min-height:100vh;display:flex;flex-direction:column}}
#topbar{{height:52px;background:#0a1320;border-bottom:1px solid rgba(255,255,255,.07);display:flex;align-items:center;padding:0 1.4rem;flex-shrink:0}}
#topbar h1{{font-size:13px;font-weight:700;letter-spacing:.5px;color:#a0aec0;margin-right:auto}}
#topbar a{{font-size:12px;color:rgba(255,255,255,.35);text-decoration:none;padding:.3rem .6rem;border-radius:5px}}
#topbar a:hover{{color:rgba(255,255,255,.65);background:rgba(255,255,255,.05)}}
#layout{{display:flex;flex:1;height:calc(100vh - 52px);overflow:hidden}}
#sidebar{{width:240px;flex-shrink:0;background:#0a1320;border-right:1px solid rgba(255,255,255,.07);display:flex;flex-direction:column;overflow:hidden}}
#sidebar-tree{{flex:1;overflow-y:auto;padding:.4rem 0}}
.st-date{{padding:.38rem .85rem;cursor:pointer;display:flex;align-items:center;gap:.4rem;font-size:.79rem;font-weight:600;color:rgba(255,255,255,.5);user-select:none}}
.st-date:hover{{background:rgba(255,255,255,.04);color:#fff}}
.st-arr{{font-size:.55rem;transition:transform .15s;color:rgba(255,255,255,.22);flex-shrink:0}}
.st-date.st-open .st-arr{{transform:rotate(90deg)}}
.st-locs{{display:none}}
.st-date.st-open+.st-locs{{display:block}}
.st-loc{{padding:.3rem .85rem .3rem 1.5rem;font-size:.76rem;color:rgba(255,255,255,.42);cursor:pointer;display:flex;align-items:center;gap:.35rem}}
.st-loc:hover{{background:rgba(255,255,255,.04);color:rgba(255,255,255,.8)}}
.st-loc.st-open .st-arr{{transform:rotate(90deg)}}
.st-subs{{display:none}}
.st-loc.st-open+.st-subs{{display:block}}
.st-sub{{padding:.27rem .85rem .27rem 2.5rem;font-size:.75rem;color:rgba(255,255,255,.35);cursor:pointer;display:flex;align-items:center;justify-content:space-between;border-radius:4px;margin:0 .3rem}}
.st-sub:hover{{background:rgba(255,255,255,.05);color:rgba(255,255,255,.7)}}
.st-cnt{{font-size:.66rem;color:rgba(255,255,255,.22)}}
.nav-sec-label{{padding:.85rem .9rem .3rem;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:rgba(255,255,255,.22)}}
.nav-link{{display:block;padding:.52rem .9rem;border-radius:6px;margin:.05rem .45rem;font-size:15px;color:rgba(255,255,255,.5);text-decoration:none;transition:background .12s,color .12s}}
.nav-link:hover{{background:rgba(255,255,255,.06);color:rgba(255,255,255,.85)}}
.nav-link.active{{background:rgba(245,197,24,.08);color:#F5C518}}
#main{{flex:1;overflow-y:auto;padding:1.75rem 2rem}}
.stats{{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}}
.stat{{background:#1a1d27;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:.9rem 1.25rem;flex:1;min-width:130px}}
.stat .label{{font-size:11px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px}}
.stat .val{{font-size:1.35rem;font-weight:700;color:#F5C518}}
.controls{{display:flex;gap:.75rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap}}
select{{background:#1a1d27;border:1px solid rgba(255,255,255,.15);color:#fff;padding:.45rem .8rem;border-radius:8px;font-size:.85rem;cursor:pointer}}
input[type=date]{{background:#1a1d27;border:1px solid rgba(255,255,255,.15);color:#fff;padding:.42rem .7rem;border-radius:8px;font-size:.85rem}}
.export-btn{{background:#F5C518;color:#0f1117;border:none;border-radius:8px;padding:.45rem 1.1rem;font-weight:700;cursor:pointer;font-size:.85rem;text-decoration:none}}
.wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.82rem;min-width:1100px}}
th{{background:#1a1d27;color:rgba(255,255,255,.45);font-weight:600;font-size:.7rem;letter-spacing:.8px;text-transform:uppercase;padding:.55rem .7rem;text-align:left;border-bottom:1px solid rgba(255,255,255,.08);white-space:nowrap}}
td{{padding:.6rem .7rem;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:top}}
.mono{{font-family:monospace;font-size:.75rem;color:rgba(255,255,255,.55);max-width:200px;word-break:break-all;white-space:normal}}
.fee{{color:#f87171}}
.net{{color:#4ade80;font-weight:600}}
.empty{{text-align:center;padding:3rem;color:rgba(255,255,255,.3)}}
.order-row:hover td{{background:rgba(255,255,255,.04);cursor:pointer}}
.copy-btn{{background:rgba(99,179,237,.12);border:1px solid rgba(99,179,237,.3);color:#63b3ed;padding:.28rem .75rem;border-radius:6px;font-size:.78rem;cursor:pointer;white-space:nowrap}}
.copy-btn:hover{{background:rgba(99,179,237,.22)}}
.regen-btn{{background:rgba(245,197,24,.1);border:1px solid rgba(245,197,24,.25);color:#F5C518;padding:.28rem .75rem;border-radius:6px;font-size:.78rem;text-decoration:none;white-space:nowrap;display:inline-block}}
.regen-btn:hover{{background:rgba(245,197,24,.2)}}
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;align-items:center;justify-content:center}}
.modal-overlay.open{{display:flex}}
.modal{{background:#1a1d27;border:1px solid rgba(255,255,255,.12);border-radius:14px;padding:1.5rem;width:90%;max-height:90vh;overflow-y:auto;transition:max-width .2s}}
.modal.narrow{{max-width:560px}}
.modal.wide{{max-width:980px}}
.modal h2{{color:#F5C518;font-size:1rem;margin-bottom:1rem;display:flex;align-items:center;gap:.6rem}}
.modal-row{{display:flex;gap:1rem;padding:.5rem 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:.85rem}}
.modal-row:last-child{{border-bottom:none}}
.modal-label{{color:rgba(255,255,255,.4);min-width:110px;flex-shrink:0}}
.modal-val{{color:#fff;word-break:break-word}}
.modal-close{{margin-left:auto;background:none;border:none;color:rgba(255,255,255,.4);font-size:1.2rem;cursor:pointer}}
.modal-close:hover{{color:#fff}}
.photo-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:.5rem;margin-top:1rem}}
.photo-grid img{{width:100%;aspect-ratio:3/2;object-fit:cover;border-radius:6px;cursor:pointer;transition:opacity .15s}}
.photo-grid img:hover{{opacity:.85}}
.photo-grid-lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:10000;align-items:center;justify-content:center}}
.photo-grid-lb.open{{display:flex}}
.photo-grid-lb img{{max-width:90vw;max-height:90vh;border-radius:8px;object-fit:contain}}
.loc-breakdown{{margin-bottom:1.5rem}}
.loc-breakdown-title{{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.3);margin-bottom:.6rem}}
.loc-bars{{display:flex;flex-direction:column;gap:.45rem}}
.loc-bar-row{{display:flex;align-items:center;gap:.75rem;font-size:.82rem}}
.loc-bar-label{{width:160px;flex-shrink:0;color:rgba(255,255,255,.7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.loc-bar-track{{flex:1;background:rgba(255,255,255,.06);border-radius:4px;height:8px;overflow:hidden}}
.loc-bar-fill{{height:100%;background:#F5C518;border-radius:4px;transition:width .3s}}
.loc-bar-stats{{width:200px;flex-shrink:0;display:flex;gap:.75rem;color:rgba(255,255,255,.5);white-space:nowrap;font-size:.78rem}}
.loc-bar-pct{{color:#F5C518;font-weight:700;min-width:38px;text-align:right}}
.loc-bar-rev{{color:#e2e8f0}}
.loc-bar-cnt{{color:rgba(255,255,255,.35)}}
#toast{{position:fixed;bottom:1.5rem;right:1.5rem;padding:.6rem 1.1rem;border-radius:8px;font-size:13px;font-weight:500;opacity:0;transition:opacity .25s;z-index:9999;pointer-events:none}}
#toast.show{{opacity:1;background:#16a34a;color:#fff}}
#toast.err{{opacity:1;background:#dc2626;color:#fff}}
</style></head><body>
<div id="topbar">
  <h1>Crystal Images — Admin</h1>
  <a href="/admin/logout">Sign out</a>
</div>
<div id="layout">
  <div id="sidebar">
    <div class="nav-sec-label">Admin</div>
    <a href="/admin/dashboard" class="nav-link">Dashboard</a>
    <a href="/admin/pricing" class="nav-link">Pricing</a>
    <a href="/admin/photographers" class="nav-link">Photographers</a>
    <a href="/admin/orders" class="nav-link active">Orders</a>
    <div id="sidebar-tree"></div>
    <div style="border-top:1px solid rgba(255,255,255,.07);padding:.25rem 0;flex-shrink:0">
      <a href="/admin/trash" class="nav-link">Trash</a>
    </div>
  </div>
  <div id="main">
<div class="modal-overlay" id="detail-modal" onclick="closeDetail(event)">
  <div class="modal narrow" id="detail-modal-box">
    <h2>Order Details <button class="modal-close" onclick="document.getElementById('detail-modal').classList.remove('open')">✕</button></h2>
    <div id="detail-body"></div>
  </div>
</div>
<div class="photo-grid-lb" id="photo-lb" onclick="this.classList.remove('open')">
  <img id="photo-lb-img" src="" alt="">
</div>
<div id="toast"></div>
<script>
function showDetail(encoded) {{
  try {{
    const d = JSON.parse(encoded.replace(/&quot;/g,'"').replace(/&#39;/g,"'"));
    const fields = [
      ['Name', d.name], ['Email', d.email], ['Location', d.location], ['Photographer', d.photographer], ['Ordered', d.what],
      ['Total', d.total], ['Discount', d.discount], ['Stripe Fee', d.fee],
      ['Net', d.net], ['Coupon', d.coupon], ['Shipping', d.shipping],
      ['Billing Address', d.address],
    ];
    let html = fields.map(([l,v]) =>
      `<div class="modal-row"><span class="modal-label">${{l}}</span><span class="modal-val">${{v}}</span></div>`
    ).join('');

    const paths = (d.photo_paths || '').split('|').filter(Boolean);
    const box = document.getElementById('detail-modal-box');
    if (paths.length) {{
      box.classList.remove('narrow'); box.classList.add('wide');
      html += `<div style="margin-top:1rem;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.3)">Photos Ordered (${{paths.length}})</div>`;
      html += '<div class="photo-grid">' + paths.map(p =>
        `<img src="/api/admin/photo?path=${{encodeURIComponent(p)}}&amp;size=thumb" loading="lazy" onclick="openLb(this)" alt="">`
      ).join('') + '</div>';
    }} else {{
      box.classList.remove('wide'); box.classList.add('narrow');
    }}
    if (d.date && d.location && d.location !== '—' && d.last_name) {{
      const dlUrl = `/api/admin/download-folder?date=${{encodeURIComponent(d.date)}}&location=${{encodeURIComponent(d.location)}}&family=${{encodeURIComponent(d.last_name)}}`;
      html += `<div style="margin-top:1.2rem;text-align:right"><a href="${{dlUrl}}" class="copy-btn" style="display:inline-block;text-decoration:none;padding:.45rem 1rem;font-size:.82rem">⬇ Download All Folder Photos</a></div>`;
    }}

    document.getElementById('detail-body').innerHTML = html;
    document.getElementById('detail-modal').classList.add('open');
  }} catch(e) {{
    console.error('showDetail error:', e);
  }}
}}
function closeDetail(e) {{
  if (e.target === document.getElementById('detail-modal'))
    document.getElementById('detail-modal').classList.remove('open');
}}
function openLb(img) {{
  document.getElementById('photo-lb-img').src = img.src.replace('size=thumb','size=medium');
  document.getElementById('photo-lb').classList.add('open');
}}
let _tt;
function showToast(msg, type='show') {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = type;
  clearTimeout(_tt); _tt = setTimeout(() => t.className='', 2500);
}}
async function copyLink(orderId, e) {{
  e.stopPropagation();
  const btn = e.currentTarget;
  const orig = btn.textContent;
  btn.textContent = '…';
  try {{
    const res = await fetch(`/api/admin/order-link/${{orderId}}`);
    const d = await res.json();
    if (!d.url || d.expired) {{
      btn.textContent = '↺ Regen Link';
      btn.className = 'regen-btn';
      btn.onclick = ev => {{ ev.stopPropagation(); location.href = `/admin/regen/${{orderId}}`; }};
      showToast('Link expired — click Regen Link to renew', 'err');
      return;
    }}
    try {{
      await navigator.clipboard.writeText(d.url);
      btn.textContent = 'Copied!';
      showToast('Download link copied to clipboard');
    }} catch(clipErr) {{
      window.prompt('Copy this download link:', d.url);
      btn.textContent = orig;
    }}
    setTimeout(() => {{ if (btn.textContent === 'Copied!') btn.textContent = orig; }}, 2500);
  }} catch(err) {{
    btn.textContent = orig;
    showToast('Copy failed', 'err');
  }}
}}
(async function(){{
  try{{
    var r=await fetch('/api/admin/folders');
    var d=await r.json();
    var folders=d.folders||[];
    var tree={{}};
    for(var i=0;i<folders.length;i++){{
      var f=folders[i];
      if(!tree[f.date])tree[f.date]={{}};
      if(!tree[f.date][f.location])tree[f.date][f.location]=[];
      tree[f.date][f.location].push(f);
    }}
    var dates=Object.keys(tree).sort().reverse();
    var el=document.getElementById('sidebar-tree');
    if(!dates.length||!el)return;
    var h='';
    for(var di=0;di<dates.length;di++){{
      var date=dates[di];
      h+='<div class="st-date" onclick="this.classList.toggle(\\'st-open\\')"><span class="st-arr">&#9654;</span>'+date+'</div><div class="st-locs">';
      var locs=tree[date];
      var locKeys=Object.keys(locs);
      for(var li=0;li<locKeys.length;li++){{
        var loc=locKeys[li];
        var subs=locs[loc];
        var hasSubs=false;
        for(var si=0;si<subs.length;si++){{if(subs[si].name){{hasSubs=true;break;}}}}
        if(hasSubs){{
          h+='<div class="st-loc" onclick="this.classList.toggle(\\'st-open\\')"><span class="st-arr">&#9654;</span>'+loc+'</div><div class="st-subs">';
          for(var si2=0;si2<subs.length;si2++){{
            var fs=subs[si2];
            if(!fs.name)continue;
            var u='/admin/dashboard?od='+encodeURIComponent(fs.date)+'&ol='+encodeURIComponent(fs.location)+'&of='+encodeURIComponent(fs.name);
            h+='<div class="st-sub" onclick="location.href=\\''+u+'\\'">'+fs.name+'<span class="st-cnt">'+fs.photo_count+'</span></div>';
          }}
          h+='</div>';
        }}else{{
          var f0=subs[0]||{{}};
          var u2='/admin/dashboard?od='+encodeURIComponent(f0.date||'')+'&ol='+encodeURIComponent(loc)+'&of=';
          h+='<div class="st-sub" style="padding-left:1.5rem" onclick="location.href=\\''+u2+'\\'">'+loc+'<span class="st-cnt">'+(f0.photo_count||'')+'</span></div>';
        }}
      }}
      h+='</div>';
    }}
    el.innerHTML=h;
    var first=el.querySelector('.st-date');
    if(first)first.classList.add('st-open');
  }}catch(e){{}}
}})();
</script>
  <div class="stats">
    <div class="stat"><div class="label">Orders</div><div class="val">{len(rows)}</div></div>
    <div class="stat"><div class="label">Revenue</div><div class="val">${total_rev:.2f}</div></div>
    <div class="stat"><div class="label">Stripe Fees</div><div class="val" style="color:#f87171">${total_fees:.2f}</div></div>
    <div class="stat"><div class="label">Net</div><div class="val" style="color:#4ade80">${total_net:.2f}</div></div>
  </div>
  <div class="loc-breakdown">
    <div class="loc-breakdown-title">Revenue by Location</div>
    <div class="loc-bars">
      {"".join(
        f'<div class="loc-bar-row">'
        f'<span class="loc-bar-label">{loc}</span>'
        f'<div class="loc-bar-track"><div class="loc-bar-fill" style="width:{(s["revenue"]/total_rev*100) if total_rev else 0:.1f}%"></div></div>'
        f'<div class="loc-bar-stats">'
        f'<span class="loc-bar-pct">{(s["revenue"]/total_rev*100) if total_rev else 0:.1f}%</span>'
        f'<span class="loc-bar-rev">${s["revenue"]:.0f}</span>'
        f'<span class="loc-bar-cnt">{s["count"]} order{"s" if s["count"]!=1 else ""}</span>'
        f'</div></div>'
        for loc, s in loc_stats.items()
      ) if loc_stats else "<span style='color:rgba(255,255,255,.3);font-size:.82rem'>No orders in this period.</span>"}
    </div>
  </div>
  <div class="controls">
    <form method="get" style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;">
      <select name="days" onchange="this.form.submit()">
        {period_opts}
      </select>
      <span style="color:rgba(255,255,255,.3);font-size:.85rem;">or</span>
      <input type="date" name="date_from" value="{date_from}" placeholder="From" />
      <input type="date" name="date_to" value="{date_to}" placeholder="To" />
      <button type="submit" style="background:rgba(245,197,24,.12);border:1px solid rgba(245,197,24,.3);color:#F5C518;padding:.42rem .9rem;border-radius:8px;cursor:pointer;font-size:.85rem;">Apply</button>
    </form>
    <a class="export-btn" href="/admin/export?{export_qs}">↓ Export CSV</a>
  </div>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Date</th><th>Name</th><th>Location</th><th>Email</th><th>Ordered</th><th>Photographer</th>
      <th>Photo Files</th><th>Total</th><th>Discount</th><th>Stripe Fee</th><th>Net</th>
      <th>Coupon</th><th>Shipping</th><th>Address</th><th>Action</th>
    </tr></thead>
    <tbody>{trs if trs else '<tr><td colspan="15" class="empty">No completed orders in this period.</td></tr>'}</tbody>
  </table>
  </div>
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
    _exp_photogs    = _load_photographers()
    _exp_prefix_map = {p.get("file_prefix","").strip().lower(): p["name"]
                       for p in _exp_photogs if p.get("file_prefix","").strip()}
    def _exp_photog(fn):
        if not fn: return "Oren Lewkowski"
        names, seen = [], set()
        for f in fn.split(','):
            f = f.strip()
            if not f: continue
            name = _exp_prefix_map.get(f[:3].lower())
            if name and name not in seen:
                names.append(name); seen.add(name)
        return " & ".join(names) if names else "Oren Lewkowski"
    writer.writerow(["Date","First Name","Last Name","Email","Photographer","What Ordered",
                     "Photo Files","Location","Photo Date","Total","Discount",
                     "Stripe Fee","Net","Coupon Code","Shipping Cost","Shipping Address"])
    for r in rows:
        writer.writerow([r["date"], r["first"], r["last"], r["email"], _exp_photog(r["filenames"]), r["what"],
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
  <a class="back" href="/admin/orders">← Back to orders</a>
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

# ── Clock record edit / delete ──
@app.put("/api/clock/record/{record_id}")
async def edit_clock_record(record_id: str, request: Request):
    body = await request.json()
    records = _load_clock_records()
    for r in records:
        if r["id"] == record_id:
            if "clock_in" in body and body["clock_in"]:
                r["clock_in"] = body["clock_in"]
            if "clock_out" in body:
                r["clock_out"] = body["clock_out"] or None
            if "location" in body:
                r["location"] = body["location"]
            _save_clock_records(records)
            return {"record": r}
    return JSONResponse(status_code=404, content={"error": "Record not found"})

@app.delete("/api/clock/record/{record_id}")
async def delete_clock_record(record_id: str):
    records = _load_clock_records()
    new_records = [r for r in records if r["id"] != record_id]
    if len(new_records) == len(records):
        return JSONResponse(status_code=404, content={"error": "Record not found"})
    _save_clock_records(new_records)
    return {"status": "deleted"}

@app.post("/api/clock/record")
async def create_clock_record(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    photographer_id = body.get("photographer_id", "")
    photographers = _load_photographers()
    p = next((x for x in photographers if x["id"] == photographer_id), None)
    if not p:
        return JSONResponse(status_code=404, content={"error": "Photographer not found"})
    record = {
        "id": str(uuid.uuid4()),
        "photographer_id": photographer_id,
        "photographer_name": p["name"],
        "location": body.get("location", ""),
        "clock_in": body.get("clock_in", ""),
        "clock_out": body.get("clock_out") or None,
        "date": body.get("clock_in", "")[:10] if body.get("clock_in") else "",
    }
    records = _load_clock_records()
    records.append(record)
    _save_clock_records(records)
    return {"record": record}

@app.get("/api/clock/records")
def get_all_clock_records(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    records = _load_clock_records()
    photographers = _load_photographers()
    return {"records": records, "photographers": photographers}

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

    folder      = body.get("folder", "").strip()
    batch_id    = body.get("batch_id") or str(uuid.uuid4())[:8]
    batch_total = len(files)

    for f in files:
        filename = os.path.basename(f["name"])
        folder_slug = folder.lower().replace(" ", "-") if folder else "unfiled"
        key      = f"pending/{date}/{p['id']}/{folder_slug}/{filename}"
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
                "folder": folder,
                "batch_id": batch_id,
                "batch_total": batch_total,
                "photographer_id": p["id"],
                "photographer_name": p["name"],
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "status": "presigned",
            })

    if new_meta:
        pending_meta.extend(new_meta)
        _save_pending_meta(pending_meta)

    return {"urls": urls, "batch_id": batch_id, "batch_total": batch_total}

@app.post("/api/upload/confirm")
async def upload_confirm(request: Request):
    body = await request.json()
    key  = body.get("key")
    if not key:
        return JSONResponse(status_code=400, content={"error": "Missing key"})
    meta = _load_pending_meta()
    for m in meta:
        if m["key"] == key and m.get("status") == "presigned":
            m["status"] = "pending"
            m["uploaded_at"] = datetime.now(timezone.utc).isoformat()
            break
    _save_pending_meta(meta)
    return {"confirmed": key}

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
    if not keys:
        return JSONResponse(status_code=400, content={"error": "Missing keys"})
    meta = _load_pending_meta()
    for m in meta:
        if m["key"] in keys:
            m["folder"] = folder
    _save_pending_meta(meta)
    return {"updated": len(keys)}

@app.post("/api/cull/rename-folder")
async def cull_rename_folder(request: Request):
    body       = await request.json()
    old_folder = body.get("old_folder", "").strip()
    new_folder = body.get("new_folder", "").strip()
    if not old_folder or not new_folder:
        return JSONResponse(status_code=400, content={"error": "Missing folder names"})
    meta = _load_pending_meta()
    updated = 0
    for m in meta:
        if m.get("folder", "") == old_folder:
            m["folder"] = new_folder
            updated += 1
    _save_pending_meta(meta)
    return {"updated": updated}

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
    global data
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
    if item.get("type") == "studio":
        entry = item.get("image_entry")
        if entry and entry["path"] not in {d["path"] for d in data}:
            data.append(entry)
            s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                          Body=json.dumps(data).encode(), ContentType="application/json")
    else:
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
         "file_prefix": body.get("file_prefix", "").strip().lower(),
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
            for field in ("name", "pin", "default_location", "file_prefix"):
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

# ── Upload downloads ──
@app.get("/api/uploads")
def list_uploads(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    meta = _load_pending_meta()
    batches = {}
    for m in meta:
        status = m.get("status")
        if status not in ("pending", "presigned"):
            continue
        bid = m.get("batch_id", f"{m.get('photographer_id')}_{m.get('date')}_{m.get('folder')}")
        if bid not in batches:
            batches[bid] = {
                "batch_id": bid,
                "photographer_id": m.get("photographer_id", ""),
                "photographer_name": m.get("photographer_name", "Unknown"),
                "date": m.get("date", ""),
                "folder": m.get("folder") or "Unfiled",
                "received": 0,
                "expected": m.get("batch_total", 0),
                "first_seen": m.get("uploaded_at", ""),
            }
        if status == "pending":
            batches[bid]["received"] += 1
        if not batches[bid]["expected"]:
            batches[bid]["expected"] = m.get("batch_total", 0)
        dc = m.get("download_count", 0)
        if dc > batches[bid].get("download_count", 0):
            batches[bid]["download_count"] = dc
    result = sorted(batches.values(), key=lambda x: x["first_seen"])
    return {"batches": result}

@app.get("/api/uploads/zip-all")
def download_all_zip(request: Request, batch_id: str = Query(...)):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    meta = _load_pending_meta()
    items = [m for m in meta
             if m.get("status") in ("pending", "presigned") and
             m.get("batch_id", f"{m.get('photographer_id')}_{m.get('date')}_{m.get('folder')}") == batch_id]
    if not items:
        return JSONResponse(status_code=404, content={"error": "No files found"})

    photographer_name = items[0].get("photographer_name", "photos").replace(" ", "_")
    date = items[0].get("date", "")
    folder_label = items[0].get("folder", "").replace(" ", "_") or "upload"

    # Fetch all photos from R2 in parallel, then build zip in memory
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def fetch_one(m):
        obj = s3.get_object(Bucket=R2_BUCKET, Key=m["key"])
        return (m.get("folder") or "Unfiled", m["filename"], obj["Body"].read())

    file_data = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_one, m): m for m in items}
        for f in as_completed(futures):
            try:
                file_data.append(f.result())
            except Exception as e:
                print(f"zip fetch failed: {e}")

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for folder_name, filename, data_bytes in file_data:
            zf.writestr(f"{folder_name}/{filename}", data_bytes)
    zip_bytes = buf.getvalue()

    # Increment download count
    meta = _load_pending_meta()
    for m in meta:
        if m.get("batch_id") == batch_id:
            m["download_count"] = m.get("download_count", 0) + 1
    _save_pending_meta(meta)

    zip_filename = f"{date}_{photographer_name}_{folder_label}.zip"
    from fastapi.responses import Response
    return Response(content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'})


@app.delete("/api/uploads/batch/{batch_id}")
def trash_batch(batch_id: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    meta = _load_pending_meta()
    items = [m for m in meta if m.get("batch_id") == batch_id
             and m.get("status") in ("pending", "presigned")]
    if not items:
        return JSONResponse(status_code=404, content={"error": "No files found"})

    now = datetime.now(timezone.utc)
    purge_at = (now + timedelta(days=7)).isoformat()
    photographer_id = items[0].get("photographer_id", "")

    photographer_name = items[0].get("photographer_name", "")
    date = items[0].get("date", "")
    location = items[0].get("location", "")

    from concurrent.futures import ThreadPoolExecutor
    import threading
    trash_lock = threading.Lock()
    trash_meta = _load_trash_meta()

    def move_one(m):
        uid = str(uuid.uuid4())[:8]
        trash_key = f"upload_trash/{uid}_{m['filename']}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": m["key"]},
                           Key=trash_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=m["key"])
            entry = {"key": trash_key, "original_key": m["key"],
                     "filename": m["filename"], "folder": m.get("folder", ""),
                     "batch_id": batch_id, "photographer_id": photographer_id,
                     "photographer_name": photographer_name,
                     "date": date, "location": location,
                     "trashed_at": now.isoformat(), "purge_at": purge_at}
            with trash_lock:
                trash_meta.append(entry)
        except Exception as e:
            print(f"Trash move failed {m['key']}: {e}")

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(move_one, items))

    for m in meta:
        if m.get("batch_id") == batch_id:
            m["status"] = "downloaded"
    _save_pending_meta(meta)
    _save_trash_meta(trash_meta)
    return {"ok": True}


@app.post("/api/trash/restore-batch")
async def trash_restore_batch(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    batch_id = body.get("batch_id", "")
    trash    = _load_trash_meta()
    batch    = [t for t in trash if t.get("batch_id") == batch_id]
    if not batch:
        return JSONResponse(status_code=404, content={"error": "Batch not found"})
    pending = _load_pending_meta()
    for t in batch:
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": t["key"]},
                           Key=t["original_key"])
            s3.delete_object(Bucket=R2_BUCKET, Key=t["key"])
        except Exception as e:
            print(f"Batch restore failed {t['key']}: {e}")
            continue
        for m in pending:
            if m["key"] == t["original_key"]:
                m["status"] = "pending"
                break
        else:
            pending.append({
                "key": t["original_key"], "filename": t["filename"],
                "date": t.get("date", ""), "location": t.get("location", ""),
                "folder": t.get("folder", ""), "batch_id": batch_id,
                "photographer_id": t.get("photographer_id", ""),
                "photographer_name": t.get("photographer_name", ""),
                "uploaded_at": t.get("trashed_at", ""),
                "status": "pending", "batch_total": len(batch),
            })
    _save_pending_meta(pending)
    _save_trash_meta([t for t in trash if t.get("batch_id") != batch_id])
    return {"restored": len(batch)}

@app.get("/admin/downloads", response_class=HTMLResponse)
def downloads_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/downloads")
    return HTMLResponse(open("templates/downloads.html").read())

@app.get("/admin/cull", response_class=HTMLResponse)
def cull_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/cull")
    return HTMLResponse(open("templates/cull.html").read())

@app.get("/admin/trash", response_class=HTMLResponse)
def trash_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/trash")
    return HTMLResponse(open("templates/trash.html").read())

@app.get("/admin/photographers", response_class=HTMLResponse)
def photographers_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/photographers")
    return HTMLResponse(open("templates/photographers.html").read())

@app.get("/admin/timecards", response_class=HTMLResponse)
def timecards_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/timecards")
    return HTMLResponse(open("templates/timecards.html").read())

# ── ZIP PRICING TEST PAGES ────────────────────────────────────────────────────

ZIP_LOCS_SET    = {"adventure zip", "adventure zip line", "adventure zipline", "nature zip", "nature zip line", "nature zipline"}
ZIP_PRICING_KEY = "meta/zip_pricing.json"
FOLDER_META_KEY = "meta/folder_meta.json"

def _load_zip_pricing():
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=ZIP_PRICING_KEY)
        return json.loads(obj["Body"].read())
    except:
        return {"tiers": []}

def _save_zip_pricing(d):
    s3.put_object(Bucket=R2_BUCKET, Key=ZIP_PRICING_KEY,
                  Body=json.dumps(d).encode(), ContentType="application/json")

_folder_meta_cache: dict = {"data": None, "ts": 0.0}
_FOLDER_META_TTL = 10  # seconds

def _load_folder_meta():
    import time
    now = time.monotonic()
    if _folder_meta_cache["data"] is not None and now - _folder_meta_cache["ts"] < _FOLDER_META_TTL:
        return _folder_meta_cache["data"]
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=FOLDER_META_KEY)
        d = json.loads(obj["Body"].read())
    except:
        d = {}
    _folder_meta_cache["data"] = d
    _folder_meta_cache["ts"] = now
    return d

def _save_folder_meta(d):
    s3.put_object(Bucket=R2_BUCKET, Key=FOLDER_META_KEY,
                  Body=json.dumps(d).encode(), ContentType="application/json")
    _folder_meta_cache["data"] = d
    import time; _folder_meta_cache["ts"] = time.monotonic()

def _folder_key(date, location, last_name):
    return f"{date}|{location}|{last_name}"

@app.get("/admin/zip-pricing")
def zip_pricing_page():
    return RedirectResponse("/admin/pricing#zip")

@app.get("/admin/zip-folders")
def zip_folders_page():
    return RedirectResponse("/admin/pricing#zip")

@app.get("/admin/zip-preview/{date}/{last_name}")
def zip_preview_page(date: str, last_name: str):
    return RedirectResponse("/admin/pricing#zip")

@app.get("/api/zip-pricing")
def api_get_zip_pricing(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return _load_zip_pricing()

@app.get("/api/viewer/zip-pricing")
def viewer_zip_pricing(request: Request):
    """Local-only: return zip pricing tiers for the viewer group size picker."""
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "Local only"})
    return _load_zip_pricing()

@app.post("/api/viewer/set-group-size")
async def viewer_set_group_size(request: Request):
    """Local-only: set zip group size for a folder so viewer shows correct pricing."""
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "Local only"})
    body = await request.json()
    fk         = body.get("folder_key")
    group_size = body.get("group_size")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    folder_meta = _load_folder_meta()
    if fk not in folder_meta:
        folder_meta[fk] = {}
    folder_meta[fk]["group_size"] = group_size
    _save_folder_meta(folder_meta)
    return {"status": "ok"}

@app.post("/api/zip-pricing")
async def api_save_zip_pricing(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    _save_zip_pricing(body)
    return {"status": "ok"}

@app.get("/api/zip-folders")
def api_get_zip_folders(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = _load_folder_meta()
    folders = {}
    for item in data:
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

@app.post("/api/zip-folder/set-group-size")
async def api_set_group_size(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk = body.get("folder_key")
    group_size = body.get("group_size")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    folder_meta = _load_folder_meta()
    if fk not in folder_meta:
        folder_meta[fk] = {}
    folder_meta[fk]["group_size"] = group_size
    _save_folder_meta(folder_meta)
    return {"status": "ok"}

@app.get("/admin/zip-compare")
def zip_compare_page():
    return RedirectResponse("/admin/pricing#zip")

@app.get("/api/zip-compare")
def api_zip_compare(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = _load_folder_meta()
    zip_pricing = _load_zip_pricing()
    tiers_list = zip_pricing.get("tiers", [])
    folders = {}
    for item in data:
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
            group_size = folder_meta.get(fk, {}).get("group_size")
            tier = next((t for t in tiers_list if str(t.get("people")) == str(group_size)), None) if group_size else None
            folders[fk] = {
                "folder_key": fk, "date": date, "location": location,
                "last_name": last_name, "photo_count": 0,
                "group_size": group_size, "tier": tier, "preview_paths": [],
            }
        folders[fk]["photo_count"] += 1
        if len(folders[fk]["preview_paths"]) < 4:
            folders[fk]["preview_paths"].append(item["path"])
    result = []
    for f in sorted(folders.values(), key=lambda x: (x["date"], x["last_name"]), reverse=True):
        previews = []
        for path in f.pop("preview_paths"):
            try:
                previews.append(_presigned_get(to_r2_key(path), expires=3600))
            except:
                pass
        result.append({**f, "previews": previews})
    return {"folders": result}

@app.get("/api/zip-preview/{date}/{last_name}")
def api_zip_preview(request: Request, date: str, last_name: str):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = _load_folder_meta()
    zip_pricing = _load_zip_pricing()
    photos = []
    location = ""
    for item in data:
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
            url = _presigned_get(r2_key, expires=3600)
            thumb_urls.append({"filename": p["filename"], "url": url})
        except:
            pass
    return {
        "date": date, "last_name": last_name, "location": location,
        "group_size": group_size, "tier": tier_data,
        "photo_count": len(photos), "photos": thumb_urls,
    }

# ── Admin Upload ──────────────────────────────────────────────────────────────

@app.post("/api/admin/upload/presign")
async def admin_upload_presign(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    files    = body.get("files", [])
    if not date or not location or not files:
        return JSONResponse(status_code=400, content={"error": "Missing date, location, or files"})
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    urls = []
    for f in files:
        filename = os.path.basename(f["name"])
        if folder_slug:
            key = f"images/{date}/{loc_slug}/{folder_slug}/{filename}"
        else:
            key = f"images/{date}/{loc_slug}/{filename}"
        presigned = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": f.get("type", "image/jpeg")},
            ExpiresIn=7200,
        )
        urls.append({"key": key, "url": presigned, "filename": filename})
    return {"urls": urls}

@app.post("/api/admin/upload/index")
async def admin_upload_index(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    keys     = body.get("keys", [])
    if not date or not location or not keys:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    is_portrait = location.lower() in PORTRAIT_LOCATIONS
    existing    = {item["path"] for item in data}
    added       = 0
    for key in keys:
        if key not in existing:
            data.append({
                "path":      key,
                "date":      date,
                "location":  location,
                "last_name": folder if is_portrait else "",
                "group":     "" if is_portrait else folder,
                "embedding": None,
                "draft":     True,
            })
            existing.add(key)
            added += 1
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(), ContentType="application/json")
    return {"indexed": added, "date": date, "location": location, "folder": folder}

@app.post("/api/admin/reindex-folder")
async def admin_reindex_folder(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    if not date or not location:
        return JSONResponse(status_code=400, content={"error": "Missing date or location"})
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    prefix = f"images/{date}/{loc_slug}/{folder_slug}/" if folder_slug else f"images/{date}/{loc_slug}/"
    is_portrait = location.lower() in PORTRAIT_LOCATIONS
    existing = {item["path"] for item in data}
    added = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            if key not in existing:
                data.append({
                    "path":      key,
                    "date":      date,
                    "location":  location,
                    "last_name": folder if is_portrait else "",
                    "group":     "" if is_portrait else folder,
                    "embedding": None,
                    "draft":     True,
                })
                existing.add(key)
                added += 1
    if added:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    return {"indexed": added, "prefix": prefix}

@app.post("/api/admin/push-live")
async def admin_push_live(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()

    # ── Pricing gate ─────────────────────────────────────────────────────────
    loc_lower = location.lower()
    if loc_lower in ZIP_LOCS_SET:
        fm = _load_folder_meta()
        fk = _folder_key(date, location, folder)
        group_size = fm.get(fk, {}).get("group_size")
        if not group_size:
            return JSONResponse(status_code=400, content={"error": "Set group size before pushing live"})
        zp = _load_zip_pricing()
        if not any(str(t.get("people")) == str(group_size) for t in zp.get("tiers", [])):
            return JSONResponse(status_code=400, content={"error": f"No zip pricing for {group_size} people — add it in Pricing"})
    else:
        _pr = _load_pricing()
        has_pricing = bool(_pr.get("default", {}).get("tiers")) or \
                      bool(_pr.get("activities", {}).get(location, {}).get("tiers"))
        if not has_pricing:
            return JSONResponse(status_code=400, content={"error": f"No pricing configured for {location} — set it up in Pricing"})

    pushed = 0
    for item in data:
        if (item.get("draft")
                and item["date"] == date
                and item["location"].strip().lower() == location.lower()
                and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()):
            del item["draft"]
            pushed += 1
    if pushed:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    return {"pushed": pushed}

@app.post("/api/admin/discard-draft")
async def admin_discard_draft(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    before   = len(data)
    data = [item for item in data if not (
        item.get("draft")
        and item["date"] == date
        and item["location"].strip().lower() == location.lower()
        and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()
    )]
    removed = before - len(data)
    if removed:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    return {"removed": removed}


@app.post("/api/admin/delete-folder")
async def admin_delete_folder(request: Request):
    """Move all photos in a folder to trash (7-day recovery window)."""
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()

    def _matches(item):
        return (
            item["date"] == date
            and item["location"].strip().lower() == location.lower()
            and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()
        )

    to_trash  = [item for item in data if _matches(item)]
    now       = datetime.now(timezone.utc)
    purge_at  = (now + timedelta(days=7)).isoformat()
    trash_meta = _load_trash_meta()
    trashed   = 0

    for item in to_trash:
        key       = to_r2_key(item["path"])
        uid       = str(uuid.uuid4())[:8]
        trash_key = f"studio_trash/{uid}_{os.path.basename(item['path'])}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": key},
                           Key=trash_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=key)
        except Exception as e:
            print(f"Folder trash failed {key}: {e}")
            continue
        trash_meta.append({
            "key":          trash_key,
            "original_key": key,
            "filename":     os.path.basename(item["path"]),
            "date":         item["date"],
            "location":     item["location"],
            "folder":       (item.get("last_name") or item.get("group") or "").strip(),
            "trashed_at":   now.isoformat(),
            "purge_at":     purge_at,
            "type":         "studio",
            "image_entry":  item,
        })
        trashed += 1

    data = [item for item in data if not _matches(item)]
    if trashed:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    _save_trash_meta(trash_meta)

    # Clean up folder_meta
    try:
        fm = _load_folder_meta()
        fk = _folder_key(date, location, folder)
        if fk in fm:
            del fm[fk]
            _save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    return {"deleted": trashed, "removed_index": len(to_trash)}


# ── Admin Folders (phase 2) ──────────────────────────────────────────────────

_KNOWN_LOCATIONS = [
    "Adventure Zip", "Adventure Zip Line", "Nature Zip Line",
    "Mountain Biking", "Lone Peak Portraits", "Explorer Gondola", "Ramcharger Portraits",
]
_SLUG_TO_LOC = {loc.lower().replace(" ", "-"): loc for loc in _KNOWN_LOCATIONS}

@app.get("/admin/folders", response_class=HTMLResponse)
def admin_folders_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/folders")
    return HTMLResponse(open("templates/admin_folders.html").read())

@app.get("/api/admin/folders")
def api_admin_folders(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = _load_folder_meta()
    folders = {}
    for item in data:
        date     = item.get("date", "")
        location = item.get("location", "").strip()
        name     = (item.get("last_name") or item.get("group") or "").strip()
        if not date or not location:
            continue
        fk = _folder_key(date, location, name)
        if fk not in folders:
            loc_lower = location.lower()
            is_zip = loc_lower in ZIP_LOCS_SET
            folders[fk] = {
                "folder_key":  fk,
                "date":        date,
                "location":    location,
                "name":        name,
                "photo_count": 0,
                "draft_count": 0,
                "is_zip":      is_zip,
                "group_size":  folder_meta.get(fk, {}).get("group_size") if is_zip else None,
                "_preview":    None,
            }
        folders[fk]["photo_count"] += 1
        if item.get("draft"):
            folders[fk]["draft_count"] += 1
        if folders[fk]["_preview"] is None:
            folders[fk]["_preview"] = item.get("path")
    from urllib.parse import quote as _quote
    result = []
    for f in sorted(folders.values(), key=lambda x: (x["date"], x["location"], x["name"]), reverse=True):
        preview_path = f.pop("_preview", None)
        f["preview_url"] = f"/api/admin/photo?path={_quote(preview_path)}&size=thumb" if preview_path else ""
        result.append(f)
    return {"folders": result}

@app.get("/api/admin/r2-scan")
def api_admin_r2_scan(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    indexed_paths = {item["path"] for item in data}
    unindexed = {}
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="images/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    continue
                if key in indexed_paths:
                    continue
                parts = key.split("/")
                if len(parts) < 4:
                    continue
                date     = parts[1]
                loc_slug = parts[2]
                folder_slug = parts[3] if len(parts) >= 5 else ""
                fk = f"{date}|{loc_slug}|{folder_slug}"
                if fk not in unindexed:
                    location    = _SLUG_TO_LOC.get(loc_slug, loc_slug.replace("-", " ").title())
                    folder_name = folder_slug.replace("-", " ").title() if folder_slug else ""
                    unindexed[fk] = {
                        "folder_key":  fk,
                        "date":        date,
                        "location":    location,
                        "name":        folder_name,
                        "loc_slug":    loc_slug,
                        "folder_slug": folder_slug,
                        "photo_count": 0,
                    }
                unindexed[fk]["photo_count"] += 1
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"folders": list(unindexed.values())}


# ── Admin Studio (unified page) ──────────────────────────────────────────────

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/dashboard")
    return HTMLResponse(open("templates/admin_studio.html").read())

@app.get("/admin/studio")
def admin_studio_redirect():
    return RedirectResponse("/admin/dashboard")

@app.get("/api/admin/folder/photos")
def api_admin_folder_photos(
    request: Request,
    date: str = Query(...),
    location: str = Query(...),
    folder: str = Query(""),
):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    loc_lower    = location.strip().lower()
    folder_lower = folder.strip().lower()
    photos = []
    for item in data:
        if item["date"] != date:
            continue
        if item["location"].strip().lower() != loc_lower:
            continue
        item_name = (item.get("last_name") or item.get("group") or "").strip().lower()
        if item_name != folder_lower:
            continue
        from urllib.parse import quote as _quote
        photos.append({
            "path":     item["path"],
            "filename": os.path.basename(item["path"]),
            "is_draft": bool(item.get("draft")),
            "url":      f"/api/admin/photo?path={_quote(item['path'])}&size=medium",
        })
    photos.sort(key=lambda x: natural_sort_key(x["path"]))
    live_count  = sum(1 for p in photos if not p["is_draft"])
    draft_count = sum(1 for p in photos if p["is_draft"])
    fk = _folder_key(date, location.strip(), folder.strip())
    fm = _load_folder_meta()
    return {
        "photos":      photos,
        "total":       len(photos),
        "live_count":  live_count,
        "draft_count": draft_count,
        "folder_key":  fk,
        "group_size":  fm.get(fk, {}).get("group_size"),
    }

@app.post("/api/admin/photo/trash")
async def admin_photo_trash(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body       = await request.json()
    paths      = body.get("paths", [])
    if not paths:
        return JSONResponse(status_code=400, content={"error": "No paths"})
    paths_set  = set(paths)
    to_trash   = [item for item in data if item["path"] in paths_set]
    now        = datetime.now(timezone.utc)
    purge_at   = (now + timedelta(days=7)).isoformat()
    trash_meta = _load_trash_meta()
    trashed    = 0
    for item in to_trash:
        key       = to_r2_key(item["path"])
        uid       = str(uuid.uuid4())[:8]
        trash_key = f"studio_trash/{uid}_{os.path.basename(item['path'])}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": key},
                           Key=trash_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=key)
        except Exception as e:
            print(f"Photo trash failed {key}: {e}")
            continue
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=_thumb_r2_key(key))
        except Exception:
            pass
        trash_meta.append({
            "key":          trash_key,
            "original_key": key,
            "filename":     os.path.basename(item["path"]),
            "date":         item["date"],
            "location":     item["location"],
            "folder":       (item.get("last_name") or item.get("group") or "").strip(),
            "trashed_at":   now.isoformat(),
            "purge_at":     purge_at,
            "type":         "studio",
            "image_entry":  item,
        })
        trashed += 1
    data = [item for item in data if item["path"] not in paths_set]
    if trashed:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    _save_trash_meta(trash_meta)
    return {"trashed": trashed}

@app.post("/api/admin/pull-draft")
async def admin_pull_draft(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    pulled   = 0
    for item in data:
        if (not item.get("draft")
                and item["date"] == date
                and item["location"].strip().lower() == location.lower()
                and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()):
            item["draft"] = True
            pulled += 1
    if pulled:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    return {"pulled": pulled}

@app.post("/api/admin/folder/move")
async def admin_folder_move(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body         = await request.json()
    date         = body.get("date", "")
    location     = body.get("location", "").strip()
    folder       = body.get("folder", "").strip()
    new_date     = body.get("new_date", "").strip()
    new_location = body.get("new_location", "").strip()
    if not new_date or not new_location:
        return JSONResponse(status_code=400, content={"error": "Missing new_date or new_location"})
    loc_lower    = location.lower()
    folder_lower = folder.lower()
    to_move = [item for item in data
               if item["date"] == date
               and item["location"].strip().lower() == loc_lower
               and (item.get("last_name","") or item.get("group","")).strip().lower() == folder_lower]
    if not to_move:
        return JSONResponse(status_code=404, content={"error": "No photos found"})
    new_loc_slug = new_location.lower().replace(" ", "-")
    folder_slug  = folder.lower().replace(" ", "-") if folder else ""
    is_portrait  = new_location.lower() in PORTRAIT_LOCATIONS
    path_remap   = {}
    from concurrent.futures import ThreadPoolExecutor
    def move_one(item):
        old_key  = to_r2_key(item["path"])
        filename = os.path.basename(item["path"])
        new_key  = (f"images/{new_date}/{new_loc_slug}/{folder_slug}/{filename}"
                    if folder_slug else f"images/{new_date}/{new_loc_slug}/{filename}")
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": old_key}, Key=new_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=old_key)
            try:
                s3.copy_object(Bucket=R2_BUCKET,
                               CopySource={"Bucket": R2_BUCKET, "Key": _thumb_r2_key(old_key)},
                               Key=_thumb_r2_key(new_key))
                s3.delete_object(Bucket=R2_BUCKET, Key=_thumb_r2_key(old_key))
            except Exception:
                pass
            return (item["path"], new_key)
        except Exception as e:
            print(f"Move failed {old_key}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(move_one, to_move))
    for r in results:
        if r:
            path_remap[r[0]] = r[1]
    for item in data:
        if item["path"] in path_remap:
            item["path"]     = path_remap[item["path"]]
            item["date"]     = new_date
            item["location"] = new_location
            if is_portrait:
                item["last_name"] = item.get("last_name") or folder
                item["group"]     = ""
            else:
                item["group"]     = item.get("group") or folder
                item["last_name"] = ""
    if path_remap:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    try:
        fm     = _load_folder_meta()
        old_fk = _folder_key(date, location, folder)
        new_fk = _folder_key(new_date, new_location, folder)
        if old_fk in fm:
            fm[new_fk] = fm.pop(old_fk)
            _save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta move error: {e}")
    return {"moved": len(path_remap), "new_date": new_date, "new_location": new_location}

@app.post("/api/admin/folder/rename")
async def admin_folder_rename(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body       = await request.json()
    date       = body.get("date", "")
    location   = body.get("location", "").strip()
    folder     = body.get("folder", "").strip()
    new_folder = body.get("new_folder", "").strip()
    if not new_folder:
        return JSONResponse(status_code=400, content={"error": "Missing new_folder"})
    loc_lower    = location.lower()
    folder_lower = folder.lower()
    loc_slug     = location.lower().replace(" ", "-")
    new_slug     = new_folder.lower().replace(" ", "-")
    is_portrait  = location.lower() in PORTRAIT_LOCATIONS
    to_rename = [item for item in data
                 if item["date"] == date
                 and item["location"].strip().lower() == loc_lower
                 and (item.get("last_name","") or item.get("group","")).strip().lower() == folder_lower]
    if not to_rename:
        return JSONResponse(status_code=404, content={"error": "No photos found"})
    path_remap = {}
    from concurrent.futures import ThreadPoolExecutor
    def rename_one(item):
        old_key  = to_r2_key(item["path"])
        filename = os.path.basename(item["path"])
        new_key  = f"images/{date}/{loc_slug}/{new_slug}/{filename}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": old_key}, Key=new_key)
            # Verify the copy actually landed before touching the original.
            # R2 can silently drop concurrent copy operations under load.
            s3.head_object(Bucket=R2_BUCKET, Key=new_key)
            s3.delete_object(Bucket=R2_BUCKET, Key=old_key)
            try:
                s3.copy_object(Bucket=R2_BUCKET,
                               CopySource={"Bucket": R2_BUCKET, "Key": _thumb_r2_key(old_key)},
                               Key=_thumb_r2_key(new_key))
                s3.delete_object(Bucket=R2_BUCKET, Key=_thumb_r2_key(old_key))
            except Exception:
                pass
            return (item["path"], new_key)
        except Exception as e:
            print(f"Rename failed {old_key}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(rename_one, to_rename))
    for r in results:
        if r:
            path_remap[r[0]] = r[1]
    for item in data:
        if item["path"] in path_remap:
            item["path"] = path_remap[item["path"]]
            if is_portrait:
                item["last_name"] = new_folder
            else:
                item["group"] = new_folder
    if path_remap:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    try:
        fm     = _load_folder_meta()
        old_fk = _folder_key(date, location, folder)
        new_fk = _folder_key(date, location, new_folder)
        if old_fk in fm:
            fm[new_fk] = fm.pop(old_fk)
            _save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta rename error: {e}")
    return {"renamed": len(path_remap), "new_folder": new_folder}


# ── PRINT PRICING ─────────────────────────────────────────────────────────────

PRINT_PRICING_KEY = "meta/print_pricing.json"

_DEFAULT_PRINT_SIZES = [
    {"label": '5×7"',  "price": 25, "ship_print": 8,  "ship_framed": 15, "size_idx": 0},
    {"label": '8×10"', "price": 30, "ship_print": 8,  "ship_framed": 15, "size_idx": 1},
    {"label": '10×13"',"price": 45, "ship_print": 13, "ship_framed": 25, "size_idx": 2},
    {"label": '16×20"',"price": 90, "ship_print": 13, "ship_framed": 50, "size_idx": 3},
]

def _load_print_pricing():
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=PRINT_PRICING_KEY)
        return json.loads(obj["Body"].read())
    except Exception:
        return {"sizes": list(_DEFAULT_PRINT_SIZES), "free_ship_threshold": 3}

def _save_print_pricing(d):
    s3.put_object(Bucket=R2_BUCKET, Key=PRINT_PRICING_KEY,
                  Body=json.dumps(d).encode(), ContentType="application/json")

@app.get("/api/print-pricing")
def api_print_pricing():
    return _load_print_pricing()

@app.post("/api/admin/print-pricing")
async def api_save_print_pricing(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    _save_print_pricing(body)
    return {"status": "ok"}

@app.get("/api/admin/pricing")
def api_admin_pricing_get(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return _load_pricing()

@app.post("/api/admin/pricing")
async def api_admin_pricing_save(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Invalid pricing data"})
    _save_pricing(body)
    return {"status": "ok"}

@app.get("/admin/pricing", response_class=HTMLResponse)
def admin_pricing_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/pricing")
    return HTMLResponse(open("templates/admin_pricing.html").read())
