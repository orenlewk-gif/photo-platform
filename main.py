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

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
)
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "crystal-images")

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

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
        ln = item.get("last_name", "").strip()
        if not ln:
            continue
        ln_lower = ln.lower()
        # Prefix match first; fall back to fuzzy
        if not ln_lower.startswith(q_lower) and fuzz.partial_ratio(q_lower, ln_lower) < 75:
            continue
        loc = clean_location(item["location"])
        key = (ln_lower, item["date"], loc)
        if key not in seen:
            seen.add(key)
            results.append({
                "last_name": ln,
                "date":      item["date"],
                "location":  loc,
            })

    # Exact/prefix matches first, then alphabetical, then by date
    results.sort(key=lambda x: (
        not x["last_name"].lower().startswith(q_lower),
        x["last_name"].lower(),
        x["date"],
    ))
    return {"results": results[:20]}


def natural_sort_key(path):
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]

@app.get("/api/browse")
def browse(date: str, location: str, family: str = Query(None)):
    pool = [item for item in data
            if item["date"] == date
            and clean_location(item["location"]) == location
            and (not family or item.get("last_name","").strip().lower() == family.lower())]
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
):
    try:
        return _search(query, last_name, date, location)
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(e)})

def _search(query, last_name, date, location):
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
    if location and location in pricing.get("activities", {}):
        return pricing["activities"][location]
    return pricing.get("default", default)


def to_r2_key(path: str) -> str:
    # Convert absolute local path to R2 key (relative, starting from 'images/')
    idx = path.find("images/")
    return path[idx:] if idx >= 0 else path

@app.get("/api/photo")
def get_photo(path: str, size: str = Query("medium")):
    """
    size=thumb  → 450px  q72  (gallery thumbnails)
    size=medium → 1200px q85  (lightbox — default)
    size=full   → 1800px q88  (download-quality)
    """
    from fastapi.responses import StreamingResponse as SR
    SIZE_MAP = {
        "thumb":  (1000, 85),
        "medium": (1800, 90),
        "full":   (2400, 92),
    }
    max_px, quality = SIZE_MAP.get(size, SIZE_MAP["medium"])
    cache_secs = 86400 if size == "thumb" else 3600

    try:
        key = to_r2_key(path)
        if os.getenv("R2_ENDPOINT_URL"):
            obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
            img = Image.open(obj["Body"]).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            img = apply_watermark(img, size)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            buf.seek(0)
            return SR(buf, media_type="image/jpeg",
                      headers={"Cache-Control": f"private, max-age={cache_secs}"})
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


@app.post("/api/checkout")
async def create_checkout(request: Request):
    try:
        body = await request.json()

        digital_count = body.get("digital_count", 0)
        digital_price = body.get("digital_price", 0)
        filenames     = body.get("digital_filenames", [])
        prints        = body.get("prints", [])
        frames        = body.get("frames", [])
        location      = body.get("location", "")
        date          = body.get("date", "")

        line_items = []
        fee_lines  = []

        if digital_count > 0:
            price_str = str(float(digital_price))
            # Individual filenames as meta so they show on the order
            file_meta = [{"key": f"File {i+1}", "value": fn}
                         for i, fn in enumerate(filenames)]
            line_items.append({
                "product_id": WC_DIGITAL_PRODUCT_ID,
                "quantity":   digital_count,
                "name":       f"Digital Photos — {location}",
                "subtotal":   price_str,
                "total":      price_str,
                "meta_data":  file_meta,
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
        FRAME_SHIP = [20, 25, 30, 65]

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

        paths = body.get("digital_paths", [])
        meta = [
            {"key": "_photo_location", "value": location},
            {"key": "_photo_date",     "value": date},
            {"key": "_photo_files",    "value": ", ".join(filenames)},
            {"key": "_photo_paths",    "value": "|".join(paths)},
        ]

        customer_email = body.get("email", "")

        order_data = {
            "status":     "pending",
            "line_items": line_items,
            "fee_lines":  fee_lines,
            "meta_data":  meta,
            "billing":    {"email": customer_email},
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

    # Only act when order is completed
    if order.get("status") != "completed":
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
        f"Hi {customer_name or 'there'}! Your Crystal Images photos are ready.\n\n"
        f"Click the link below to access your download page:\n{download_url}\n\n"
        f"Your download link is valid for {DOWNLOAD_EXPIRE_DAYS} days. "
        f"If it expires, just contact us and we'll resend your photos anytime."
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
      contact us anytime at bigskyphotos.com to resend.
    </div>
  </div>

  <div class="card">
    <div class="license-title">⬇ Download Your Photos ({len(rec['paths'])} files)</div>
    {photo_rows}
  </div>
</div></body></html>"""
    return HTMLResponse(html)


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
# FRONTEND
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("templates/index.html").read())
