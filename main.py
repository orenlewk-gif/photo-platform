import threading
import time as _time
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from html import escape as _html_escape
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import json
import re
import hmac
import hashlib
import ipaddress
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import base64
from io import BytesIO
from PIL import Image
import boto3
import requests as http_requests
from core.images import (admin_thumb_r2_key as _admin_thumb_r2_key, fix_orientation,
                         grid_thumb_r2_key as _grid_thumb_r2_key, nowm_thumb_r2_key as _nowm_thumb_r2_key,
                         nowm_large_r2_key as _nowm_large_r2_key,
                         thumb_r2_key as _thumb_r2_key)
from core.checkout import (CATEGORIES, CheckoutError, activity_groups, bundle_for_cart, check_coupon,
                           coupon_item_discount, coupon_scope, parse_int, shipping_for)
from core.classify import MODEL_LABEL, ColorModel, open_for_model
from core.index import build_folder_index as _build_folder_index
from core.pricing import best_tier_price, pricing_for, selection_limit
from core.search import filter_by_labels, match_last_name
from core.storage import JsonDoc
from core.text import (clean_location, folder_key as _folder_key, item_group_from_path as _item_group_from_path,
                       natural_sort_key, to_r2_key)
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://bigskyphotos.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

if os.path.exists("frames"):
    app.mount("/frames", StaticFiles(directory="frames"), name="frames")

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─────────────────────────────────────────
# R2 CLIENT
# ─────────────────────────────────────────

from botocore.config import Config as BotocoreConfig
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    config=BotocoreConfig(signature_version="s3v4", max_pool_connections=50),
)
R2_BUCKET          = os.getenv("R2_BUCKET_NAME", "crystal-images")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
PRICING_KEY  = "meta/pricing.json"

# ── Model file bootstrap — download from R2 on cold start if missing ──────────
def _ensure_model_files():
    files = [f"{MODEL_LABEL}.onnx", f"{MODEL_LABEL}.onnx.data", f"{MODEL_LABEL}_meta.json"]
    missing = [f for f in files if not os.path.exists(f)]
    if not missing:
        return
    print(f"Downloading model files from R2: {missing}")
    for fname in missing:
        try:
            s3.download_file(R2_BUCKET, f"models/{fname}", fname)
            print(f"  {fname} ready")
        except Exception as e:
            print(f"  {fname} download failed: {e}")

_ensure_model_files()

# ── Activity Pricing (R2) ─────────────────────────────────────────────────────
_PRICING_TTL = 30  # seconds
_pricing_doc = JsonDoc(s3, R2_BUCKET, PRICING_KEY, ttl=_PRICING_TTL, indent=2,
                       default=lambda: {"default": {"tiers": []}, "activities": {}, "combos": []},
                       valid=lambda d: isinstance(d, dict))
_pricing_cache = _pricing_doc.cache

def _load_pricing():  return _pricing_doc.load()
def _save_pricing(d): _pricing_doc.save(d)

# Which websites may use the photo bucket from a browser (the studio's direct uploads,
# "Save to Photos" on download pages) is set in the Cloudflare dashboard (R2 >
# crystal-images > Settings > CORS policy): only https://photos.bigskyphotos.com, for
# GET / PUT / HEAD. The app's storage key deliberately can't change bucket settings,
# so the app doesn't try (it used to attempt "any website" on every start and fail).

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

# Locations that use last-name sub-folders (portrait style) — base set, extended by R2 config
_PORTRAIT_LOCATIONS_BASE = {"lone peak portraits", "explorer gondola", "ramcharger portraits", "adventure zip line", "adventure zip", "nature zip line", "nature zipline", "nature zip"}
PORTRAIT_LOCATIONS = set(_PORTRAIT_LOCATIONS_BASE)  # live copy, refreshed from R2

# Locations whose group/sub-folder names are searchable

PORTRAIT_LOCATIONS_KEY = "meta/portrait_locations.json"
_portrait_locs_cache: dict = {"data": None, "ts": 0.0}
_PORTRAIT_LOCS_TTL = 60

def _load_portrait_locations():
    import time
    global PORTRAIT_LOCATIONS
    now = time.monotonic()
    if _portrait_locs_cache["data"] is not None and now - _portrait_locs_cache["ts"] < _PORTRAIT_LOCS_TTL:
        return _portrait_locs_cache["data"]
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=PORTRAIT_LOCATIONS_KEY)
        locs = [l.lower().strip() for l in json.loads(obj["Body"].read()) if str(l).strip()]
    except Exception:
        locs = []
    if locs:
        PORTRAIT_LOCATIONS = set(locs)
    else:
        # No file yet — seed from base set and persist so admin can edit
        PORTRAIT_LOCATIONS = set(_PORTRAIT_LOCATIONS_BASE)
        _save_portrait_locations(sorted(PORTRAIT_LOCATIONS))
    _portrait_locs_cache["data"] = sorted(PORTRAIT_LOCATIONS)
    _portrait_locs_cache["ts"] = now
    return _portrait_locs_cache["data"]

def _save_portrait_locations(locs: list):
    s3.put_object(Bucket=R2_BUCKET, Key=PORTRAIT_LOCATIONS_KEY,
                  Body=json.dumps(locs, indent=2).encode(), ContentType="application/json")
    _portrait_locs_cache["data"] = None  # invalidate so next load re-reads from R2
    _portrait_locs_cache["data"] = None  # bust cache

# ── XMP Color Label Reader ─────────────────────────────────────────────────────
def read_xmp_color_label(jpeg_bytes: bytes) -> str:
    """Extract Lightroom/macOS XMP color label from JPEG bytes (no external deps)."""
    # Try both null-terminated and space-terminated XMP namespace markers
    for marker in (b'http://ns.adobe.com/xap/1.0/\x00', b'http://ns.adobe.com/xap/1.0/ '):
        idx = jpeg_bytes.find(marker)
        if idx == -1:
            continue
        xmp_data = jpeg_bytes[idx + len(marker):]
        m = re.search(rb'xmp:Label\s*=\s*"([^"]*)"', xmp_data)
        if m:
            return m.group(1).decode("utf-8", errors="replace").strip()
        m = re.search(rb'<xmp:Label>\s*([^<]*?)\s*</xmp:Label>', xmp_data)
        if m:
            return m.group(1).decode("utf-8", errors="replace").strip()
    return ""

# ── Seasons ────────────────────────────────────────────────────────────────────
SEASONS_KEY = "meta/seasons.json"
_SEASONS_TTL = 60
_seasons_doc = JsonDoc(s3, R2_BUCKET, SEASONS_KEY, ttl=_SEASONS_TTL, indent=2, default=list)
_seasons_cache = _seasons_doc.cache

def _load_seasons():  return _seasons_doc.load()
def _save_seasons(d): _seasons_doc.save(d)

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

# The faint logos the gallery used to draw over every thumbnail (CSS .watermark: the
# transparent logo at 60% / 42% / 38% of the width, at center / 12% 18% / 88% 82%, 30% opacity).
# Baked into gallery thumbnails so a saved file looks exactly like the tile on screen.
_THUMB_OVERLAY = ((0.60, 0.50, 0.50), (0.42, 0.12, 0.18), (0.38, 0.88, 0.82))
_THUMB_OVERLAY_OPACITY = 0.30
_overlay_logo_cache = {}


def apply_thumb_overlay(img):
    logo = _overlay_logo_cache.get("logo")
    if logo is None:
        try:
            logo = Image.open("static/watermark-transparent.png").convert("RGBA")
        except Exception:
            return img
        _overlay_logo_cache["logo"] = logo
    W, H = img.size
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for frac, px, py in _THUMB_OVERLAY:
        w = max(1, round(W * frac))
        h = max(1, round(logo.height * w / logo.width))
        mark = logo.resize((w, h), Image.LANCZOS)
        layer.alpha_composite(mark, (round((W - w) * px), round((H - h) * py)))
    layer.putalpha(layer.getchannel("A").point(lambda a: int(a * _THUMB_OVERLAY_OPACITY)))
    out = img.convert("RGBA")
    out.alpha_composite(layer)
    return out.convert("RGB")


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
        # Tiled grid — every region of the photo has a watermark so no clean crop is possible
        wm_size   = int(min(img.width, img.height) * 0.28)
        step_x    = wm_size          # tight packing, no visible gaps
        step_y    = wm_size
        wm_scaled = wm.resize((wm_size, wm_size), Image.LANCZOS)
        row = 0
        y   = -(wm_size // 4)        # start slightly above edge
        while y < img.height + wm_size // 2:
            # Stagger alternate rows so there are no diagonal clear lanes
            x = -(wm_size // 4) + (step_x // 2 if row % 2 else 0)
            while x < img.width + wm_size // 2:
                try:
                    img_rgba.paste(wm_scaled, (int(x), int(y)), wm_scaled)
                except Exception:
                    pass
                x += step_x
            y   += step_y
            row += 1

    return img_rgba.convert("RGB")

# ─────────────────────────────────────────
# LOAD DATA (once on startup)
# ─────────────────────────────────────────

# Background jobs (classification, thumbnails): progress shown in the studio
_embed_jobs: dict = {}  # job_id -> {"total": int, "done": int, "finished": bool}

# ── Color labels ──────────────────────────────────────────────────────────────


# ── Labeling model ────────────────────────────────────────────────────────────
_color_model = ColorModel(f"{MODEL_LABEL}.onnx", f"{MODEL_LABEL}_meta.json")


def _load_onnx_model() -> bool:
    return _color_model.load()


def classify_photo_colors(img_bytes: bytes) -> dict | None:
    """Label sport, jacket and pants colors with the ONNX model (v3)."""
    return _color_model.classify(img_bytes)


def _apply_labels(item: dict, colors: dict | None) -> None:
    if colors:
        item["sport"]         = colors.get("sport", "unclear")
        item["jacket_colors"] = colors.get("jacket_colors", [])
        item["pants_colors"]  = colors.get("pants_colors", [])
        item["classifier"]    = MODEL_LABEL
        print(f"{MODEL_LABEL}: {item['path'].split('/')[-1]} → sport={item['sport']} jacket={item['jacket_colors']} pants={item['pants_colors']}")


def _needs_labels(item: dict) -> bool:
    """A winter action (non-portrait) photo the labeling model hasn't labeled yet."""
    return item.get("jacket_colors") is None and not _is_portrait_location(item.get("location", ""))


# Newly uploaded photos are labeled while their thumbnails are made (one download of each
# original). A labeling job that reaches a photo still being handled there waits for it
# instead of downloading it again.
_label_waits: dict = {}          # photo path -> threading.Event, set once its upload work is done
_LABEL_WAIT_SECONDS = 600


def _label_photos(items: list, job_id: str = None, needs=None, save_every: int = 0) -> int:
    """Run the labeling model over photos, 8 at a time: download each original, label it,
    stamp the model version. `needs(item)` False skips a photo without downloading it.
    Progress shows in _embed_jobs[job_id]; save_every=N saves the photo list every N photos
    so a long job keeps its progress through a restart. Returns how many were handled."""
    def one(item):
        pending = _label_waits.get(item["path"])
        if pending:
            pending.wait(_LABEL_WAIT_SECONDS)
        if needs and not needs(item):
            return True
        try:
            raw = s3.get_object(Bucket=R2_BUCKET, Key=item["path"])["Body"].read()
            _apply_labels(item, classify_photo_colors(raw))
            return True
        except Exception as e:
            print(f"Labeling failed {item['path']}: {e}")
            return False

    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, ok in enumerate(ex.map(one, items), 1):
            done += ok
            if job_id and job_id in _embed_jobs:
                _embed_jobs[job_id]["done"] = done
            if save_every and i % save_every == 0:
                _save_photo_list()
    return done


def embed_items_fast(items: list, job_id: str = None) -> int:
    """Label newly published photos (portraits and already-labeled photos are skipped)."""
    count = _label_photos(items, job_id, needs=_needs_labels)
    if job_id and job_id in _embed_jobs:
        _embed_jobs[job_id]["finished"] = True
    return count


data: list = []
_data_ready = threading.Event()
_photo_list_save_lock = threading.Lock()


def _save_photo_list() -> None:
    """Save the photo list (images.json) to storage. Saves take turns, and each writes the
    list as it is when its turn comes, so a slow save that started before a delete or move
    can never land after it and bring old photos back."""
    with _photo_list_save_lock:
        for attempt in range(3):
            try:
                body = json.dumps(list(data)).encode()
                break
            except RuntimeError:          # a photo record changed while it was being written out
                if attempt == 2:
                    raise
                _time.sleep(0.05)
        s3.put_object(Bucket=R2_BUCKET, Key="images.json", Body=body, ContentType="application/json")


_SAVE_SOON_DELAY = 1.0
_save_soon = {"pending": False}
_save_soon_lock = threading.Lock()


def _save_photo_list_soon() -> None:
    """Save the photo list shortly, in the background. Quick bursts of edits (label
    corrections) share one save instead of each writing the whole list."""
    with _save_soon_lock:
        if _save_soon["pending"]:
            return                        # a save is already on its way and will include this edit
        _save_soon["pending"] = True

    def run():
        _time.sleep(_SAVE_SOON_DELAY)
        with _save_soon_lock:
            _save_soon["pending"] = False # edits from here on schedule the next save
        try:
            _save_photo_list()
        except Exception as e:
            print(f"[photo list] background save failed: {e}")

    threading.Thread(target=run, daemon=True).start()


def _drop_legacy_embeddings(items: list) -> int:
    """Remove the old CLIP 'embedding' vectors (512 numbers per photo, written by the
    retired indexers). Nothing reads them — search uses the ONNX color labels — and
    they made up ~90% of images.json. Returns how many records had one."""
    n = 0
    for it in items:
        if "embedding" in it:
            del it["embedding"]
            n += 1
    return n

def _load_data_bg() -> None:
    global data
    # Always from storage: a stray local images.json (e.g. an old copy in the folder) used
    # to be loaded instead, and the next save would have written it over the real index.
    print("Loading images.json from R2...")
    # Keep trying until the index loads. Starting with an empty list after a failed
    # read would let the first save overwrite the real index in R2. Only a missing
    # file (a brand-new bucket) means "no photos yet".
    attempt = 0
    while True:
        attempt += 1
        try:
            obj = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
            data = json.loads(obj["Body"].read().decode("utf-8"))
            print(f"Loaded {len(data)} photos from R2.")
            break
        except s3.exceptions.NoSuchKey:
            print("No images.json in R2 yet — starting with an empty photo list.")
            data = []
            break
        except Exception as e:
            wait = min(30, 2 * attempt)
            print(f"WARNING: could not load images.json (attempt {attempt}: {e}) — retrying in {wait}s")
            _time.sleep(wait)
    # One-time cleanup: drop the unused CLIP vectors and save the slimmer index before
    # taking requests (handlers are held off until _data_ready, so nothing races this save).
    dropped = _drop_legacy_embeddings(data)
    if dropped:
        try:
            _save_photo_list()
            print(f"Removed unused CLIP embeddings from {dropped} photos; saved slimmer images.json.")
        except Exception as e:
            print(f"WARNING: could not save slimmer images.json ({e}) — will be saved on the next change.")
    # Backfill classifier field on records that were classified before tracking was added
    backfilled = 0
    for item in data:
        if item.get("jacket_colors") is not None and "classifier" not in item:
            item["classifier"] = "haiku"
            backfilled += 1
    if backfilled:
        print(f"Backfilled classifier='haiku' on {backfilled} existing records.")
    _data_ready.set()

threading.Thread(target=_load_data_bg, daemon=True).start()
# Wait up to 8 s so the first request isn't served with empty data,
# but don't block Railway's health check indefinitely.
_data_ready.wait(timeout=8)

# Until the photo index has loaded, answer with "starting up" instead of serving
# empty galleries or letting anything save over the real index. Customer download
# links, the WooCommerce webhook, static files and the health check still work.
_STARTUP_ALLOWED = ("/healthz", "/webhook", "/download/", "/package/", "/static/", "/frames/")
_STARTUP_PAGE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<title>Starting up — Crystal Images</title></head>
<body style="margin:0;background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:2rem">
<div><h2 style="color:#F2C94C;margin-bottom:.5rem">Just a moment</h2>
<p style="color:rgba(255,255,255,.6)">The site is starting up. This page will refresh in a few seconds.</p></div>
</body></html>"""

@app.middleware("http")
async def _wait_for_photo_index(request: Request, call_next):
    if not _data_ready.is_set() and not request.url.path.startswith(_STARTUP_ALLOWED):
        headers = {"Retry-After": "5"}
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=503, headers=headers,
                                content={"error": "Starting up — try again in a few seconds"})
        return HTMLResponse(_STARTUP_PAGE, status_code=503, headers=headers)
    return await call_next(request)

@app.get("/healthz")
def healthz():
    """Railway health check: healthy only once the photo index has loaded."""
    if not _data_ready.is_set():
        return JSONResponse(status_code=503, content={"status": "loading"})
    return {"status": "ok", "photos": len(data)}

_storage_check = {"ok": None, "ts": 0.0, "error": ""}
_storage_check_lock = threading.Lock()


@app.get("/healthz/storage")
def healthz_storage():
    """For the uptime monitor: can the site actually reach photo storage? (The home page keeps
    loading even when storage can't be read, so /healthz alone can't tell.) 503 when it can't.
    The answer is reused for 20 seconds so frequent checks don't add load."""
    if not _data_ready.is_set():
        return JSONResponse(status_code=503, content={"status": "loading"})
    with _storage_check_lock:
        if _time.monotonic() - _storage_check["ts"] > 20:
            try:
                s3.head_object(Bucket=R2_BUCKET, Key="images.json")
                _storage_check.update(ok=True, error="")
            except Exception as e:
                code = str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) or type(e).__name__
                _storage_check.update(ok=False, error=code)
            _storage_check["ts"] = _time.monotonic()
        if _storage_check["ok"]:
            return {"status": "ok", "storage": "reachable"}
        return JSONResponse(status_code=503, content={"status": "error", "storage": "unreachable",
                                                      "reason": _storage_check["error"]})


# Pre-load portrait locations from R2 so PORTRAIT_LOCATIONS is current on startup
try:
    _load_portrait_locations()
except Exception:
    pass

# ── Dimension cache ──────────────────────────────────────────────────────────
_dim_cache: dict = {}

def _load_dim_cache() -> None:
    global _dim_cache
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key="metadata/dimensions.json")
        _dim_cache = json.loads(obj["Body"].read())
        print(f"Dim cache: {len(_dim_cache)} entries")
    except Exception:
        pass  # empty cache is fine — misses are fetched on demand

def _save_dim_cache_bg() -> None:
    try:
        s3.put_object(Bucket=R2_BUCKET, Key="metadata/dimensions.json",
                      Body=json.dumps(_dim_cache).encode(),
                      ContentType="application/json")
    except Exception as e:
        print(f"dim cache save error: {e}")

# Camera files carry ~100 KB of extra information (preview, settings) before the part
# that says how big the photo is, so read the first 256 KB.
_DIMS_READ_BYTES   = 262144
_DIMS_WAIT_SECONDS = 6      # a folder never waits longer; slower ones finish in the background

def _fetch_dims(path: str) -> tuple:
    try:
        resp     = s3.get_object(Bucket=R2_BUCKET, Key=path, Range=f"bytes=0-{_DIMS_READ_BYTES - 1}")
        img_data = resp["Body"].read()
        img      = Image.open(BytesIO(img_data))
        return img.size  # (w, h)
    except Exception:
        return (0, 0)

def _dims_for_paths(paths: list) -> dict:
    """Return {path: (w, h)} for all paths, fetching missing ones in parallel."""
    import concurrent.futures as _cf
    uncached = [p for p in paths if p not in _dim_cache]
    if uncached:
        def _measure(p):
            w, h = _fetch_dims(p)
            if w and h:
                _dim_cache[p] = [w, h]     # remembered even if it finishes after the wait
                return True
            return False
        ex = ThreadPoolExecutor(max_workers=20)
        futures = [ex.submit(_measure, p) for p in uncached]
        done, _ = _cf.wait(futures, timeout=_DIMS_WAIT_SECONDS)
        ex.shutdown(wait=False)            # don't hold the visitor for the stragglers
        if any(f.result() for f in done if not f.exception()):
            threading.Thread(target=_save_dim_cache_bg, daemon=True).start()
    return {p: tuple(_dim_cache[p]) if p in _dim_cache else (0, 0) for p in paths}

threading.Thread(target=_load_dim_cache, daemon=True).start()

def _autopurge_loop():
    import time
    while True:
        time.sleep(3600)
        try:
            trash = _load_trash_meta()
            now   = datetime.now(timezone.utc)
            keep  = [t for t in trash if datetime.fromisoformat(t["purge_at"]) > now]
            purge = [t for t in trash if datetime.fromisoformat(t["purge_at"]) <= now]
            if purge:
                _save_trash_meta(keep)
                _batch_delete_keys([t["key"] for t in purge])
                print(f"[autopurge] removed {len(purge)} expired trash items")
        except Exception as e:
            print(f"[autopurge] error: {e}")

threading.Thread(target=_autopurge_loop, daemon=True).start()

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
        _save_photo_list()
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

# Seed Winter Action defaults (tiers + flags) if missing
try:
    _wp  = _load_pricing()
    _wa  = _wp.setdefault("activities", {}).setdefault("Winter Action", {})
    _wf  = _wa.setdefault("flags", {})
    _dirty = False
    if not _wf.get("time_search"):
        _wf["time_search"] = True
        _dirty = True
    # Ensure the all-photos cap tier always exists (guards against stale admin saves)
    _tiers = list(_wa.get("tiers") or [])
    _has_cap = any(t.get("count") == "all" and t.get("max") for t in _tiers)
    if not _has_cap:
        _wa["tiers"] = [
            {"label": "1 Photo",    "count": 1,     "price": 25},
            {"label": "All Photos", "count": "all", "price": 115, "max": True},
        ]
        _dirty = True
    if _dirty:
        _save_pricing(_wp)
        print("Seeded Winter Action defaults (tiers + flags).")
except Exception as _we:
    print(f"Warning: could not seed Winter Action defaults: {_we}")


# ─────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────

class _RateLimiter:
    """Simple sliding-window in-memory rate limiter (no external deps)."""
    def __init__(self, max_calls: int, window: int):
        self.max_calls = max_calls
        self.window = window
        self._calls: dict = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = _time.monotonic()
        with self._lock:
            dq = self._calls.setdefault(key, deque())
            while dq and dq[0] < now - self.window:
                dq.popleft()
            if len(dq) >= self.max_calls:
                return False
            dq.append(now)
            return True

    def count(self, key: str) -> int:
        """Number of calls recorded for key inside the current window (records nothing)."""
        now = _time.monotonic()
        with self._lock:
            dq = self._calls.get(key)
            if not dq:
                return 0
            while dq and dq[0] < now - self.window:
                dq.popleft()
            return len(dq)

_search_limiter    = _RateLimiter(max_calls=20, window=60)   # 20 req/min per IP
_coupon_fail_limiter = _RateLimiter(max_calls=15, window=600)  # wrong discount codes per IP: no guessing codes
# Enlarged (lightbox-size) previews for visitors: plenty for flipping through photos, but nobody
# can copy a whole day. Per device (address + browser), with a higher cap for the address so a
# hotel's or the resort's shared Wi-Fi isn't treated as one person.
_preview_device_limiter = _RateLimiter(max_calls=300, window=600)
_preview_ip_limiter     = _RateLimiter(max_calls=1000, window=600)


def _preview_allowed(request: Request) -> bool:
    ip = _client_ip(request)
    device = ip + "|" + hashlib.sha256(request.headers.get("user-agent", "").encode()).hexdigest()[:16]
    return _preview_device_limiter.is_allowed(device) and _preview_ip_limiter.is_allowed(ip)
_lastname_limiter  = _RateLimiter(max_calls=30, window=60)   # 30 req/min per IP

# ─────────────────────────────────────────
# CHECKOUT PRICING HELPERS
# ─────────────────────────────────────────

_best_tier_price = best_tier_price


def _server_digital_price(album: dict, order_date: str) -> float:
    """Price for a digital album: the tiers the shop shows for that folder, with the
    shop's best-price rule. Raises ValueError if the folder has no prices set up."""
    count = int(album.get("count", 0))
    tiers = _pricing_for((album.get("location") or "").strip() or None,
                         (album.get("date") or order_date or "").strip() or None,
                         (album.get("last_name") or "").strip() or None).get("tiers") or []
    if count > 0 and not tiers:
        raise ValueError(f"Prices for {album.get('location') or 'these photos'} aren't set up yet")
    limit = selection_limit(tiers)
    if limit and count > limit:      # online orders only; staff can sell more at the POS
        raise ValueError(f"You can choose up to {limit:g} photos from {album.get('location') or 'one folder'} — "
                         f"please remove {count - limit:g} and try again")
    return _best_tier_price(count, tiers)

def _server_print_price(size_idx: int) -> float:
    """Authoritative server-side price for a print by size index."""
    sizes = _load_print_pricing().get("sizes", list(_DEFAULT_PRINT_SIZES))
    if 0 <= size_idx < len(sizes):
        return float(sizes[size_idx].get("price", 0))
    return 0.0

# ── Folder index ─────────────────────────────────────────────────────────────
# The day list, location lists, folders and name lookups used to scan every photo on
# every request. They're now built once from the photo list and rebuilt only when it
# changes: whenever images.json is saved (every lasting change saves it), when the list
# is replaced, and at least every few minutes as a safety net. Folder lists hold the
# same photo records, so label edits show up immediately.
_INDEX_MAX_AGE = 300
_index_lock = threading.Lock()
_index_state = {"version": 0, "built": None}   # built: (version, id(data), built_at, index)


def _photo_list_saved(params=None, **_):
    if params and params.get("Key") == "images.json":
        _index_state["version"] += 1

s3.meta.events.register("before-parameter-build.s3.PutObject", _photo_list_saved)


def _photo_list_changed() -> None:
    """Photo records were changed in memory (and their save is on its way): folder lists are
    rebuilt on their next use instead of waiting for the save."""
    _index_state["version"] += 1


def _folder_index() -> dict:
    """The current folder index, rebuilt if the photo list changed."""
    now = _time.monotonic()
    built = _index_state["built"]
    if built and built[0] == _index_state["version"] and built[1] == id(data) and now - built[2] < _INDEX_MAX_AGE:
        return built[3]
    with _index_lock:
        built = _index_state["built"]
        version = _index_state["version"]
        if built and built[0] == version and built[1] == id(data) and now - built[2] < _INDEX_MAX_AGE:
            return built[3]
        items = data
        index = _build_folder_index(items)
        _index_state["built"] = (version, id(items), _time.monotonic(), index)
        return index


def _folder_photos(date: str, location: str, pos: bool = False) -> list:
    """Live (non-draft) photos in a folder, in photo-list order. location = cleaned name.
    pos=True also includes partner photos (Eagle Mount), which the public gallery leaves out."""
    return _folder_index()["pos_folders" if pos else "folders"].get((date, location), [])


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

def _search(last_name, date, location, group=None, time_from=None, time_to=None,
            jacket_color=None, pants_color=None, sport=None, ski_instructor=None):
    jacket_colors_filter = [c for c in (jacket_color or []) if c] if isinstance(jacket_color, list) else ([jacket_color] if jacket_color else [])
    has_labels = any([jacket_colors_filter, pants_color, sport, ski_instructor])
    if not last_name and not has_labels:
        return JSONResponse(status_code=400, content={"error": "Provide last_name or color filter"})

    # (time_from / time_to are applied in the browser from /api/folder-times)
    if has_labels:
        pool = _folder_photos(date, location) if date and location else list(_folder_index()["by_path"].values())
        results = [(0.0, item) for item in filter_by_labels(pool, date, location, group, jacket_colors_filter,
                                                            pants_color, sport, ski_instructor)]
    else:
        results = match_last_name(list(_folder_index()["by_path"].values()), last_name, date, location, group)

    paths  = [item["path"] for _, item in results]
    dims   = _dims_for_paths(paths)
    photos = []
    for score, item in results:
        w, h = dims.get(item["path"], (0, 0))
        photos.append({
            "path":      item["path"],
            "date":      item["date"],
            "location":  clean_location(item["location"]),
            "last_name": item.get("last_name", ""),
            "filename":  os.path.basename(item["path"]),
            "score":     round(score, 4),
            "w": w,
            "h": h,
        })
    return {"count": len(photos), "photos": photos}


def _pricing_for(location: str = None, date: str = None, family: str = None) -> dict:
    """The pricing tiers the shop shows for a folder (see core.pricing.pricing_for)."""
    return pricing_for(location, date, family, load_pricing=_load_pricing, load_folder_meta=_load_folder_meta,
                       load_zip_pricing=_load_zip_pricing, load_items_bundles=_load_items_bundles)


# ── Thumbnails ────────────────────────────────────────────────────────────────
# Every photo has small versions, cached in R2 next to the original:
#   grid   — 700px WebP, centered logo + faint logo pattern: customer gallery (thumbs/gridwm/)
#   pos    — 700px, no watermark: iPad POS               (thumbs/nowm/)
#   admin  — 450px, no watermark: studio, download pages (admin_thumbs/)
#   public — 700px JPEG, watermarked: the gallery version before WebP (thumbs/v2/).
#            No longer made; photos that already have one keep using it.
#   large  — 1800px, no watermark: the enlarged view on a partner's free page (thumbs/nowm-large/).
#            Made only for partner photos, when they're marked or uploaded (else on first view).
# They're made in the background when photos are uploaded or go live, and on
# demand (as a fallback) the first time one is requested.
_THUMB_VARIANTS = {
    # name:   (cache key,            max px, quality, watermark, Cache-Control,              format)
    "grid":   (_grid_thumb_r2_key,   700, 80, True,  "public, max-age=31536000", "WEBP"),
    "pos":    (_nowm_thumb_r2_key,   700, 80, False, "public, max-age=31536000", "JPEG"),
    "admin":  (_admin_thumb_r2_key,  450, 72, False, "private, max-age=86400",   "JPEG"),
    "public": (_thumb_r2_key,        700, 80, True,  "public, max-age=31536000", "JPEG"),
    "large":  (_nowm_large_r2_key,  1800, 86, False, "private, max-age=86400",   "JPEG"),
}
_NEW_PHOTO_VARIANTS = ["grid", "pos", "admin"]
_THUMB_MEDIA_TYPES = {"WEBP": "image/webp", "JPEG": "image/jpeg"}
_thumb_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="thumbs")


def _open_for_thumbnail(fp, max_px: int) -> Image.Image:
    """Open a photo for shrinking. JPEGs are decoded at a reduced scale (draft mode),
    which is several times faster and uses far less memory than a full decode of a
    5-7 MB original; the result is then resized down with LANCZOS as before."""
    img = Image.open(fp)
    if img.format in ("JPEG", "MPO"):   # MPO = camera JPEG with a built-in preview
        # Largest reduction that still leaves both sides >= max_px (e.g. 6000x4000 -> 1500x1000)
        img.draft("RGB", (max_px, max_px))
    img = img.convert("RGB")
    return fix_orientation(img)


def _encode_thumb(img: Image.Image, variant: str) -> bytes:
    _, max_px, quality, watermark, _, fmt = _THUMB_VARIANTS[variant]
    t = img.copy()
    t.thumbnail((max_px, max_px), Image.LANCZOS)
    if watermark:
        t = apply_watermark(t, "thumb")
        if variant == "grid":
            t = apply_thumb_overlay(t)        # the faint logos the page used to draw on top
    buf = BytesIO()
    if fmt == "WEBP":
        t.save(buf, format="WEBP", quality=quality, method=6)
    else:
        t.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _make_thumbs(r2_key: str, variants, original: bytes = None, label_item: dict = None) -> dict:
    """Make the given thumbnail variants from one download of the original and save
    them to R2. Returns {variant: jpeg bytes}. Saving is best-effort. label_item: also
    label this photo record with the labeling model, from the same decode."""
    if original is None:
        original = s3.get_object(Bucket=R2_BUCKET, Key=r2_key)["Body"].read()
    max_px = max(_THUMB_VARIANTS[v][1] for v in variants)
    if label_item is not None and _needs_labels(label_item):
        rgb = open_for_model(original, max_px)        # as stored (the model's view), not yet turned upright
        _apply_labels(label_item, _color_model.classify_rgb(rgb))
        img = fix_orientation(rgb)
    else:
        img = _open_for_thumbnail(BytesIO(original), max_px)
    # Remember the original's size (as stored, like _fetch_dims) for the gallery layout
    try:
        _dim_cache[r2_key] = list(Image.open(BytesIO(original)).size)
    except Exception:
        pass
    out = {}
    for v in variants:
        key_fn, _, _, _, cache_control, fmt = _THUMB_VARIANTS[v]
        out[v] = _encode_thumb(img, v)
        try:
            s3.put_object(Bucket=R2_BUCKET, Key=key_fn(r2_key), Body=out[v],
                          ContentType=_THUMB_MEDIA_TYPES[fmt], CacheControl=cache_control)
        except Exception as e:
            print(f"[thumbs] save failed {key_fn(r2_key)}: {e}")
    return out


def _cached_thumb(r2_key: str, variant: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=R2_BUCKET, Key=_THUMB_VARIANTS[variant][0](r2_key))["Body"].read()
    except Exception:
        return None


def _thumb_on_demand(r2_key: str, variant: str) -> bytes:
    """Serve a thumbnail that isn't cached yet: make the requested one now, and the
    other variants in the background from the same download."""
    original = s3.get_object(Bucket=R2_BUCKET, Key=r2_key)["Body"].read()
    data = _make_thumbs(r2_key, [variant], original)[variant]
    others = [v for v in _NEW_PHOTO_VARIANTS if v != variant]
    _thumb_pool.submit(_make_thumbs_quietly, r2_key, others, original)
    return data


# Thumbnails are served straight from storage: the site answers with a redirect to a
# signed, expiring link for that one stored file, so the image never passes through this
# server. Each thumbnail keeps the same link for a day (browsers cache by link), and
# every link stays valid for three days, so a page or redirect a browser saved is still good.
_THUMB_LINK_REUSE_SECONDS = 86400
_THUMB_LINK_VALID_SECONDS = 3 * 86400
_THUMB_LINKS_MAX = 50000            # most recently used links remembered (~30 MB)
_thumb_links: "OrderedDict[str, tuple]" = OrderedDict()   # storage key -> (link, reuse until)
_thumb_links_lock = threading.Lock()


def _stored_thumb_link(r2_key: str, variant: str) -> str | None:
    """Signed link to the stored thumbnail, or None if it hasn't been made yet."""
    tkey = _THUMB_VARIANTS[variant][0](r2_key)
    now = _time.time()
    with _thumb_links_lock:
        hit = _thumb_links.get(tkey)
        if hit and hit[1] > now:
            _thumb_links.move_to_end(tkey)
            return hit[0]
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=tkey)
    except Exception:
        return None
    link = _presigned_get(tkey, _THUMB_LINK_VALID_SECONDS)
    if not link:
        return None
    with _thumb_links_lock:
        _thumb_links[tkey] = (link, now + _THUMB_LINK_REUSE_SECONDS)
        _thumb_links.move_to_end(tkey)
        while len(_thumb_links) > _THUMB_LINKS_MAX:
            _thumb_links.popitem(last=False)
    return link


def _forget_thumb_links(r2_key: str) -> None:
    """Drop remembered links for a photo whose thumbnails were removed."""
    with _thumb_links_lock:
        for key_fn, *_ in _THUMB_VARIANTS.values():
            _thumb_links.pop(key_fn(r2_key), None)


def _thumb_redirect(link: str) -> RedirectResponse:
    # Safe to cache for a day: a link is handed out for at most a day and valid for three
    return RedirectResponse(link, status_code=302, headers={"Cache-Control": "private, max-age=86400"})


# No-watermark photos are for staff: a signed-in POS (cookie set when the PIN is entered)
# or the studio. Anyone else asking for nowm=1 gets the watermarked photo.
POS_COOKIE = "ci_pos"
POS_COOKIE_SECONDS = 30 * 86400


def _pos_token(expires: int) -> str:
    sig = hmac.new((ADMIN_PASSWORD or "").encode(), f"pos|{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


# ── POS devices ──────────────────────────────────────────────────────────────
# The first time a device signs in with a PIN (the POS or the time clock), the owner is emailed
# a 6-digit code; once it's entered, that device is approved (for two years, or until removed in
# Settings > Security) and only needs a PIN. Everything the POS and the time clock do checks the
# device as well as the PIN. Kept in meta/admin_auth.json ("pos_devices"), hashes only.
# POS_DEVICE_CODES=off on Railway is the safety valve (e.g. if email is down).
POS_DEVICE_COOKIE   = "ci_pos_device"
POS_DEVICE_DAYS     = 730
POS_DEVICE_CODES_ON = os.getenv("POS_DEVICE_CODES", "on").strip().lower() != "off"
_pos_challenges: dict = {}        # challenge id -> pending code (in memory, 10 minutes)


def _pos_device_approved(request: Request) -> bool:
    """This device may use the POS: approved with an emailed code, or signed in to the studio."""
    if not POS_DEVICE_CODES_ON:
        return True
    token = request.cookies.get(POS_DEVICE_COOKIE, "")
    if token:
        dev = _load_admin_auth()["pos_devices"].get(_token_hash(token))
        if dev and dev.get("expires", "") > _now_iso():
            return True
    return _admin_authed(request)


def _device_refusal():
    return JSONResponse(status_code=403, content={"device_approval_needed": True,
                                                  "error": "This device needs approval — log out and enter your PIN"})


def _approve_pos_device(info: dict, photographer: dict) -> str:
    """Remember a newly approved POS device. Returns the token for its cookie."""
    token = secrets.token_urlsafe(32)
    d = _load_admin_auth()
    with _admin_auth_lock:
        d["pos_devices"][_token_hash(token)] = {
            "approved": _now_iso(), "describe": info.get("device", ""), "ip": info.get("ip", ""),
            "by": photographer.get("name", ""), "last_used": _now_iso(),
            "expires": (datetime.now(timezone.utc) + timedelta(days=POS_DEVICE_DAYS)).isoformat()}
        _save_admin_auth(d)
    return token


def _send_pos_device_code_email(code: str, info: dict, photographer: dict) -> bool:
    if not RESEND_API_KEY:
        print(f"[POS device code] {code} (RESEND_API_KEY not set, so it wasn't emailed)")
        return False
    try:
        from zoneinfo import ZoneInfo
        when = datetime.now(ZoneInfo("America/Denver")).strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except Exception:
        when = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    who = _html_escape(photographer.get("name", ""))
    body_html = (
        f'<p><strong>{who}</strong> entered their PIN (POS or time clock) on a <strong>new device</strong>:</p>'
        f'<p style="margin:0 0 1rem;color:#555">{_html_escape(info.get("device", ""))} &middot; '
        f'internet address {_html_escape(info.get("ip", ""))} &middot; {when} (Mountain)</p>'
        f'<p>The code to approve it:</p>'
        f'<p style="font-size:2rem;font-weight:700;letter-spacing:.3rem;margin:.5rem 0 1rem">{code}</p>'
        f'<p style="color:#555">It works for {LOGIN_CODE_MINUTES} minutes. After that, this device stays approved '
        f'until you remove it in Settings &gt; Security.</p>'
        f'<p style="color:#b91c1c"><strong>If this isn\'t one of the shop\'s devices,</strong> don\'t share the code, '
        f'and change {who}\'s PIN in Photographers.</p>')
    try:
        r = http_requests.post("https://api.resend.com/emails",
                               headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                               json={"from": RESEND_FROM, "to": [OWNER_EMAIL],
                                     "subject": f"New device code: {code}",
                                     "html": _wrap_email_html(body_html),
                                     "text": f"New device code: {code} ({photographer.get('name', '')} on "
                                             f"{info.get('device', '')}, {info.get('ip', '')}, {when}). If this isn't "
                                             f"one of the shop's devices, change that PIN."},
                               timeout=10)
        if not r.ok:
            print(f"POS device code email failed: {r.text}")
        return r.ok
    except Exception as e:
        print(f"POS device code email failed: {e}")
        return False


def _pos_signed_in(request: Request) -> bool:
    token = request.cookies.get(POS_COOKIE, "")
    if not ADMIN_PASSWORD or not token or not _pos_device_approved(request):
        return False
    try:
        expires = int(token.split(".", 1)[0])
    except ValueError:
        return False
    return expires > _time.time() and hmac.compare_digest(token, _pos_token(expires))


def _nowm_allowed(request: Request) -> bool:
    return _pos_signed_in(request) or _admin_authed(request)


# ── Store screens ────────────────────────────────────────────────────────────
# The in-store favoriting screens (/store). A new screen asks for access: the owner is emailed a
# 6-digit code, it's typed on that screen once, and the screen is approved under the name it was
# given (two years, or until removed in Settings > Security). A separate list from POS devices:
# a store screen sees published photos without watermarks, but can't use the POS. Every visit
# from a screen that isn't approved is blocked and the owner is told (one email per address an hour).
STORE_SCREEN_COOKIE = "ci_store_screen"
STORE_SCREEN_DAYS   = 730
STORE_NAME_MAX      = 40
_store_challenges: dict = {}      # challenge id -> pending code (in memory, 10 minutes)
_store_request_limiter = _RateLimiter(max_calls=3, window=3600)      # access requests per address
_store_notice_limiter  = _RateLimiter(max_calls=1, window=3600)      # blocked-visit emails per address
_store_notice_daily    = _RateLimiter(max_calls=20, window=86400)    # blocked-visit emails in all, a day


def _store_screen(request: Request) -> tuple[str, dict] | None:
    """(id hash, record) of this device's approved store screen, or None."""
    token = request.cookies.get(STORE_SCREEN_COOKIE, "")
    if not token:
        return None
    h = _token_hash(token)
    rec = _load_admin_auth()["store_screens"].get(h)
    return (h, rec) if rec and rec.get("expires", "") > _now_iso() else None


def _store_page_allowed(request: Request) -> bool:
    """An approved store screen, or the owner signed in to the studio (to look at it)."""
    return bool(_store_screen(request)) or _admin_authed(request)


def _approve_store_screen(name: str, info: dict) -> str:
    """Remember a newly approved store screen. Returns the token for its cookie."""
    token = secrets.token_urlsafe(32)
    now = _now_iso()
    d = _load_admin_auth()
    with _admin_auth_lock:
        d["store_screens"][_token_hash(token)] = {
            "name": name, "approved": now, "last_used": now,
            "describe": info.get("device", ""), "ip": info.get("ip", ""),
            "expires": (datetime.now(timezone.utc) + timedelta(days=STORE_SCREEN_DAYS)).isoformat()}
        _save_admin_auth(d)
    return token


def _touch_store_screen(screen_id: str) -> None:
    """Note when a screen was last used (saved at most once an hour)."""
    d = _load_admin_auth()
    with _admin_auth_lock:
        rec = d["store_screens"].get(screen_id)
        if not rec:
            return
        an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        if rec.get("last_used", "") < an_hour_ago:
            rec["last_used"] = _now_iso()
            _save_admin_auth(d)


def _send_store_screen_email(info: dict, name: str = "", code: str = "") -> bool:
    """To the owner: a store screen asking for access (with its code), or a blocked visit."""
    if not RESEND_API_KEY:
        print(f"[Store screen] {'code ' + code if code else 'blocked visit'} ({name or 'no name'}, "
              f"{info.get('device', '')}, {info.get('ip', '')}) — RESEND_API_KEY not set, so it wasn't emailed")
        return False
    try:
        when = datetime.now(ZoneInfo("America/Denver")).strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except Exception:
        when = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    where = (f'<p style="margin:0 0 1rem;color:#555">{_html_escape(info.get("device", ""))} &middot; '
             f'internet address {_html_escape(info.get("ip", ""))} &middot; {when} (Mountain)</p>')
    if code:
        subject = f"Store screen code: {code}"
        body_html = (
            f'<p>A screen asked to use <strong>store mode</strong> as '
            f'<strong>{_html_escape(name)}</strong>:</p>{where}'
            f'<p>The code to approve it:</p>'
            f'<p style="font-size:2rem;font-weight:700;letter-spacing:.3rem;margin:.5rem 0 1rem">{code}</p>'
            f'<p style="color:#555">It works for {LOGIN_CODE_MINUTES} minutes. After that, the screen stays approved '
            f'until you remove it in Settings &gt; Security.</p>'
            f'<p style="color:#b91c1c"><strong>If this isn\'t one of the shop\'s screens,</strong> don\'t share the code.</p>')
        text = (f"Store screen code: {code} ({name} on {info.get('device', '')}, {info.get('ip', '')}, {when}). "
                f"If this isn't one of the shop's screens, don't share the code.")
    else:
        subject = "Store mode: a screen that isn't approved was blocked"
        body_html = (f'<p>A device that isn\'t an approved store screen opened <strong>store mode</strong> '
                     f'and was blocked:</p>{where}'
                     f'<p style="color:#555">Nothing to do if this was you or the shop. Approved screens are '
                     f'listed in Settings &gt; Security.</p>')
        text = (f"A device that isn't an approved store screen opened store mode and was blocked "
                f"({info.get('device', '')}, {info.get('ip', '')}, {when}).")
    try:
        r = http_requests.post("https://api.resend.com/emails",
                               headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                               json={"from": RESEND_FROM, "to": [OWNER_EMAIL], "subject": subject,
                                     "html": _wrap_email_html(body_html), "text": text},
                               timeout=10)
        if not r.ok:
            print(f"Store screen email failed: {r.text}")
        return r.ok
    except Exception as e:
        print(f"Store screen email failed: {e}")
        return False


def _note_blocked_store_visit(info: dict) -> None:
    """Tell the owner about a blocked visit (one email per address an hour, 20 a day in all)."""
    if _store_notice_limiter.is_allowed(info.get("ip", "")) and _store_notice_daily.is_allowed("all"):
        _send_store_screen_email(info)


# Prints of photos also bought as digital files are this much off (online checkout; the POS's
# print window starts at half price too). Store screens advertise it from here.
PRINT_WITH_DIGITAL_PCT = 50


# ── Send to sale: a store screen's favorites, picked up at the POS with a 4-letter code ────────
# The customer taps "Send to sale" on a store screen. Their favorites are kept under a short code
# (letters only, no I or O; good until the end of the day, Big Sky time) that staff type at the
# POS. While the code waits, the screen keeps it up to date with any new hearts. When the POS
# records the sale that used it, the code is marked sold and the screen starts over by itself.
STORE_SENDS_PREFIX = "store_sends/"
STORE_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_STORE_CODE_RE = re.compile(r"[A-HJ-NP-Z]{4}")
_store_sends: dict = {}                     # code -> record (R2 keeps them across restarts)
_store_sends_lock = threading.Lock()
_store_send_limiter = _RateLimiter(max_calls=240, window=3600)    # sends and updates per screen


def _store_send_r2_key(code: str) -> str:
    return f"{STORE_SENDS_PREFIX}{code}.json"


def _load_store_send(code: str) -> dict | None:
    """The favorites sent under this code, or None (unknown, or past the end of its day)."""
    code = (code or "").strip().upper()
    if not _STORE_CODE_RE.fullmatch(code):
        return None
    rec = _store_sends.get(code)
    if rec is None:
        try:
            rec = json.loads(s3.get_object(Bucket=R2_BUCKET, Key=_store_send_r2_key(code))["Body"].read())
        except Exception:
            return None
        _store_sends[code] = rec
    return rec if rec.get("expires", "") > _now_iso() else None


def _save_store_send(rec: dict) -> None:
    s3.put_object(Bucket=R2_BUCKET, Key=_store_send_r2_key(rec["code"]), Body=json.dumps(rec).encode(),
                  ContentType="application/json")
    _store_sends[rec["code"]] = rec


def _store_code_day_end() -> str:
    """Midnight tonight, Big Sky time (as UTC, like the other times kept)."""
    now = datetime.now(ZoneInfo("America/Denver"))
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
    return midnight.astimezone(timezone.utc).isoformat()


def _store_send(screen_id: str, screen_name: str, cart: dict, code: str = "") -> dict:
    """Keep these favorites for the POS: under the screen's code that's still waiting, or a new
    one. Favorites cleared on the screen leave the code with nothing to sell ("cleared")."""
    summary = _cart_summary(cart)
    with _store_sends_lock:
        rec = _load_store_send(code) if code else None
        if rec and rec.get("screen") == screen_id and rec["status"] == "sold":
            return rec                      # already sold: the screen thanks them and starts over
        if not rec or rec.get("screen") != screen_id:
            if not summary["items"]:
                return {"code": "", "status": "cleared"}
            for _ in range(100):
                code = "".join(secrets.choice(STORE_CODE_LETTERS) for _ in range(4))
                if not _load_store_send(code):
                    break
            rec = {"code": code, "screen": screen_id, "screen_name": screen_name, "created": _now_iso(),
                   "expires": _store_code_day_end(), "status": "waiting"}
        status = rec["status"] if summary["items"] else "cleared"
        if status == "cleared" and summary["items"]:
            status = "waiting"
        rec = {**rec, "cart": cart, "summary": summary, "updated": _now_iso(), "status": status}
        _save_store_send(rec)
    return rec


def _mark_store_send(code: str, status: str, **fields) -> None:
    """The POS opened a code ("opened"), or recorded the sale that used it ("sold")."""
    with _store_sends_lock:
        rec = _load_store_send(code)
        if not rec or rec.get("status") == "sold":
            return
        _save_store_send({**rec, **fields, "status": status, status: _now_iso()})


def _make_thumbs_quietly(r2_key: str, variants, original: bytes = None, label_item: dict = None) -> bool:
    try:
        _make_thumbs(r2_key, variants, original, label_item)
        return True
    except Exception as e:
        print(f"[thumbs] failed {r2_key}: {e}")
        return False
    finally:
        if label_item is not None:
            done = _label_waits.pop(label_item["path"], None)
            if done:
                done.set()


def _start_thumb_job(work: list) -> str | None:
    """Make thumbnails in the background. work = [(r2_key, [variants]) or
    (r2_key, [variants], photo record to label), ...].
    Progress is visible at /api/admin/embed-status?job_id=…"""
    work = [w for w in work if w[1]]
    if not work:
        return None
    job_id = "thumbs-" + str(uuid.uuid4())[:8]
    _embed_jobs[job_id] = {"total": len(work), "done": 0, "finished": False, "failed": 0}
    for w in work:
        if len(w) > 2 and w[2] is not None:
            _label_waits.setdefault(w[2]["path"], threading.Event())

    def _run():
        job = _embed_jobs[job_id]
        for ok in _thumb_pool.map(lambda w: _make_thumbs_quietly(w[0], w[1], label_item=w[2] if len(w) > 2 else None),
                                  work):
            job["done"] += 1
            if not ok:
                job["failed"] += 1
        job["finished"] = True
        _save_dim_cache_bg()   # sizes recorded while making the thumbnails
        if any(len(w) > 2 and w[2] is not None for w in work):
            _save_photo_list_soon()             # the labels
        print(f"[thumbs] job {job_id} done: {job['done'] - job['failed']}/{job['total']} ok")

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def _queue_new_photo_thumbs(r2_keys: list) -> str | None:
    """All thumbnail variants for newly uploaded / published / moved photos; winter action
    photos are labeled (colors and sport) from the same download."""
    by_path = {it["path"]: it for it in data} if r2_keys else {}
    work = []
    for k in r2_keys:
        item = by_path.get(k)
        work.append((to_r2_key(k), list(_NEW_PHOTO_VARIANTS),
                     item if item is not None and _needs_labels(item) and _color_model.load() else None))
    return _start_thumb_job(work)

_folder_times_cache: dict = {}  # in-memory cache: (date, location) → {filename: minutes}

def _times_r2_key(date: str, location: str) -> str:
    slug = location.strip().lower().replace(" ", "-")
    return f"meta/times/{date}/{slug}.json"

# ─────────────────────────────────────────
# WOOCOMMERCE CHECKOUT
# ─────────────────────────────────────────

WC_BASE                  = "https://bigskyphotos.com/wp-json/wc/v3"
WC_KEY                   = os.getenv("WC_CONSUMER_KEY", "")
WC_SECRET                = os.getenv("WC_CONSUMER_SECRET", "")

SQUARE_APP_ID            = os.getenv("SQUARE_APP_ID", "")
SQUARE_ACCESS_TOKEN      = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_LOCATION_ID       = os.getenv("SQUARE_LOCATION_ID", "")
POS_PASSCODE             = os.getenv("POS_PASSCODE", "")
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

def _wc_coupon_payload(code_obj: dict) -> dict:
    """Build the WooCommerce coupon creation/update payload from our code object."""
    ctype = code_obj.get("type", "percent")
    applies_to = code_obj.get("applies_to", "all")

    if ctype == "free_shipping":
        wc_type, wc_amount = "percent", "0"
    elif ctype == "fixed":
        wc_type, wc_amount = "fixed_cart", str(code_obj.get("amount", 0))
    else:
        wc_type, wc_amount = "percent", str(code_obj.get("amount", 0))

    product_ids = []
    if applies_to == "digital" or applies_to == "portraits":
        product_ids = [WC_DIGITAL_PRODUCT_ID]
    elif applies_to == "prints":
        product_ids = [WC_PRINT_PRODUCT_ID]
    elif applies_to == "frames":
        product_ids = [WC_FRAME_PARENT_ID]

    payload = {
        "code":                 code_obj["code"].lower(),
        "discount_type":        wc_type,
        "amount":               wc_amount,
        "description":          code_obj.get("description", ""),
        "free_shipping":        bool(code_obj.get("free_shipping", False) or ctype == "free_shipping"),
        "minimum_amount":       str(code_obj.get("min_order") or 0),
        "product_ids":          product_ids,
        "usage_limit":          int(code_obj["usage_limit"]) if code_obj.get("usage_limit") else 0,
        "usage_limit_per_user": int(code_obj["usage_limit_per_user"]) if code_obj.get("usage_limit_per_user") else 0,
        "individual_use":       False,
    }
    if not code_obj.get("active", True):
        # Deactivated codes: set expires to yesterday so WC rejects them
        payload["date_expires"] = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    elif code_obj.get("expires"):
        payload["date_expires"] = code_obj["expires"]

    return payload

def _wc_sync_coupon(code_obj: dict) -> dict:
    """Create or update a coupon in WooCommerce. Returns {"wc_id": int} or {"error": str}."""
    if not WC_KEY or not WC_SECRET:
        return {"error": "WooCommerce credentials not configured"}
    payload = _wc_coupon_payload(code_obj)
    wc_id = code_obj.get("wc_id")
    try:
        if wc_id:
            r = http_requests.put(f"{WC_BASE}/coupons/{wc_id}", json=payload,
                                  auth=(WC_KEY, WC_SECRET), timeout=10)
        else:
            r = http_requests.post(f"{WC_BASE}/coupons", json=payload,
                                   auth=(WC_KEY, WC_SECRET), timeout=10)
        if r.ok:
            return {"wc_id": r.json().get("id")}
        return {"error": r.text[:200]}
    except Exception as e:
        return {"error": str(e)}

def _wc_delete_coupon(wc_id: int):
    """Permanently delete a coupon from WooCommerce."""
    if not WC_KEY or not WC_SECRET or not wc_id:
        return
    try:
        http_requests.delete(f"{WC_BASE}/coupons/{wc_id}", params={"force": True},
                             auth=(WC_KEY, WC_SECRET), timeout=10)
    except Exception:
        pass


# ── Online checkout pricing ───────────────────────────────────────────────────
# One calculation for everything an online order costs. The checkout page shows it
# (/api/checkout/quote) and /api/checkout charges exactly it, so what customers see
# is what WooCommerce charges. No price, discount or bundle is taken from the browser.

def _activity_groups(pricing: dict = None) -> dict:
    return activity_groups(pricing if pricing is not None else _load_pricing())


def _bundle_for_cart(locations: set, has_portraits: bool):
    return bundle_for_cart(_load_pricing(), locations, has_portraits)


def _check_coupon(code: str, email: str = ""):
    return check_coupon(_load_discount_codes(), code, email)


COUPON_LOCKED_MSG = "Too many wrong codes — please wait a few minutes and try again"


def _coupon_locked_out(request: Request) -> bool:
    return _coupon_fail_limiter.count(_client_ip(request)) >= _coupon_fail_limiter.max_calls


def _record_coupon_result(request: Request, problem: str) -> None:
    """Counts a code that doesn't exist toward the visitor's lockout (expired codes etc. don't count)."""
    if problem == "Invalid coupon code":
        _coupon_fail_limiter.is_allowed(_client_ip(request))


_FRAME_SIZE_LABELS = ["5x7", "8x10", "10x13", "16x20"]
_FRAME_VARIATION_INFO = {vid: key for key, vid in FRAME_VARIATION_MAP.items()}   # vid -> (size_idx, orientation)
_frame_prices_cache = {"data": None, "ts": 0.0}
_FRAME_PRICES_TTL = 300   # seconds; saving frame prices in the studio clears it


def _frame_variations() -> dict:
    """{variation_id: {"price": float, "stock_status": str}} for the frame product, from
    WooCommerce (kept for a few minutes). Raises CheckoutError if WooCommerce can't say."""
    c = _frame_prices_cache
    now = _time.time()
    if c["data"] is not None and now - c["ts"] < _FRAME_PRICES_TTL:
        return c["data"]
    try:
        r = http_requests.get(f"{WC_BASE}/products/{WC_FRAME_PARENT_ID}/variations",
                              params={"per_page": 100}, auth=(WC_KEY, WC_SECRET), timeout=15)
    except Exception as e:
        raise CheckoutError("Couldn't load frame prices — please try again in a moment") from e
    if r.status_code != 200:
        raise CheckoutError("Couldn't load frame prices — please try again in a moment")
    data = {}
    for v in r.json():
        try:
            data[int(v["id"])] = {"price": float(v.get("price") or v.get("regular_price") or 0),
                                  "stock_status": v.get("stock_status", "instock")}
        except (KeyError, TypeError, ValueError):
            continue
    c.update(data=data, ts=now)
    return data


def _frame_price(variation_id: int) -> float:
    v = _frame_variations().get(int(variation_id))
    if not v or v["price"] <= 0:
        raise CheckoutError("One of the frames in your cart isn't available right now")
    return v["price"]


def _checked_pose_paths(pose: dict, date: str, location: str, last_name: str, live: dict) -> list:
    """The photos of a portrait pose, if they really are that pose in that family's folder
    (from the folder's pose settings); otherwise the order is refused."""
    p_date = (pose.get("date") or date or "").strip()
    p_loc = (pose.get("location") or location or "").strip()
    family = (pose.get("family") or last_name or "").strip()
    meta_poses = _load_folder_meta().get(_folder_key(p_date, p_loc, family), {}).get("poses", [])
    match = next((mp for mp in meta_poses if (mp.get("color") or "") == (pose.get("color") or "")
                  and (not pose.get("pose_label") or mp.get("label") == pose.get("pose_label"))), None)
    paths = list(dict.fromkeys(p for p in pose.get("paths", []) if p))
    files = set((match or {}).get("files", []))
    if not match or not paths or not all(live.get(p) and live[p]["date"] == p_date
                                         and p.rsplit("/", 1)[-1] in files for p in paths):
        raise CheckoutError("One of the portrait poses in your cart isn't available — please refresh the page and try again")
    return paths


def _check_portrait_single(single: dict, last_name: str, live: dict) -> None:
    """A single portrait photo must be a published photo from that family's portrait folder."""
    it = live.get(single.get("path") or "")
    family = (single.get("family") or last_name or "").strip().lower()
    if not it or not _is_portrait_location(it.get("location", "")) or \
            (it.get("last_name") or it.get("group") or "").strip().lower() != family:
        raise CheckoutError("One of the portrait photos in your cart isn't available — please refresh the page and try again")


def _photo_folder(item: dict) -> tuple:
    """The gallery folder a photo is shown and priced in: its day, location and family (or
    group). Checkout works this out from the photo itself, never from what the cart says."""
    name = (item.get("last_name") or "").strip() or (item.get("group") or "").strip() or _item_group_from_path(item)
    return item["date"], clean_location(item["location"]), name


def _price_order(body: dict, partner_prints: dict | None = None) -> dict:
    """Price an online cart. Returns the WooCommerce order parts (line_items,
    fee_lines, coupon_lines, meta) and the summary the checkout page shows.
    partner_prints: {photo path: (percent off, partner name)} for prints of partner photos
    (Eagle Mount) when the buyer is signed in to that partner's page — worked out by the
    checkout route from the visitor's cookie, never taken from the cart."""
    partner_prints = partner_prints or {}
    pricing = _load_pricing()
    logo_fee = float(pricing.get("logo_fee", 2) or 0)
    date = (body.get("date") or "").strip()
    location = body.get("location", "")
    last_name = (body.get("last_name") or "").strip()

    items, rows = [], []          # items become WooCommerce lines; rows are what the page lists

    def add(cat, full, line, row=None, name=None, sub="", no_discount=False):
        if row is None:
            rows.append({"name": name or line["name"], "sub": sub, "full": 0.0, "price": 0.0})
            row = len(rows) - 1
        # no_discount: already discounted (a partner photo's print) and never combined with
        # a bundle or a code
        items.append({"cat": cat, "full": round(full, 2), "line": line, "row": row, "no_discount": no_discount})
        return row

    # ── Digital albums (priced like the gallery shows them) ──
    albums = [a for a in (body.get("digital_albums") or []) if int(a.get("count") or 0) > 0]
    album_paths, locations = [], set()
    live = _folder_index()["by_path"]          # published photos by path
    for a in albums:
        count = parse_int(a.get("count"), 0, 1, 10000, "photo count")
        # Only the photos paid for are delivered: each must be a published photo from this
        # album's folder, and there must be exactly as many as are charged for.
        # Its day, location and family come from the photos (all from one gallery folder), so
        # nothing the cart says can change which prices apply.
        a_paths = list(dict.fromkeys(p for p in (a.get("paths") or []) if p))
        if len(a_paths) != count or not all(p in live for p in a_paths) \
                or len({_photo_folder(live[p]) for p in a_paths}) != 1:
            raise CheckoutError("Something in your cart isn't right (photos) — please refresh the page and try again")
        f_date, f_loc, f_name = _photo_folder(live[a_paths[0]])
        try:
            price = _server_digital_price({"count": count, "date": f_date, "location": f_loc, "last_name": f_name}, date)
        except ValueError as e:
            raise CheckoutError(str(e))
        label = f_name or f_loc or "Photos"
        add("digital", price, {"product_id": WC_DIGITAL_PRODUCT_ID, "quantity": count,
                               "name": f"Digital Photos — {label}",
                               "meta_data": [{"key": "Photos", "value": f"{count} digital photo{'s' if count != 1 else ''}"}]},
            sub=f"{count} photo{'s' if count != 1 else ''}")
        album_paths += a_paths
        locations.add(f_loc.strip().lower())

    # ── Portrait poses and single photos ──
    poses = body.get("portrait_poses") or []
    singles = body.get("portrait_singles") or []
    portrait_paths = []
    if poses or singles:
        pp = _load_portrait_pricing()
        pose_tiers = pp.get("pose_tiers", _DEFAULT_PORTRAIT_PRICING["pose_tiers"])
        single_price = float(pp.get("single_photo", _DEFAULT_PORTRAIT_PRICING["single_photo"]))
        # Poses are sold by family: each family's poses get the rate for how many of that
        # family's poses are in the order (two families with one pose each pay full price twice)
        def family_of(pose):
            return ((pose.get("date") or date or "").strip(), clean_location(pose.get("location") or location or ""),
                    (pose.get("family") or last_name or "").strip().lower())
        per_family = Counter(family_of(pose) for pose in poses)
        for pose in poses:
            n = len(pose.get("files", []))
            paths = _checked_pose_paths(pose, date, location, last_name, live)
            count = per_family[family_of(pose)]
            pose_price = float(pose_tiers[min(count - 1, len(pose_tiers) - 1)])
            add("digital", pose_price, {
                "product_id": WC_DIGITAL_PRODUCT_ID, "quantity": 1,
                "name": f"Portrait {pose.get('pose_label', 'Pose')} ({n} photo{'s' if n != 1 else ''}) — {pose.get('family', last_name or location)}",
                "meta_data": [{"key": "Photos", "value": f"{n} digital photo{'s' if n != 1 else ''}"},
                              {"key": "_photo_paths", "value": "|".join(paths)}]})
            portrait_paths += paths
        for single in singles:
            _check_portrait_single(single, last_name, live)
            add("digital", single_price, {
                "product_id": WC_DIGITAL_PRODUCT_ID, "quantity": 1,
                "name": f"Portrait Single Photo — {single.get('family', last_name or location)}",
                "meta_data": [{"key": "Photos", "value": single.get("filename", "Photo")},
                              {"key": "_photo_paths", "value": single.get("path", "")}]},
                sub=single.get("filename", ""))
            if single.get("path"):
                portrait_paths.append(single["path"])

    bought_digital = set(album_paths) | set(portrait_paths)

    # ── Big Sky Resort logo on digital photos (edited by hand, so listed by name) ──
    logo_paths = [p for p in dict.fromkeys(body.get("digital_logo_paths") or []) if p in bought_digital]
    if logo_paths and logo_fee > 0:
        names = [p.rsplit("/", 1)[-1] for p in logo_paths]
        n = len(logo_paths)
        add("digital", logo_fee * n, {
            "product_id": WC_DIGITAL_PRODUCT_ID, "quantity": n,
            "name": f"Big Sky Resort logo — {n} photo{'s' if n != 1 else ''}",
            "meta_data": [{"key": "Logo photos", "value": ", ".join(names)},
                          {"key": "_logo_paths", "value": "|".join(logo_paths)}]},
            sub=", ".join(names))

    # ── Prints (half price when the same photo is bought digitally) and their frames ──
    sizes = _load_print_pricing().get("sizes", list(_DEFAULT_PRINT_SIZES))
    prints = body.get("prints") or []
    all_photos = _folder_index()["all_by_path"]
    for p in prints:
        size_idx = parse_int(p.get("size_idx"), 0, 0, len(sizes) - 1, "print size")
        # A print is of a published photo, or a partner photo the buyer is signed in for
        photo = live.get(p.get("path") or "") or (all_photos.get(p["path"]) if p.get("path") in partner_prints else None)
        if not photo:
            raise CheckoutError("One of the prints in your cart isn't available — please refresh the page and try again")
        size_label = p.get("size") or sizes[size_idx].get("label", "")
        half = bool(p.get("path")) and p["path"] in bought_digital
        logo = bool(p.get("logo"))
        base = float(sizes[size_idx].get("price", 0))
        partner_pct, partner_name = partner_prints.get(p.get("path") or "", (0, ""))
        with_digital = base * (1 - PRINT_WITH_DIGITAL_PCT / 100)
        if partner_pct and base * (1 - partner_pct / 100) < (with_digital if half else base):
            half = False                          # the partner discount is the better one
            base = round(base * (1 - partner_pct / 100), 2)
        else:
            partner_pct = 0
            base = with_digital if half else base
        price = base + (logo_fee if logo else 0)
        meta = []
        if half:
            meta.append({"key": "Price", "value": f"{PRINT_WITH_DIGITAL_PCT}% off (digital photo also purchased)"})
        if partner_pct:
            meta.append({"key": "Price", "value": f"{partner_pct:g}% off ({partner_name} photo)"})
            # hidden (leading _): lets the paid order record this print in the partner's report
            meta.append({"key": "_partner_print", "value": json.dumps({
                "partner": (_folder_index()["all_by_path"].get(p.get("path") or "") or {}).get("partner", ""),
                "path": p.get("path") or "", "size": size_label, "full": float(sizes[size_idx].get("price", 0)),
                "logo": logo_fee if logo else 0})})
        if logo:
            meta.append({"key": "Logo", "value": "Big Sky Resort logo"})
        frame = p.get("frame") if p.get("frame") not in ("", None, "No Frame") else None
        row = add("prints", price, {"product_id": WC_PRINT_PRODUCT_ID, "quantity": 1,
                                    "name": f"{size_label} Print — {p.get('filename', '')}" + (" + Big Sky Resort logo" if logo else ""),
                                    "meta_data": meta},
                  name=f"{size_label} Print" + (f" + {frame}" if frame else "") + (" + Logo" if logo else ""),
                  sub=p.get("filename", ""), no_discount=bool(partner_pct))
        locations.add(clean_location(photo["location"]).strip().lower())
        if frame:
            orientation = p.get("orientation") if p.get("orientation") in ("landscape", "portrait") else "landscape"
            vid = FRAME_VARIATION_MAP.get((size_idx, orientation))
            if vid is None:
                raise CheckoutError("One of the frames in your cart isn't available right now")
            add("frames", _frame_price(vid), {"product_id": WC_FRAME_PARENT_ID, "variation_id": vid,
                                              "quantity": 1, "name": f"  └ Frame: {frame}"}, row=row)

    # ── Frames on their own ──
    frame_ship_sizes = []
    for fr in body.get("frames") or []:
        vid = parse_int(fr.get("variation_id"), 0, 1, 10 ** 9, "frame")
        if vid not in _FRAME_VARIATION_INFO:
            raise CheckoutError("One of the frames in your cart isn't available right now")
        qty = parse_int(fr.get("quantity"), 1, 1, 20, "frame quantity")
        size_idx, orientation = _FRAME_VARIATION_INFO[vid]
        label = _FRAME_SIZE_LABELS[size_idx] if size_idx < len(_FRAME_SIZE_LABELS) else ""
        add("frames", _frame_price(vid) * qty, {"product_id": WC_FRAME_PARENT_ID, "variation_id": vid, "quantity": qty,
                                                "name": f"Frame — {label} ({orientation.capitalize()})"},
            name=f"{label} Frame ({orientation}) ×{qty}")
        frame_ship_sizes += [size_idx] * qty

    # ── Shipping ──
    shipping, free_shipping = shipping_for(prints, frame_ship_sizes, sizes,
                                           _load_print_pricing().get("free_ship_threshold", 3))

    # ── Discounts: a bundle takes its % off every item, except items a code takes more off ──
    def worth(cats, key="full", discountable=False):
        return round(sum(it[key] for it in items if it["cat"] in cats and not (discountable and it["no_discount"])), 2)

    bundle, upsell = _bundle_for_cart(locations, bool(poses or singles))
    coupon, coupon_note = None, ""
    code_in = (body.get("coupon_code") or "").strip()
    if code_in:
        coupon, coupon_note = _check_coupon(code_in, body.get("email", ""))
    bundle_cats = set(CATEGORIES) if bundle else set()
    if coupon and coupon_item_discount(coupon, 1.0) > 0 and not worth(coupon_scope(coupon), discountable=True) \
            and any(it["no_discount"] for it in items):
        coupon_note = "Codes can't be combined with the partner discount on these prints"
        coupon = None
    if bundle and coupon:
        scope = coupon_scope(coupon) & {it["cat"] for it in items}
        code_off = coupon_item_discount(coupon, worth(scope, discountable=True))
        if code_off > 0:
            if code_off > round(worth(scope, discountable=True) * bundle["pct"] / 100, 2):
                bundle_cats -= scope            # the code gives these items more
            else:
                coupon_note = f"The {bundle['label']} ({bundle['pct']:g}% off) saves you more than this code"
                coupon = None
    for it in items:
        pct = bundle["pct"] if it["cat"] in bundle_cats and not it["no_discount"] else 0
        it["price"] = round(it["full"] * (1 - pct / 100), 2)
        rows[it["row"]]["full"] = round(rows[it["row"]]["full"] + it["full"], 2)
        rows[it["row"]]["price"] = round(rows[it["row"]]["price"] + it["price"], 2)
    bundle_savings = round(worth(CATEGORIES) - worth(CATEGORIES, "price"), 2)
    coupon_off = coupon_item_discount(coupon, worth(coupon_scope(coupon), "price", discountable=True)) if coupon else 0.0
    subtotal = worth(CATEGORIES, "price")

    # ── WooCommerce order parts ──
    line_items = [{**it["line"], "subtotal": str(it["price"]), "total": str(it["price"])} for it in items]
    fee_lines = [{"name": "Shipping", "total": str(shipping)}] if shipping else []
    filenames = [p.rsplit("/", 1)[-1] for p in album_paths] + [s.get("filename", "") for s in singles]
    meta = [
        {"key": "_photo_location", "value": location},
        {"key": "_photo_date",     "value": date},
        {"key": "_photo_files",    "value": ", ".join(filenames)},
        {"key": "_photo_paths",    "value": "|".join(list(dict.fromkeys(album_paths + portrait_paths)))},
    ]
    if coupon:
        meta += [{"key": "_coupon_code", "value": code_in.lower()},
                 {"key": "_coupon_discount", "value": str(coupon_off)}]
    if bundle_savings > 0:
        meta += [{"key": "_bundle_discount_pct", "value": str(bundle["pct"])},
                 {"key": "_bundle_savings", "value": str(bundle_savings)}]
    return {
        "line_items": line_items,
        "fee_lines": fee_lines,
        "coupon_lines": [{"code": code_in.lower()}] if coupon else [],
        "meta": meta,
        "summary": {
            "rows": rows,
            "subtotal": subtotal,
            "shipping": shipping,
            "free_shipping": free_shipping,
            "coupon": {"code": code_in.upper(), "discount": coupon_off,
                       "free_shipping": bool(coupon.get("free_shipping") or coupon.get("type") == "free_shipping")} if coupon else None,
            "coupon_note": coupon_note or "",
            "bundle": {"label": bundle["label"], "pct": bundle["pct"], "savings": bundle_savings} if bundle_savings > 0 else None,
            "upsell": upsell,
            "total": round(max(0.0, subtotal + shipping - coupon_off), 2),
        },
    }


def _partner_print_discounts(request: Request, body: dict) -> dict:
    """{path: (percent off, partner name)} for the prints in this cart that are photos of a
    partner whose page this visitor is signed in to."""
    out = {}
    paths = {p.get("path") for p in (body.get("prints") or []) if isinstance(p, dict) and p.get("path")}
    if not paths:
        return out
    for partner in _load_partners():
        pct = float(partner.get("print_discount_pct") or 0)
        if not _partner_signed_in(request, partner):
            continue                              # (0% off still lets them order prints of their photos)
        for path in paths:
            if _partner_photo(partner, path):
                out[path] = (pct, partner["name"])
    return out


def _visitor_photo_access(request: Request, path: str) -> str | None:
    """May a visitor without a staff sign-in see this photo (watermarked)? "public" for photos
    published in the gallery, "partner" for a partner's (Eagle Mount) photo when the visitor is
    signed in to that partner's page, None for anything else (unpublished folders included)."""
    index = _folder_index()
    if path in index["by_path"]:
        return "public"
    item = index["all_by_path"].get(path or "")
    if not item or not item.get("partner"):
        return None
    if any(_partner_photo(p, path) and _partner_signed_in(request, p) for p in _load_partners()):
        return "partner"
    return None


def _studio_partner_prints(body: dict) -> dict:
    """Studio invoices: the partner discount for prints of partner photos in the cart (the
    customer saved them from the partner's page; the studio can still change any price)."""
    partners = {p["id"]: p for p in _load_partners()}
    out = {}
    for p in body.get("prints") or []:
        item = _folder_index()["all_by_path"].get((p or {}).get("path") or "")
        partner = partners.get((item or {}).get("partner"))
        if partner:
            out[p["path"]] = (float(partner.get("print_discount_pct") or 0), partner["name"])
    return out


def _create_checkout(body: dict, partner_prints: dict | None = None, line_prices: list | None = None,
                     extra_lines: list | None = None, shipping_price: float | None = None):
    """Create the WooCommerce order. Studio invoices only: line_prices sets each line's price
    (in the order the cart's lines are priced), extra_lines adds items the studio typed in
    ([{"name", "price"}]) and shipping_price replaces the shipping. The checkout then lists
    every item at its invoice price; the normal price is kept on the order for reference."""
    try:
        priced = _price_order(body, partner_prints)
    except CheckoutError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    total = priced["summary"]["total"]
    normal_total = total
    edited = line_prices is not None or bool(extra_lines) or shipping_price is not None
    if edited:
        lines = priced["line_items"]
        if line_prices is not None:
            if len(line_prices) != len(lines):
                return JSONResponse(status_code=400, content={"error": "The cart changed: reopen the invoice form"})
            lines = [{**ln, "subtotal": f"{p:.2f}", "total": f"{p:.2f}"} for ln, p in zip(lines, line_prices)]
        lines = lines + [{"product_id": WC_DIGITAL_PRODUCT_ID, "quantity": 1, "name": x["name"],
                          "subtotal": f"{x['price']:.2f}", "total": f"{x['price']:.2f}"} for x in (extra_lines or [])]
        shipping = float(priced["summary"]["shipping"] or 0) if shipping_price is None else shipping_price
        priced["line_items"] = lines
        priced["fee_lines"] = [f for f in priced["fee_lines"] if f.get("name") != "Shipping"] + \
                              ([{"name": "Shipping", "total": f"{shipping:.2f}"}] if shipping else [])
        priced["coupon_lines"] = []
        total = round(sum(float(ln["total"]) for ln in lines) + shipping, 2)
        if total != normal_total:
            priced["meta"] = priced["meta"] + [{"key": "_normal_price", "value": f"{normal_total:.2f}"}]
    try:
        customer_email = body.get("email", "")
        billing_data = dict(body.get("billing") or {})
        billing_data["email"] = customer_email      # always set, so WooCommerce emails the customer
        order_data = {
            "status":       "pending",
            "line_items":   priced["line_items"],
            "fee_lines":    priced["fee_lines"],
            "coupon_lines": priced["coupon_lines"],
            "meta_data":    priced["meta"],
            "billing":      billing_data,
            "shipping":     body.get("shipping", {}),
        }
        resp = http_requests.post(f"{WC_BASE}/orders", json=order_data, auth=(WC_KEY, WC_SECRET), timeout=15)
        if resp.status_code not in (200, 201):
            return JSONResponse(status_code=500, content={"error": f"WooCommerce error: {resp.text}"})
        order = resp.json()
        pay_url = (f"https://bigskyphotos.com/checkout/order-pay/{order['id']}/"
                   f"?pay_for_order=true&key={order['order_key']}")
        return {"checkout_url": pay_url, "total": total, "normal_total": normal_total, "order_id": order["id"]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class _KeyedLocks:
    """One lock per key (a Square payment, a POS sale, an online order), so work on the
    same thing never overlaps while different ones still run side by side."""
    def __init__(self):
        self._locks, self._guard = {}, threading.Lock()

    @contextmanager
    def hold(self, key: str):
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(key, None)


_sale_locks = _KeyedLocks()


def _square_id_marker(square_id: str) -> str:
    return f"pos_square_ids/{hashlib.sha256(square_id.encode()).hexdigest()}.json"


def _square_id_used(square_id: str) -> bool:
    """Has this Square payment already been recorded? One lookup instead of reading
    every sale; falls back to reading them if storage can't answer."""
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=_square_id_marker(square_id))
        return True
    except Exception as e:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        return any(o.get("square_payment_id") == square_id for o in _fetch_pos_orders())


def _mark_square_id_used(square_id: str, order_id: str):
    s3.put_object(Bucket=R2_BUCKET, Key=_square_id_marker(square_id),
                  Body=json.dumps({"square_payment_id": square_id, "order_id": order_id}).encode(),
                  ContentType="application/json")


def _backfill_square_id_markers():
    """Startup: markers for sales recorded before markers existed (so a payment can't
    be reused for a second sale). Only writes the ones that are missing."""
    _data_ready.wait()
    try:
        for o in _fetch_pos_orders():
            sid = o.get("square_payment_id")
            if sid and sid != "COMP" and not _square_id_used(sid):
                _mark_square_id_used(sid, o.get("id", ""))
    except Exception as e:
        print(f"Square payment markers not backfilled: {e}")


def _record_pos_sale(body: dict):
    """Verify (unless COMP), record the sale, add the customer and send the receipt.
    Returns the result dict, or a JSONResponse for a refused sale. Two completions of
    the same Square payment never run at once (the second then sees it was used)."""
    square_id = body.get("square_payment_id", "")
    if not square_id or square_id == "COMP":
        return _record_pos_sale_now(body)
    with _sale_locks.hold("sq:" + square_id):
        return _record_pos_sale_now(body)


def _record_pos_sale_now(body: dict):
    pin = body.get("pin", "").strip()
    photog = _auth_photographer(pin)
    if not photog:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})

    customer_email  = body.get("email", "").strip()
    customer_name   = body.get("name", "").strip()
    customer_phone  = body.get("phone", "").strip()
    customer_addr   = body.get("address", "").strip()

    # Multi-item cart support
    items = body.get("items", [])

    # The photos this sale delivers: action photos and portrait poses; fall back to legacy fields.
    # File names (what commission by file prefix reads) come from the action photos only, as before.
    photo_paths, filenames, location, photo_date = [], [], "", ""
    for item in items:
        if item.get("category") in ("action_photos", "portrait"):
            photo_paths.extend(p for p in item.get("photo_paths", []) if isinstance(p, str) and p)
            if item.get("category") == "action_photos":
                filenames.extend(item.get("filenames", []))
            if not location:
                location = item.get("location", "")
            if not photo_date:
                photo_date = item.get("date", "")
    photo_paths = list(dict.fromkeys(photo_paths))
    if not photo_paths:
        photo_paths  = body.get("photo_paths", [])
        filenames    = body.get("filenames", [])
        location     = body.get("location", "")
        photo_date   = body.get("date", "")

    total           = float(body.get("total", 0))
    coupon_code     = body.get("coupon_code", "").strip().lower()
    coupon_discount = float(body.get("coupon_discount", 0))
    square_id       = body.get("square_payment_id", "")
    expire_days_req = int(body.get("expire_days", DOWNLOAD_EXPIRE_DAYS))

    if not customer_email:
        return JSONResponse(status_code=400, content={"error": "Customer email required"})

    # An Unlimited package was checked against the Pricing settings before Square took the
    # payment, so a paid one is recorded even if its price was edited since (comps are checked now)
    unlimited, problem = _check_unlimited_cart(items, strict=(square_id == "COMP"))
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})

    # Square sales: confirm with Square that it was really paid before recording
    # the sale or sending photos. "COMP" is a deliberate free sale by staff.
    square_check = {}
    if square_id != "COMP":
        if _square_id_used(square_id):
            return JSONResponse(status_code=409, content={
                "error": "This Square payment was already used for another sale", "verify_failed": True})
        ok, detail = _square_verify_payment(square_id, int(round(total * 100)))
        if not ok:
            return JSONResponse(status_code=402, content={
                "error": f"Couldn't confirm the payment with Square: {detail}", "verify_failed": True})
        square_check = {"square_verified": True, "square_paid": detail["paid_cents"] / 100,
                        "square_tip": detail["tip_cents"] / 100, "square_tax": detail["tax_cents"] / 100,
                        "square_tenders": detail["tender_types"],
                        "square_payment_ids": detail["payment_ids"],
                        "square_fee": detail["fee_cents"] / 100,
                        "square_fee_final": detail["fee_final"]}
        if detail["payment_ids"]:
            square_check.update(_square_receipt(detail["payment_ids"][0]))

    now   = datetime.now(timezone.utc)
    token = str(uuid.uuid4()).replace("-", "")

    if photo_paths:
        expires = (now + timedelta(days=expire_days_req)).isoformat()
        title   = f"Your Photos — {location or photo_date or now.strftime('%B %d, %Y')}"
        pkg = {
            "token":       token,
            "title":       title,
            "emails":      [customer_email],
            "paths":       photo_paths,
            "created":     now.isoformat(),
            "expires":     expires,
            "expire_days": expire_days_req,
            "source":      "pos",
        }
        _store_pkg(token, pkg)
    elif unlimited:
        # The package's own download page: empty now, photos are added during the trip
        dates = _unlimited_dates(unlimited["days"], unlimited["link_days_after"], now)
        expires = dates["expires"]
        expire_days_req = max(1, (datetime.fromisoformat(expires) - now).days + 1)
        title = "Your Unlimited Action Photos"
        unlimited = {**unlimited, **dates, "token": token, "order_id": f"pos-{token[:8]}",
                     "name": customer_name, "email": customer_email, "phone": customer_phone,
                     "photographer_id": photog.get("id", ""), "photographer_name": photog.get("name", ""),
                     "created": now.isoformat(), "status": "open"}
        _store_pkg(token, {
            "token":       token,
            "title":       title,
            "emails":      [customer_email],
            "paths":       [],
            "created":     now.isoformat(),
            "expires":     expires,
            "expire_days": expire_days_req,
            "source":      "pos",
            "unlimited":   {k: unlimited[k] for k in ("tier_id", "tier_name", "days", "people",
                                                       "start", "last_day", "order_id")},
        })
        _store_unlimited_sale(unlimited)
    else:
        title = "In-Person Sale"

    order_id = f"pos-{token[:8]}"
    order_record = {
        "id":                order_id,
        "date":              now.strftime("%Y-%m-%d"),
        "created":           now.isoformat(),
        "source":            "pos",
        "photographer_name": photog.get("name", ""),
        "photographer_id":   photog.get("id", ""),
        "name":              customer_name,
        "email":             customer_email,
        "phone":             customer_phone,
        "address":           customer_addr,
        "location":          location,
        "photo_date":        photo_date,
        "photo_paths":       "|".join(photo_paths),
        "filenames":         ", ".join(filenames),
        "total":             total,
        "coupon_code":       coupon_code,
        "coupon_discount":   coupon_discount,
        "square_payment_id": square_id,
        "package_token":     token if (photo_paths or unlimited) else "",
        "items":             items,
        **square_check,
    }
    if unlimited:
        # Commission for a package is split by photographer when it closes, not credited now
        order_record["unlimited"] = {k: unlimited[k] for k in ("tier_id", "tier_name", "days", "people",
                                                               "custom_price", "start", "last_day", "status")}
    _store_pos_order(order_record)
    if square_check:
        _mark_square_id_used(square_id, order_id)
    if square_check and not square_check["square_fee_final"]:
        # Square fills the card fee in shortly after the payment; record it then
        fee_check = threading.Timer(30, _refresh_square_fee, args=(order_record,))
        fee_check.daemon = True
        fee_check.start()

    try:                                     # partner photos (Eagle Mount): their donation report
        _log_partner_pos_sale(items, photo_paths, order_id)
    except Exception as e:
        print(f"Partner sale not logged ({order_id}): {e}")

    try:                                     # an in-store order: their online saved cart is done
        _clear_saved_cart_for(customer_email)
    except Exception as e:
        print(f"Saved cart not cleared after POS sale ({customer_email}): {e}")

    store_code = str(body.get("store_code") or "")
    try:                                     # favorites sent from a store screen: that screen starts over
        if store_code:
            _mark_store_send(store_code, "sold", order_id=order_id)
    except Exception as e:
        print(f"Store code not marked sold after POS sale ({store_code}): {e}")

    # Add the customer to the directory (or refresh their contact info).
    # A failure here must never block the sale.
    try:
        cust_info = body.get("customer") or {}
        if not cust_info.get("first") and not cust_info.get("last"):
            name_parts = customer_name.split(None, 1)
            cust_info = {**cust_info,
                         "first": name_parts[0] if name_parts else "",
                         "last":  name_parts[1] if len(name_parts) > 1 else ""}
        if not cust_info.get("phone"):
            cust_info = {**cust_info, "phone": customer_phone}
        _upsert_customer_from_pos({**cust_info, "email": customer_email})
    except Exception as e:
        print(f"POS customer upsert failed ({customer_email}): {e}")

    if coupon_code and coupon_discount > 0:
        _record_coupon_use(coupon_code.upper(), {
            "date":         now.strftime("%Y-%m-%d"),
            "order_id":     order_id,
            "email":        customer_email,
            "amount_saved": coupon_discount,
            "order_total":  total,
            "source":       "pos",
        })

    result = {"order_id": order_id}
    download_url = expire_str = None
    package_note = ""
    if photo_paths or unlimited:
        # The package was saved above, so this link works the moment the email arrives
        download_url = f"{SITE_URL}/package/{token}"
        expire_str   = (now + timedelta(days=expire_days_req)).strftime("%B %d, %Y")
        if unlimited:
            expire_str = _mountain_date_str(unlimited["expires"])
            package_note = _unlimited_note(unlimited["last_day"], expire_str)
            result["unlimited"] = {k: unlimited[k] for k in ("tier_name", "people", "start", "last_day")}
        result["token"]        = token
        result["download_url"] = download_url

    # One email per sale: the itemized receipt, which carries the download button
    _send_pos_receipt_email(
        to_email=customer_email,
        customer_name=customer_name,
        order_id=order_id,
        items=items,
        total=total,
        coupon_code=coupon_code,
        coupon_discount=coupon_discount,
        download_url=download_url,
        expire_str=expire_str,
        square=square_check or None,
        comp=(square_id == "COMP"),
        package_note=package_note,
    )

    return result


# ── Square sales that finish in another browser ──────────────────────────────
# On the iPad, Square returns to the POS by opening its callback in Safari, even
# when the POS runs as a Home Screen app, and the two don't share storage. So the
# sale details are saved here before Square opens, keyed by the random `state`
# Square hands back, and whichever browser Square returns to finishes the sale.
POS_PENDING_PREFIX = "pos_pending/"
POS_PENDING_MAX_AGE = timedelta(hours=48)
_POS_STATE_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")


def _load_pos_pending(state: str):
    if not _POS_STATE_RE.match(state or ""):
        return None
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"{POS_PENDING_PREFIX}{state}.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _save_pos_pending(state: str, pending: dict):
    s3.put_object(Bucket=R2_BUCKET, Key=f"{POS_PENDING_PREFIX}{state}.json",
                  Body=json.dumps(pending).encode(), ContentType="application/json")


def _complete_pending_sale(state: str, transaction_id: str):
    pending = _load_pos_pending(state)
    if not pending:
        return JSONResponse(status_code=404, content={"error": "This sale wasn't found — check the Square app"})
    if pending.get("status") == "completed":
        return pending.get("result") or {}
    try:
        created = datetime.fromisoformat(pending["created"])
    except Exception:
        created = None
    if not created or datetime.now(timezone.utc) - created > POS_PENDING_MAX_AGE:
        return JSONResponse(status_code=410, content={"error": "This sale is too old to finish here — record it from the dashboard"})
    try:
        result = _record_pos_sale({**pending["sale"], "square_payment_id": transaction_id})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    if isinstance(result, JSONResponse):
        return result
    result = {**result, "email": pending["sale"].get("email", "")}
    _save_pos_pending(state, {**pending, "status": "completed", "result": result,
                              "completed": datetime.now(timezone.utc).isoformat()})
    return result


# ── SQUARE (Point of Sale handoff) ─────────────────────────────────────────────
# The iPad POS hands each sale to the Square app (tip + payment happen there) using
# Square's Point of Sale API. The server supplies the app ID and finds/creates the
# customer in the seller's Square account so they appear on the Square sale.
SQUARE_API_BASE = "https://connect.squareup.com/v2"


def _square_headers() -> dict:
    # No Square-Version header: Square then uses the app's version from the Developer Console.
    return {"Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}", "Content-Type": "application/json"}


class SquareTokenRejected(Exception):
    """Square refused the access token — payments couldn't be confirmed afterwards."""


def _square_customer_id(info: dict) -> tuple:
    """(Square customer id, problem) for this POS customer, matched by email and created if
    needed. problem is a short reason when the customer couldn't be attached (the sale can
    still go ahead without one). Raises SquareTokenRejected if Square refuses the token."""
    email = (info.get("email") or "").strip()
    if not email:
        return None, None
    r = http_requests.post(f"{SQUARE_API_BASE}/customers/search", headers=_square_headers(), timeout=6,
                           json={"query": {"filter": {"email_address": {"exact": email}}}, "limit": 1})
    if r.status_code == 401:
        raise SquareTokenRejected()
    if not r.ok:
        print(f"Square customer search failed ({r.status_code}): {r.text[:200]}")
        return None, f"Square customer search failed ({r.status_code})"
    found = r.json().get("customers") or []
    if found:
        return found[0].get("id"), None

    new_customer = {
        "idempotency_key": str(uuid.uuid4()),
        "given_name":  (info.get("first") or "").strip(),
        "family_name": (info.get("last") or "").strip(),
        "email_address": email,
    }
    phone = (info.get("phone") or "").strip()
    if phone:
        new_customer["phone_number"] = phone
    if (info.get("street") or "").strip():
        new_customer["address"] = {
            "address_line_1": info.get("street", "").strip(),
            "locality": (info.get("city") or "").strip(),
            "administrative_district_level_1": (info.get("state") or "").strip(),
            "postal_code": (info.get("zip") or "").strip(),
            "country": "US",
        }
    r = http_requests.post(f"{SQUARE_API_BASE}/customers", headers=_square_headers(), timeout=6, json=new_customer)
    if not r.ok and ("phone_number" in new_customer or "address" in new_customer):
        # Square is strict about phone/address formats — retry with name + email only
        print(f"Square customer create failed ({r.status_code}), retrying without phone/address: {r.text[:200]}")
        new_customer.pop("phone_number", None)
        new_customer.pop("address", None)
        new_customer["idempotency_key"] = str(uuid.uuid4())
        r = http_requests.post(f"{SQUARE_API_BASE}/customers", headers=_square_headers(), timeout=6, json=new_customer)
    if not r.ok:
        print(f"Square customer create failed ({r.status_code}): {r.text[:200]}")
        try:
            detail = (r.json().get("errors") or [{}])[0].get("detail") or ""
        except Exception:
            detail = ""
        return None, f"Square couldn't create the customer ({r.status_code}{': ' + detail if detail else ''})"
    return (r.json().get("customer") or {}).get("id"), None


def _square_verify_payment(transaction_id: str, expected_cents: int, attempts: int = 3) -> tuple:
    """Confirm with Square that this POS transaction was really paid.
    Returns (True, details) or (False, reason). The Point of Sale API's transaction_id is
    the Square order id; a just-finished order can take a moment to appear, so retry."""
    if not transaction_id or transaction_id == "COMP":
        return False, "No Square transaction ID came back (Square may be in offline mode)"
    order = None
    for attempt in range(attempts):
        r = http_requests.get(f"{SQUARE_API_BASE}/orders/{transaction_id}", headers=_square_headers(), timeout=8)
        if r.status_code == 401:
            return False, "Square rejected the access token"
        if r.ok:
            order = r.json().get("order") or {}
            break
        if r.status_code != 404:
            return False, f"Square answered {r.status_code} when looking up the payment"
        if attempt < attempts - 1:
            _time.sleep(2)
    if order is None:
        return False, "Square has no record of this transaction"
    if SQUARE_LOCATION_ID and order.get("location_id") and order["location_id"] != SQUARE_LOCATION_ID:
        return False, "The payment was taken at a different Square location"
    if order.get("state") not in ("COMPLETED", "OPEN"):
        return False, f"The Square order isn't paid (state: {order.get('state')})"
    tenders = order.get("tenders") or []
    if not tenders:
        return False, "The Square order has no payment on it"
    paid_cents = sum(int((t.get("amount_money") or {}).get("amount") or 0) for t in tenders)
    # Square adds resort tax and the tip on top, so paid must be at least the POS total
    if paid_cents < expected_cents:
        return False, f"Square shows ${paid_cents / 100:.2f} paid, less than the ${expected_cents / 100:.2f} sale"
    return True, {"paid_cents": paid_cents,
                  "tip_cents": int((order.get("total_tip_money") or {}).get("amount") or 0),
                  "tax_cents": int((order.get("total_tax_money") or {}).get("amount") or 0),
                  "tender_types": [t.get("type") for t in tenders],
                  **_square_fee_info(order)}


# Payment types Square takes no processing fee on
_SQUARE_NO_FEE_TENDERS = {"CASH", "OTHER", "NO_SALE", "SQUARE_GIFT_CARD"}


def _square_fee_info(order: dict) -> dict:
    """Square's processing fee from an order's payments. Square fills the fee in about
    ten seconds after a card payment, so fee_final says whether it's there yet."""
    tenders = order.get("tenders") or []
    fee_cents, final = 0, True
    for t in tenders:
        fee = t.get("processing_fee_money")
        if fee is not None:
            fee_cents += int(fee.get("amount") or 0)
        elif t.get("type") not in _SQUARE_NO_FEE_TENDERS:
            final = False
    return {"fee_cents": fee_cents, "fee_final": final,
            "payment_ids": [t["id"] for t in tenders if t.get("id")]}


def _square_receipt(payment_id: str) -> dict:
    """Square's receipt number (the short code shown in Square's Transactions list) and
    receipt link for a payment. Best effort: an empty dict if Square can't say."""
    try:
        r = http_requests.get(f"{SQUARE_API_BASE}/payments/{payment_id}", headers=_square_headers(), timeout=6)
        if r.ok:
            p = r.json().get("payment") or {}
            return {k: v for k, v in (("square_receipt_number", p.get("receipt_number")),
                                      ("square_receipt_url", p.get("receipt_url"))) if v}
    except Exception as e:
        print(f"Square receipt lookup failed ({payment_id}): {e}")
    return {}


def _refresh_square_fee(order_record: dict) -> dict:
    """Fill in the Square fee (and receipt number) on a recorded POS sale once Square
    has them. Saves and returns the updated record; unchanged if Square isn't ready."""
    try:
        r = http_requests.get(f"{SQUARE_API_BASE}/orders/{order_record['square_payment_id']}",
                              headers=_square_headers(), timeout=6)
        if not r.ok:
            return order_record
        info = _square_fee_info(r.json().get("order") or {})
    except Exception as e:
        print(f"Square fee lookup failed ({order_record.get('id')}): {e}")
        return order_record
    updated = {**order_record, "square_payment_ids": info["payment_ids"]}
    if info["fee_final"]:
        updated.update(square_fee=info["fee_cents"] / 100, square_fee_final=True)
    if not updated.get("square_receipt_number") and info["payment_ids"]:
        updated.update(_square_receipt(info["payment_ids"][0]))
    if updated != order_record:
        _store_pos_order(updated)
    return updated


def _fill_pending_square_fees(pos_orders: list) -> list:
    """Before showing orders: look up fees Square hadn't filled in when the sale was
    recorded (e.g. the server restarted before the follow-up check ran)."""
    now = datetime.now(timezone.utc)
    def due(o):
        if not o.get("square_verified") or o.get("square_fee_final") or not o.get("square_payment_id"):
            return False
        try:
            age = now - datetime.fromisoformat(o["created"])
        except Exception:
            return False
        return timedelta(seconds=20) < age < timedelta(days=90)
    todo = [i for i, o in enumerate(pos_orders) if due(o)]
    if todo and SQUARE_ACCESS_TOKEN:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, rec in zip(todo, pool.map(lambda i: _refresh_square_fee(pos_orders[i]), todo)):
                pos_orders[i] = rec
    return pos_orders


# ── POS PRODUCTS (Base Area, Mats custom items) ────────────────────────────────
POS_PRODUCTS_KEY = "meta/pos_products.json"

def _load_pos_products():
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=POS_PRODUCTS_KEY)
        d = json.loads(obj["Body"].read())
    except Exception:
        d = {}
    d.setdefault("base_area", [])
    d.setdefault("mats", [])
    d.setdefault("base_area_tiers", [])
    d.setdefault("satin_black_frames", [])
    return d

def _save_pos_products(d):
    s3.put_object(Bucket=R2_BUCKET, Key=POS_PRODUCTS_KEY,
                  Body=json.dumps(d, indent=2).encode(), ContentType="application/json")

# ── Customer directory (pos_data/customers.json) ──────────────────────────────
CUSTOMERS_KEY = "pos_data/customers.json"
_customers_cache: dict = {"data": None}
_customers_lock = threading.RLock()
_CUSTOMER_CONTACT_FIELDS = ("first", "last", "email", "phone", "street", "city", "state", "zip")

def _new_customer_id() -> str:
    return uuid.uuid4().hex[:12]

def _load_customers() -> list:
    """The customer directory, cached in memory.
    Raises if R2 can't be read — never treats a failed read as an empty directory,
    so a later save can't wipe it. Records without a permanent id get one, saved once."""
    with _customers_lock:
        if _customers_cache["data"] is None:
            try:
                obj = s3.get_object(Bucket=R2_BUCKET, Key=CUSTOMERS_KEY)
                customers = json.loads(obj["Body"].read())
            except s3.exceptions.NoSuchKey:
                customers = []
            missing_ids = [c for c in customers if not c.get("id")]
            for c in missing_ids:
                c["id"] = _new_customer_id()
            _customers_cache["data"] = customers
            if missing_ids:
                _save_customers(customers)
        return _customers_cache["data"]

def _save_customers(customers: list) -> None:
    with _customers_lock:
        s3.put_object(
            Bucket=R2_BUCKET, Key=CUSTOMERS_KEY,
            Body=json.dumps(customers, ensure_ascii=False).encode(),
            ContentType="application/json",
        )
        _customers_cache["data"] = customers

def _warm_customers():
    """Load the customer directory in the background at startup, so the first POS
    customer search after a restart or deploy doesn't wait for the 5-6 MB file."""
    _data_ready.wait()
    try:
        print(f"Loaded {len(_load_customers())} customers.")
    except Exception as e:
        print(f"Customer directory not preloaded ({e}); it will load on first search.")

threading.Thread(target=_warm_customers, daemon=True).start()

def _customer_by_email(customers: list, email: str):
    e = (email or "").strip().lower()
    if not e:
        return None
    return next((c for c in customers if (c.get("email") or "").strip().lower() == e), None)

def _customer_name_matches(c: dict, q: str) -> bool:
    """POS lookup: first/last name prefix; two words match first+last in either order."""
    ql = q.lower().strip()
    parts = ql.split()
    fn = (c.get("first") or "").lower()
    ln = (c.get("last") or "").lower()
    if len(parts) >= 2:
        return (fn.startswith(parts[0]) and ln.startswith(parts[1])) \
            or (fn.startswith(parts[1]) and ln.startswith(parts[0]))
    return fn.startswith(ql) or ln.startswith(ql)

def _customer_admin_matches(c: dict, q: str) -> bool:
    """Studio search: name prefix, or email / phone containing the text."""
    if _customer_name_matches(c, q):
        return True
    ql = q.lower().strip()
    if ql in (c.get("email") or "").lower():
        return True
    digits = re.sub(r"\D", "", ql)
    return len(digits) >= 3 and digits in re.sub(r"\D", "", c.get("phone") or "")

def _upsert_customer_from_pos(info: dict) -> None:
    """Add a POS customer to the directory, matched by email.
    For an existing customer, only blank fields are filled in — nothing already on
    file is overwritten (two people can share one email address). Changes to existing
    details are made in the studio CRM. Never touches notes, tags or follow-up history."""
    email = (info.get("email") or "").strip()
    if not email:
        return
    fields = {k: str(info.get(k) or "").strip() for k in _CUSTOMER_CONTACT_FIELDS if k != "email"}
    with _customers_lock:
        customers = _load_customers()
        existing = _customer_by_email(customers, email)
        if existing:
            changed = False
            for k, v in fields.items():
                if v and not (existing.get(k) or "").strip():
                    existing[k] = v
                    changed = True
            if changed:
                _save_customers(customers)
        else:
            customers.append({
                "id": _new_customer_id(), "email": email, **fields,
                "source": "pos", "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })
            _save_customers(customers)

# ── Customer directory: studio (admin) editing, one record at a time ──────────

def _clean_customer_fields(body: dict) -> dict:
    out = {}
    for k in _CUSTOMER_CONTACT_FIELDS:
        if k in body:
            out[k] = str(body.get(k) or "").strip()
    if "notes" in body:
        out["notes"] = str(body.get("notes") or "").strip()
    if "tags" in body:
        tags = body.get("tags") or []
        out["tags"] = [str(t).strip() for t in tags if str(t).strip()] if isinstance(tags, list) else []
    return out

def _build_followup_email(email: str, body: dict):
    """Build a follow-up email for one customer.
    Returns (ftype, subject, html, text), or a JSONResponse describing what's missing."""
    ftype = body.get("type")
    if ftype not in ("review", "discount", "custom"):
        return JSONResponse(status_code=400, content={"error": "type must be review, discount, or custom"})

    # Load customer for personalisation
    customer = _customer_by_email(_load_customers(), email)
    first_name = (customer.get("first") or "").strip() if customer else ""
    greeting   = f"Hi {_html_escape(first_name)}," if first_name else "Hi,"

    if ftype == "review":
        # Sent from the Review Request template (Settings > Email Templates),
        # using the Google review link saved on that template.
        tmpl = _load_email_templates().get("review_request") or _EMAIL_TEMPLATE_DEFAULTS["review_request"]
        review_url = (body.get("review_url") or tmpl.get("review_url") or "").strip()
        if not review_url:
            return JSONResponse(status_code=400, content={
                "error": "Add your Google review link in Settings > Email Templates > Review Request"})
        plain_vars = {"first_name": first_name or "there", "review_url": review_url,
                      "review_button": f"Leave us a review: {review_url}"}
        subject = tmpl["subject"]
        for k, v in plain_vars.items():
            subject = subject.replace(f"{{{{{k}}}}}", v)
        html_inner, text_inner = _render_email_body(
            tmpl["body"], plain_vars, {"review_button": _review_button_html(review_url)})

    elif ftype == "discount":
        code = (body.get("discount_code") or "").strip().upper()
        if not code:
            return JSONResponse(status_code=400, content={"error": "discount_code is required"})
        codes    = _load_discount_codes()
        code_obj = next((c for c in codes if c.get("code", "").upper() == code), None)
        if not code_obj:
            return JSONResponse(status_code=400, content={"error": f"Code '{code}' not found"})
        # Sent from the Discount Code template (Settings > Email Templates)
        tmpl = _load_email_templates().get("discount_code") or _EMAIL_TEMPLATE_DEFAULTS["discount_code"]
        detail = _discount_detail(code_obj)
        plain_vars = {"first_name": first_name or "there", "code": code, "discount_amount": detail,
                      "code_box": f"Your discount code: {code}" + (f" ({detail})" if detail else "")
                                  + f"\nShop now: {SITE_URL}"}
        subject = tmpl["subject"]
        for k, v in plain_vars.items():
            subject = subject.replace(f"{{{{{k}}}}}", v)
        html_inner, text_inner = _render_email_body(
            tmpl["body"], plain_vars, {"code_box": _code_box_html(code, detail)})

    else:  # custom
        subject = (body.get("subject") or "").strip()
        message = (body.get("message") or "").strip()
        if not subject:
            return JSONResponse(status_code=400, content={"error": "subject is required"})
        if not message:
            return JSONResponse(status_code=400, content={"error": "message is required"})
        html_inner = f"<p style='margin:0 0 .8rem'>{greeting}</p>" + \
            "".join(f"<p style='margin:0 0 .8rem'>{line}</p>" for line in message.split("\n") if line.strip())
        text_inner = (f"Hi {first_name}," if first_name else "Hi,") + f"\n\n{message}"

    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f9f9f9;font-family:Arial,sans-serif">
<div style="max-width:560px;margin:0 auto;padding:2rem 1rem">
  <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:2rem">
    <p style="margin:0 0 1.5rem;font-size:.78rem;color:#999;text-transform:uppercase;letter-spacing:.5px">Crystal Images</p>
    {html_inner}
    <hr style="border:none;border-top:1px solid #eee;margin:1.5rem 0 1rem">
    <p style="margin:0;font-size:.75rem;color:#aaa">Questions? <a href="mailto:info@bigskyphotos.com" style="color:#1a73e8">info@bigskyphotos.com</a></p>
    {_marketing_footer_html(email)}
  </div>
</div></body></html>"""

    return ftype, subject, html, text_inner + _marketing_footer_text(email)


def _find_pos_order(order_id: str, date: str, token: str = ""):
    """A recorded POS sale by id + date (one read), or by its download token (older POS pages)."""
    if re.fullmatch(r"pos-[A-Za-z0-9]{4,32}", order_id) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        try:
            obj = s3.get_object(Bucket=R2_BUCKET, Key=f"pos_orders/{date}_{order_id}.json")
            return json.loads(obj["Body"].read())
        except Exception:
            return None
    if re.fullmatch(r"[A-Za-z0-9]{16,64}", token):
        return next((o for o in _fetch_pos_orders() if o.get("package_token") == token), None)
    return None


def _resend_pos_receipt(order: dict) -> str:
    """Send a recorded sale's receipt again. Returns a note for staff ('' if nothing to add)."""
    download_url = expire_str = None
    note = ""
    token = order.get("package_token")
    if token:
        pkg = _load_pkg(token)
        try:
            expires = datetime.fromisoformat(pkg["expires"]) if pkg else None
        except Exception:
            expires = None
        if expires and datetime.now(timezone.utc) <= expires:
            download_url = f"{SITE_URL}/package/{token}"
            expire_str = expires.strftime("%B %d, %Y")
        else:
            note = "The download link has expired, so the receipt was sent without it"
    package_note = ""
    if download_url and (pkg or {}).get("unlimited"):
        expire_str = _mountain_date_str(pkg["expires"])
        package_note = _unlimited_note(pkg["unlimited"].get("last_day", ""), expire_str)
    square = None
    if order.get("square_verified"):
        square = {k: order.get(k) for k in ("square_paid", "square_tax", "square_tip")}
    _send_pos_receipt_email(
        to_email=order.get("email", ""), customer_name=order.get("name", ""), order_id=order.get("id", ""),
        items=order.get("items") or [], total=float(order.get("total") or 0),
        coupon_code=order.get("coupon_code", ""), coupon_discount=float(order.get("coupon_discount") or 0),
        download_url=download_url, expire_str=expire_str, square=square,
        comp=(order.get("square_payment_id") == "COMP"), package_note=package_note)
    return note


# ─────────────────────────────────────────
# WEBHOOK + DIGITAL DELIVERY
# ─────────────────────────────────────────

WC_WEBHOOK_SECRET  = os.getenv("WC_WEBHOOK_SECRET", "")
if not WC_WEBHOOK_SECRET:
    print("WARNING: WC_WEBHOOK_SECRET is not set — the order webhook will refuse every message, "
          "so paid online orders won't get their download emails")
DOWNLOAD_EXPIRE_DAYS = 30
SITE_URL = "https://photos.bigskyphotos.com"

def _store_token(token: str, payload: dict):
    body = json.dumps(payload).encode()
    s3.put_object(Bucket=R2_BUCKET, Key=f"downloads/{token}.json",
                  Body=body, ContentType="application/json")
    # Write a per-order lookup so _get_best_token_for_order needs only 2 R2 reads
    oid = str(payload.get("order_id", ""))
    if oid:
        try:
            idx = {"token": token, "expires": payload.get("expires", "")}
            s3.put_object(Bucket=R2_BUCKET, Key=f"downloads/by_order/{oid}.json",
                          Body=json.dumps(idx).encode(), ContentType="application/json")
        except Exception:
            pass

def _load_token(token: str):
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"downloads/{token}.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None

def _handle_paid_wc_order(order: dict):
    """A paid online order: create the download link and have WooCommerce email it."""
    # Act on processing (manual/physical orders) or completed (auto-completed digital orders)
    if order.get("status") not in ("processing", "completed"):
        return Response(status_code=200)
    # WooCommerce calls this when an order is paid and again when it's completed
    # (often seconds apart). One link and one email per order: the second call waits
    # for the first, then sees the order already has a valid link.
    try:                                     # their saved cart is done once they've ordered
        _clear_saved_cart_for((order.get("billing") or {}).get("email", ""))
    except Exception as e:
        print(f"Saved cart not cleared for order {order.get('id')}: {e}")
    try:                                     # prints of partner photos: the partner's report
        _log_partner_online_prints(order)
    except Exception as e:
        print(f"Partner prints not logged for order {order.get('id')}: {e}")
    with _sale_locks.hold(f"wc:{order.get('id')}"):
        if _order_has_valid_link(order.get("id")):
            return Response(status_code=200)
        return _deliver_wc_order(order)


def _order_has_valid_link(order_id) -> bool:
    if not order_id:
        return False
    try:
        idx = json.loads(s3.get_object(Bucket=R2_BUCKET, Key=f"downloads/by_order/{order_id}.json")["Body"].read())
        return bool(idx.get("token")) and datetime.fromisoformat(idx["expires"]) > datetime.now(timezone.utc)
    except Exception:
        return False


def _deliver_wc_order(order: dict):
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

    # Record coupon usage from our own meta store
    wc_coupon = meta.get("_coupon_code", "")
    wc_coupon_discount = float(meta.get("_coupon_discount", 0) or 0)
    if wc_coupon and wc_coupon_discount > 0:
        try:
            _record_coupon_use(wc_coupon.upper(), {
                "date":         datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "order_id":     str(order_id),
                "email":        customer_email,
                "amount_saved": wc_coupon_discount,
                "order_total":  float(order.get("total", 0)),
                "source":       "web",
            })
        except Exception:
            pass

    # Record bundle deal usage
    wc_bundle_pct     = float(meta.get("_bundle_discount_pct", 0) or 0)
    wc_bundle_savings = float(meta.get("_bundle_savings", 0) or 0)
    if wc_bundle_pct > 0:
        try:
            _record_bundle_use({
                "date":         datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "order_id":     str(order_id),
                "email":        customer_email,
                "amount_saved": wc_bundle_savings,
                "pct_off":      wc_bundle_pct,
                "order_total":  float(order.get("total", 0)),
                "source":       "web",
            })
        except Exception:
            pass

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


# ─────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────

ADMIN_PASSWORD  = os.getenv("ADMIN_PASSWORD", "")
ADMIN_COOKIE    = "ci_admin_session"
STRIPE_RATE     = 0.029
STRIPE_FIXED    = 0.30


# ── Studio sign-in ────────────────────────────────────────────────────────────
# Each sign-in is its own session (90 days), kept in R2 so it can be ended: signing
# out ends this device's session; "sign out all devices" ends every session and
# forgets every approved device. A device is approved the first time it signs in
# with the password plus a 6-digit code emailed to the owner; approved devices then
# only need the password (for 90 days). Stored values are hashes, never the tokens.
ADMIN_AUTH_KEY        = "meta/admin_auth.json"
ADMIN_DEVICE_COOKIE   = "ci_admin_device"
ADMIN_SESSION_DAYS    = 90
ADMIN_DEVICE_DAYS     = 90
OWNER_EMAIL           = os.getenv("OWNER_EMAIL", "orenlewk@gmail.com")
# Safety valve: set ADMIN_LOGIN_CODES=off on Railway to sign in without a code
# (e.g. if email is down). Approved devices never need a code anyway.
ADMIN_LOGIN_CODES_ON  = os.getenv("ADMIN_LOGIN_CODES", "on").strip().lower() != "off"
LOGIN_CODE_MINUTES    = 10
LOGIN_CODE_TRIES      = 5
_admin_auth_lock      = threading.Lock()
_admin_auth_cache     = {"data": None}
_login_challenges: dict = {}      # challenge id -> pending code (lives in memory, 10 minutes)
_admin_pw_fail_limiter = _RateLimiter(max_calls=10, window=600)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_admin_auth() -> dict:
    with _admin_auth_lock:
        if _admin_auth_cache["data"] is None:
            try:
                d = json.loads(s3.get_object(Bucket=R2_BUCKET, Key=ADMIN_AUTH_KEY)["Body"].read())
            except s3.exceptions.NoSuchKey:
                d = {}
            d.setdefault("sessions", {})
            d.setdefault("devices", {})
            d.setdefault("pos_devices", {})
            d.setdefault("store_screens", {})
            _admin_auth_cache["data"] = d
        return _admin_auth_cache["data"]


def _save_admin_auth(d: dict):
    """Drop anything expired, then save (caller holds _admin_auth_lock)."""
    now = _now_iso()
    d["sessions"] = {k: v for k, v in d["sessions"].items() if v.get("expires", "") > now}
    d["devices"] = {k: v for k, v in d["devices"].items() if v.get("expires", "") > now}
    d["pos_devices"] = {k: v for k, v in d.get("pos_devices", {}).items() if v.get("expires", "") > now}
    d["store_screens"] = {k: v for k, v in d.get("store_screens", {}).items() if v.get("expires", "") > now}
    s3.put_object(Bucket=R2_BUCKET, Key=ADMIN_AUTH_KEY, Body=json.dumps(d, indent=1).encode(),
                  ContentType="application/json")
    _admin_auth_cache["data"] = d


def _request_info(request: Request) -> dict:
    return {"device": _describe_device(request.headers.get("user-agent", "")), "ip": _client_ip(request)}


def _describe_device(ua: str) -> str:
    """'Mac · Chrome' from a browser's user-agent string (shown in emails and the device list)."""
    ua = ua or ""
    system = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
              else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua
              else "Unknown device")
    browser = ("Edge" if "Edg/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else
               "Firefox" if ("Firefox/" in ua or "FxiOS" in ua) else "Safari" if "Safari/" in ua else "browser")
    return f"{system} · {browser}"


def _device_approved(request: Request) -> str | None:
    """This device's id hash if it was approved with an emailed code (and not forgotten)."""
    token = request.cookies.get(ADMIN_DEVICE_COOKIE, "")
    if not token:
        return None
    h = _token_hash(token)
    dev = _load_admin_auth()["devices"].get(h)
    return h if dev and dev.get("expires", "") > _now_iso() else None


def _create_admin_session(device_hash: str = "", info: dict = None) -> str:
    """A new studio session (90 days). Returns the token for the session cookie."""
    token = secrets.token_urlsafe(32)
    info = info or {}
    d = _load_admin_auth()
    with _admin_auth_lock:
        d["sessions"][_token_hash(token)] = {
            "device": device_hash, "created": _now_iso(),
            "expires": (datetime.now(timezone.utc) + timedelta(days=ADMIN_SESSION_DAYS)).isoformat(),
            "describe": info.get("device", ""), "ip": info.get("ip", "")}
        if device_hash and device_hash in d["devices"]:
            d["devices"][device_hash]["last_signin"] = _now_iso()
        _save_admin_auth(d)
    return token


def _admin_authed(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE, "")
    if not token:
        return False
    s = _load_admin_auth()["sessions"].get(_token_hash(token))
    return bool(s) and s.get("expires", "") > _now_iso()


def _signed_in_response(next_url: str, device_token: str = None, device_hash: str = "", info: dict = None):
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(ADMIN_COOKIE, _create_admin_session(device_hash, info), httponly=True, samesite="strict",
                    max_age=ADMIN_SESSION_DAYS * 86400)
    if device_token:
        resp.set_cookie(ADMIN_DEVICE_COOKIE, device_token, httponly=True, samesite="strict",
                        max_age=ADMIN_DEVICE_DAYS * 86400)
    return resp


def _send_login_code_email(code: str, info: dict) -> bool:
    if not RESEND_API_KEY:
        print(f"[sign-in code] {code} (RESEND_API_KEY not set, so it wasn't emailed)")
        return False
    try:
        from zoneinfo import ZoneInfo
        when = datetime.now(ZoneInfo("America/Denver")).strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except Exception:
        when = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    body_html = (
        f'<p>Someone entered your studio password on a <strong>new device</strong>:</p>'
        f'<p style="margin:0 0 1rem;color:#555">{_html_escape(info.get("device", ""))} &middot; '
        f'internet address {_html_escape(info.get("ip", ""))} &middot; {when} (Mountain)</p>'
        f'<p>Your sign-in code:</p>'
        f'<p style="font-size:2rem;font-weight:700;letter-spacing:.3rem;margin:.5rem 0 1rem">{code}</p>'
        f'<p style="color:#555">It works for {LOGIN_CODE_MINUTES} minutes. After that, this device stays approved for '
        f'{ADMIN_DEVICE_DAYS} days.</p>'
        f'<p style="color:#b91c1c"><strong>If this wasn\'t you,</strong> don\'t share the code, and change the studio '
        f'password (ADMIN_PASSWORD on Railway).</p>')
    try:
        r = http_requests.post("https://api.resend.com/emails",
                               headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                               json={"from": RESEND_FROM, "to": [OWNER_EMAIL],
                                     "subject": f"Studio sign-in code: {code}",
                                     "html": _wrap_email_html(body_html),
                                     "text": f"Studio sign-in code: {code} (new device: {info.get('device', '')}, "
                                             f"{info.get('ip', '')}, {when}). If this wasn't you, change the studio password."},
                               timeout=10)
        if not r.ok:
            print(f"Sign-in code email failed: {r.text}")
        return r.ok
    except Exception as e:
        print(f"Sign-in code email failed: {e}")
        return False


def _masked_email(e: str) -> str:
    name, _, domain = e.partition("@")
    return (name[:2] + "•" * max(1, len(name) - 2) + "@" + domain) if domain else e


def _safe_next(url) -> str:
    """Where to go after signing in: only a page on this site. A link like
    /admin?next=https://elsewhere could otherwise send you to another site."""
    url = str(url or "")
    if (not url.startswith("/") or url.startswith("//") or url.startswith("/\\")
            or any(c in url for c in "\r\n\t\\") or len(url) > 500):
        return "/admin/dashboard"
    return url


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

def _get_best_token_for_order(order_id):
    """Return (token, rec, expires_dt) for the order's download token.
    Fast path (2 R2 reads): checks the per-order index written by _store_token.
    Slow fallback (N reads): full scan for orders created before the index existed."""
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"downloads/by_order/{order_id}.json")
        idx = json.loads(obj["Body"].read())
        token = idx.get("token")
        if token:
            rec = _load_token(token)
            if rec:
                exp = datetime.fromisoformat(rec["expires"])
                return token, rec, exp
    except Exception:
        pass
    # Fallback: scan all tokens (only hits for orders predating this index)
    best_token, best_rec, best_exp = None, None, None
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="downloads/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if "/by_order/" in key:
                    continue
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
    # Write the index now so next lookup is instant
    if best_token and best_rec:
        try:
            idx = {"token": best_token, "expires": best_rec.get("expires", "")}
            s3.put_object(Bucket=R2_BUCKET, Key=f"downloads/by_order/{order_id}.json",
                          Body=json.dumps(idx).encode(), ContentType="application/json")
        except Exception:
            pass
    return best_token, best_rec, best_exp

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
    coupon_code  = ", ".join(c.get("code","") for c in coupon_lines) if coupon_lines else meta.get("_coupon_code", "")
    return {
        "order_id":    order.get("id"),
        "order_no":    f"#{order.get('number') or order.get('id')}",
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

def _fetch_wc_orders_all(after: str = None, before: str = None, max_pages: int = 20):
    """Paginate through WooCommerce completed orders, returning all results."""
    all_orders = []
    for page in range(1, max_pages + 1):
        params = {"status": "completed", "per_page": 100, "page": page,
                  "orderby": "date", "order": "desc"}
        if after:  params["after"]  = after
        if before: params["before"] = before
        try:
            r = http_requests.get(f"{WC_BASE}/orders", params=params,
                                  auth=(WC_KEY, WC_SECRET), timeout=20)
            if not r.ok:
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            all_orders.extend(batch)
            if len(batch) < 100:
                break
        except Exception:
            break
    return all_orders


# ── POS order storage ─────────────────────────────────────────────────────────

def _store_pos_order(record: dict):
    key = f"pos_orders/{record['date']}_{record['id']}.json"
    s3.put_object(Bucket=R2_BUCKET, Key=key,
                  Body=json.dumps(record).encode(), ContentType="application/json")

_POS_PREFIX = "pos_orders/"
_pos_order_cache: dict = {}        # storage key -> (ETag, sale record); a sale is re-read only when its file changes
_pos_order_cache_lock = threading.Lock()


def _fetch_pos_orders(after: str = None, before: str = None) -> list:
    """In-store sales, newest first, optionally only those dated after..before (YYYY-MM-DD).
    Sale files are named by date, so out-of-range ones aren't opened; sales already read are
    remembered by their file's ETag; new or changed ones are read in parallel."""
    a, b = (after or "")[:10], (before or "")[:10]
    listed = []
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=_POS_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                day = key[len(_POS_PREFIX):len(_POS_PREFIX) + 10]
                if (a and day < a) or (b and day > b):
                    continue
                listed.append((key, obj.get("ETag", "")))
    except Exception:
        pass
    with _pos_order_cache_lock:
        stale = [(k, e) for k, e in listed if _pos_order_cache.get(k, ("",))[0] != e or not e]

    def read(key_etag):
        key, etag = key_etag
        try:
            rec = json.loads(s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read())
        except Exception:
            return
        with _pos_order_cache_lock:
            _pos_order_cache[key] = (etag, rec)
    if stale:
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(read, stale))
    with _pos_order_cache_lock:
        # (older sales stored the staff member's PIN; it's never handed out)
        orders = [{f: v for f, v in _pos_order_cache[k][1].items() if f != "photographer_pin"}
                  for k, _ in listed if k in _pos_order_cache]
    if after or before:
        orders = [o for o in orders
                  if (not a or o.get("date","") >= a)
                  and (not b or o.get("date","") <= b)]
    return sorted(orders, key=lambda o: o.get("created",""), reverse=True)

# ── Refunds (recorded by hand) ────────────────────────────────────────────────
# Refunds are done in Square or WooCommerce; the studio records them here so they
# come off revenue, net and commission. Keyed "pos:<sale id>" or "wc:<order id>".
ORDER_REFUNDS_KEY = "meta/order_refunds.json"
_order_refunds_lock = threading.Lock()
_REFUND_KEY_RE = re.compile(r"^(pos|wc):[A-Za-z0-9_-]{1,64}$")


def _load_order_refunds(strict: bool = False) -> dict:
    """strict: raise on a storage error (before saving), instead of treating it as no refunds."""
    try:
        return json.loads(s3.get_object(Bucket=R2_BUCKET, Key=ORDER_REFUNDS_KEY)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception as e:
        if strict:
            raise
        print(f"Couldn't read order refunds: {e}")
        return {}


def _save_order_refunds(refunds: dict):
    s3.put_object(Bucket=R2_BUCKET, Key=ORDER_REFUNDS_KEY,
                  Body=json.dumps(refunds, indent=1).encode(), ContentType="application/json")


def _refund_key(row: dict) -> str:
    return f"{'pos' if row.get('source') == 'pos' else 'wc'}:{row.get('order_id')}"


def _apply_refunds(rows: list, refunds: dict) -> list:
    """Mark refunded orders and take the refund off what they count for. A full refund
    counts for nothing; a partial one comes off the amount charged and the net."""
    for r in rows:
        r["refund_key"] = _refund_key(r)
        r["charged_original"] = float(r.get("charged", r["total"]))
        rf = refunds.get(r["refund_key"])
        if not rf:
            r["refunded"] = 0.0
            continue
        amount = round(float(rf.get("amount") or 0), 2)
        r.update(refunded=amount, refund_full=bool(rf.get("full")),
                 refund_date=(rf.get("date") or "")[:10], refund_note=rf.get("note") or "")
        if r["refund_full"]:
            r.update(charged=0.0, net=0.0, tip=0.0)
        else:
            r.update(charged=round(max(r["charged_original"] - amount, 0), 2), net=round(r["net"] - amount, 2))
    return rows


def _commission_amount(row: dict, refunds: dict) -> float:
    """What a sale counts for toward commission: nothing if refunded in full."""
    amount = float(row.get("total", 0))
    rf = refunds.get(_refund_key(row))
    if rf:
        amount = 0.0 if rf.get("full") else max(amount - float(rf.get("amount") or 0), 0.0)
    return amount


def _pos_order_row(o: dict) -> dict:
    total    = float(o.get("total", 0))           # POS total — already after any coupon
    discount = float(o.get("coupon_discount", 0))
    is_comp  = o.get("square_payment_id") == "COMP"
    tip      = float(o.get("square_tip") or 0)
    tax      = float(o.get("square_tax") or 0)
    # What the customer actually paid: Square's verified amount (tax + tip included);
    # nothing for comps; for older sales without Square data, the POS total.
    charged  = 0.0 if is_comp else float(o.get("square_paid") or total)
    fee      = float(o.get("square_fee") or 0)
    fee_pending = bool(o.get("square_verified")) and not o.get("square_fee_final")
    name     = o.get("name", "")
    parts    = name.split(None, 1) if name else ["", ""]
    return {
        "order_id":    o.get("id"),
        "date":        o.get("date", o.get("created","")[:10]),
        "first":       parts[0],
        "last":        parts[1] if len(parts) > 1 else "",
        "email":       o.get("email",""),
        "what":        f"In-Person — {o.get('location','')}",
        "filenames":   o.get("filenames",""),
        "location":    o.get("location",""),
        "photo_date":  o.get("photo_date",""),
        "total":       total,
        "discount":    discount,
        "stripe_fee":  fee,                        # Square's processing fee
        "fee_pending": fee_pending,
        "net":         0.0 if is_comp else round(total - fee, 2),
        "order_no":    o.get("square_receipt_number", ""),
        "receipt_url": o.get("square_receipt_url", ""),
        "shipping":    0,
        "ship_addr":   "",
        "coupon_code": o.get("coupon_code",""),
        "photo_paths": o.get("photo_paths",""),
        "source":      "pos",
        "photographer": o.get("photographer_name",""),
        "charged":     round(charged, 2),
        "tip":         round(tip, 2),
        "tax":         round(tax, 2),
        "is_comp":     is_comp,
        "package_token": o.get("package_token", ""),
        "unlimited":   bool(o.get("unlimited")),
    }


# ─────────────────────────────────────────
# PHOTO MANAGEMENT — HELPERS & DATA
# ─────────────────────────────────────────

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
def _load_discount_codes(): return _r2_json_load("meta/discount_codes.json", [])
def _save_discount_codes(d):_r2_json_save("meta/discount_codes.json", d)

_discount_codes_lock = threading.RLock()


def _record_coupon_use(code_upper: str, use: dict):
    """Count a use of a discount code. Checkouts can now run side by side, so the
    read-modify-write is done under a lock (a use is never lost to another save)."""
    with _discount_codes_lock:
        _record_coupon_use_locked(code_upper, use)


def _record_coupon_use_locked(code_upper: str, use: dict):
    codes = _load_discount_codes()
    for c in codes:
        if c.get("code","").upper() == code_upper:
            c["usage_count"] = int(c.get("usage_count", 0)) + 1
            c["total_saved"] = round(float(c.get("total_saved", 0)) + float(use.get("amount_saved", 0)), 2)
            uses = c.get("uses", [])
            uses.append(use)
            c["uses"] = uses[-500:]   # cap history at 500 entries
            break
    _save_discount_codes(codes)

def _record_bundle_use(use: dict):
    """Record a bundle deal use — upserts a synthetic BUNDLE_DEAL entry."""
    with _discount_codes_lock:
        _record_bundle_use_locked(use)


def _record_bundle_use_locked(use: dict):
    codes = _load_discount_codes()
    entry = next((c for c in codes if c.get("code") == "BUNDLE_DEAL"), None)
    if not entry:
        entry = {
            "id":           "BUNDLE_DEAL",
            "code":         "BUNDLE_DEAL",
            "label":        "Bundle Deal (auto-applied)",
            "discount_type": "percent",
            "amount":       0,
            "applies_to":   "all",
            "active":       True,
            "is_system":    True,
            "usage_count":  0,
            "total_saved":  0.0,
            "uses":         [],
        }
        codes.insert(0, entry)
    entry["usage_count"] = int(entry.get("usage_count", 0)) + 1
    entry["total_saved"] = round(float(entry.get("total_saved", 0)) + float(use.get("amount_saved", 0)), 2)
    uses = entry.get("uses", [])
    uses.append(use)
    entry["uses"] = uses[-500:]
    _save_discount_codes(codes)

import random, string as _string
def _generate_code(prefix: str = "", length: int = 6) -> str:
    chars = _string.ascii_uppercase + _string.digits
    suffix = "".join(random.choices(chars, k=length))
    return f"{prefix.upper()}{suffix}" if prefix else suffix

def _auth_photographer(pin: str):
    pin = str(pin or "").strip()
    if not pin:                       # a photographer saved without a PIN must not match an empty one
        return None
    for p in _load_photographers():
        if hmac.compare_digest(str(p.get("pin") or ""), pin):
            return p
    return None

# Failed PIN attempts per IP — 10 wrong PINs in 10 minutes locks that IP out
# of PIN-protected endpoints until the window passes.
_pin_fail_limiter = _RateLimiter(max_calls=10, window=600)

_PROXY_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]


def _client_ip(request: Request) -> str:
    """The visitor's real address, for per-visitor limits and lockouts.

    Behind Railway, request.client is one of Railway's internal proxy addresses
    (100.64.x.x), each shared by many visitors, so limits keyed on it were shared.
    Railway passes the visitor's address in X-Forwarded-For; each proxy adds the
    address it saw to the END, so the right-most public address is the one Railway
    saw. A visitor can put fake entries at the start, but can't change that one."""
    for part in reversed(request.headers.get("x-forwarded-for", "").split(",")):
        try:
            addr = ipaddress.ip_address(part.strip())
        except ValueError:
            continue
        if not any(addr in net for net in _PROXY_NETS):
            return str(addr)
    return request.client.host if request.client else "unknown"

def _pin_locked_out(request: Request) -> bool:
    return _pin_fail_limiter.count(_client_ip(request)) >= _pin_fail_limiter.max_calls

def _record_pin_failure(request: Request):
    _pin_fail_limiter.is_allowed(_client_ip(request))

# ── Owner PIN (the Pricing and Admin pages) ────────────────────────────────────
# On top of the studio login, what the Pricing and Admin (photographers) pages save,
# and the photographer list itself, need the owner's own POS PIN (the photographer
# named OWNER_NAME), checked here on the server. Once entered, that browser stays
# unlocked for an hour. Digital photo prices and activity groups are also edited on
# the Activities page, which has no PIN, so those two saves only need the login.
OWNER_NAME = "Oren Lewkowski"
OWNER_COOKIE = "ci_owner"
OWNER_UNLOCK_SECONDS = 3600


def _owner_pin():
    owner = next((p for p in _load_photographers()
                  if (p.get("name") or "").strip().lower() == OWNER_NAME.lower()), None)
    return str((owner or {}).get("pin") or "") or None


def _owner_token(expires: int) -> str:
    sig = hmac.new((ADMIN_PASSWORD or "").encode(), f"owner|{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _owner_unlocked(request: Request) -> bool:
    if not ADMIN_PASSWORD or not _admin_authed(request):
        return False
    token = request.cookies.get(OWNER_COOKIE, "")
    try:
        expires = int(token.split(".", 1)[0])
    except ValueError:
        return False
    return expires > _time.time() and hmac.compare_digest(token, _owner_token(expires))


def _owner_required(request: Request):
    """None when the owner PIN has been entered (within the hour), else the refusal."""
    if _owner_unlocked(request):
        return None
    return JSONResponse(status_code=403, content={"error": "Enter your PIN to continue", "pin_required": True})


def _pin_photographer(request: Request, pin: str):
    """Photographer for a PIN someone typed, or None. Every PIN check goes through here so
    wrong PINs count toward the lockout (and nothing answers a PIN while locked out)."""
    pin = str(pin or "").strip()
    if not pin or _pin_locked_out(request):
        return None
    p = _auth_photographer(pin)
    if not p:
        _record_pin_failure(request)
    return p


def _pin_refusal(request: Request):
    """The answer for a PIN that _pin_photographer refused."""
    if _pin_locked_out(request):
        return JSONResponse(status_code=429, content={"error": "Too many wrong PINs — wait 10 minutes"})
    return JSONResponse(status_code=401, content={"error": "Invalid PIN"})


def _pos_pin_photographer(request: Request):
    """Photographer for the X-POS-PIN header on an approved POS device, or None (also None
    while locked out)."""
    if not _pos_device_approved(request):
        return None
    return _pin_photographer(request, request.headers.get("X-POS-PIN", ""))


def _pos_device_ok(request: Request) -> bool:
    """A signed-in POS (its cookie or PIN) or the studio: for the POS's own photo lists,
    which include partner (Eagle Mount) folders the public gallery leaves out."""
    return _nowm_allowed(request) or bool(_pos_pin_photographer(request))

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
# ── Clock in / out ──
# Clock-ins run side by side (off the main loop), so the timecard file is updated
# under a lock: two photographers clocking in at once can't overwrite each other.
_clock_lock = threading.Lock()


def _clock_in(p: dict, body: dict):
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

def _clock_out(p: dict):
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

# ── Clock record edit / delete ──
# ── Upload ──
_PORTRAIT_CATEGORY_NAMES = {"summer portraits", "winter portraits"}

def _is_portrait_location(location: str) -> bool:
    """Return True if location is a portrait-type location (hardcoded set, R2 list, or portrait-category activity)."""
    loc_lower = location.strip().lower()
    if loc_lower in PORTRAIT_LOCATIONS:
        return True
    for cat in _load_pricing().get("activity_categories", []):
        if cat.get("name", "").strip().lower() in _PORTRAIT_CATEGORY_NAMES:
            if loc_lower in [a.strip().lower() for a in (cat.get("acts") or [])]:
                return True
    return False

# ── Pending (cull feed) ──
# ── Trash ──
def _batch_delete_keys(keys: list):
    """Batch-delete R2 keys, up to 1000 per call. Ignores missing keys."""
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i+1000]
        try:
            s3.delete_objects(Bucket=R2_BUCKET,
                              Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
        except Exception as e:
            print(f"[batch_delete] error: {e}")


# ── Photographer management (admin) ──
# ─────────────────────────────────────────
# DISCOUNT CODE ADMIN API
# ─────────────────────────────────────────

# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

# ── Upload downloads ──
# ── ZIP PRICING TEST PAGES ────────────────────────────────────────────────────

ZIP_PRICING_KEY = "meta/zip_pricing.json"
FOLDER_META_KEY = "meta/folder_meta.json"

_zip_pricing_doc = JsonDoc(s3, R2_BUCKET, ZIP_PRICING_KEY, default=lambda: {"tiers": []})

def _load_zip_pricing():  return _zip_pricing_doc.load()
def _save_zip_pricing(d): _zip_pricing_doc.save(d)

_FOLDER_META_TTL = 10  # seconds
_folder_meta_doc = JsonDoc(s3, R2_BUCKET, FOLDER_META_KEY, ttl=_FOLDER_META_TTL, default=dict)
_folder_meta_cache = _folder_meta_doc.cache

def _load_folder_meta():  return _folder_meta_doc.load()
def _save_folder_meta(d): _folder_meta_doc.save(d)

def _update_folder_poses(date: str, location: str, folder: str, r2_keys: list[str]):
    """Read XMP color labels from uploaded R2 images and store pose groupings in folder_meta."""
    if not r2_keys:
        return

    def fetch_label(key: str) -> tuple[str, str]:
        try:
            obj = s3.get_object(Bucket=R2_BUCKET, Key=key, Range="bytes=0-262143")
            label = read_xmp_color_label(obj["Body"].read())
            return (key, label)
        except Exception:
            return (key, "")

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(lambda k: fetch_label(k), r2_keys))

    # Build ordered pose groups — order by first appearance of each color
    color_order: list[str] = []
    color_files: dict[str, list[str]] = {}
    for key, label in sorted(results, key=lambda x: x[0]):
        filename = os.path.basename(key)
        bucket   = label or ""
        if bucket not in color_files:
            color_order.append(bucket)
            color_files[bucket] = []
        color_files[bucket].append(filename)

    # Only store pose data if at least one photo has a color label
    labeled_colors = [c for c in color_order if c]
    if not labeled_colors:
        return

    poses = []
    pose_num = 1
    for color in color_order:
        files = color_files[color]
        if color:
            poses.append({"color": color, "label": f"Pose {pose_num}", "files": files})
            pose_num += 1
        else:                                      # photos without a label are a pose too
            poses.append({"color": "", "label": f"Pose {pose_num}", "files": files})
            pose_num += 1

    fk = _folder_key(date, location, folder)
    fm = _load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["poses"]      = poses
    fm[fk].setdefault("poses_live", True)
    _save_folder_meta(fm)

def _write_folder_poses_from_ui(date: str, location: str, folder: str, poses: list):
    """Write pose groupings from UI selection directly to folder_meta, bypassing XMP detection."""
    formatted = []
    for i, p in enumerate(poses):
        files = [os.path.basename(f) if '/' in f else f for f in p.get("files", [])]
        if files:
            formatted.append({
                "color": p.get("color", ""),
                "label": p.get("label", f"Pose {i+1}"),
                "files": files,
            })
    if not formatted:
        return
    fk = _folder_key(date, location, folder)
    fm = _load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["poses"] = formatted
    fm[fk].setdefault("poses_live", True)
    _save_folder_meta(fm)

# ── Text Presets ──
TEXT_PRESETS_KEY = "meta/text_presets.json"

_text_presets_doc = JsonDoc(s3, R2_BUCKET, TEXT_PRESETS_KEY, default=list)

def _load_text_presets():        return _text_presets_doc.load()
def _save_text_presets(presets): _text_presets_doc.save(presets)

# ── Admin Upload ──────────────────────────────────────────────────────────────

_pos_thumbs_job = {"job_id": None}


def _r2_keys_under(prefix: str) -> set:
    keys = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix):
        keys.update(o["Key"] for o in page.get("Contents", []))
    return keys


_trash_meta_lock = threading.Lock()


def _add_to_trash_meta(entries: list) -> None:
    """Add entries to the trash list (trash saves take turns, so none is lost)."""
    if entries:
        with _trash_meta_lock:
            tm = _load_trash_meta()
            tm.extend(entries)
            _save_trash_meta(tm)


def _bulk_trash(to_trash: list) -> tuple[list, int]:
    """Copy items to trash (8 at a time), then batch-delete the originals that were copied.
    Returns (trash entries, how many)."""
    if not to_trash:
        return [], 0

    now       = datetime.now(timezone.utc)
    now_iso   = now.isoformat()
    purge_iso = (now + timedelta(days=7)).isoformat()

    def copy_one(item):
        key       = to_r2_key(item["path"])
        uid       = str(uuid.uuid4())[:8]
        trash_key = f"studio_trash/{uid}_{os.path.basename(item['path'])}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": key},
                           Key=trash_key)
        except Exception as e:
            print(f"[trash] copy failed {key}: {e}")
            return None
        return {
            "key":          trash_key,
            "original_key": key,
            "filename":     os.path.basename(item["path"]),
            "date":         item["date"],
            "location":     item["location"],
            "folder":       (item.get("last_name") or item.get("group") or "").strip(),
            "trashed_at":   now_iso,
            "purge_at":     purge_iso,
            "type":         "studio",
            "image_entry":  item,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        new_meta = [m for m in ex.map(copy_one, to_trash) if m]
    orig_keys = [m["original_key"] for m in new_meta]

    if not orig_keys:
        return [], 0

    # Batch-delete originals (up to 1000 per call instead of one-by-one)
    for i in range(0, len(orig_keys), 1000):
        chunk = orig_keys[i:i+1000]
        try:
            s3.delete_objects(Bucket=R2_BUCKET,
                              Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
        except Exception as e:
            print(f"[trash] batch delete failed: {e}")

    return new_meta, len(new_meta)


import copy as _copy

_SETTINGS_DEFAULTS: dict = {
    "time_filter_locations": [],
    "site":     {"name": "Crystal Images", "tagline": "", "logo_url": ""},
    # Shown in the site footer and at the bottom of marketing emails (the law requires a mailing address there)
    "business": {"name": "Crystal Images", "address": "PO Box 162712, Big Sky, MT 59716", "phone": "(406) 404-6680", "email": "info@bigskyphotos.com"},
    "frontend": {"accent": "#F5C430", "page_bg": "#093A52", "panel_bg": "#093A53"},
    "admin":    {"accent": "#F5C430", "page_bg": "#093A52", "panel_bg": "#093A52"},
}

def _safe_color(val: str, default: str) -> str:
    v = str(val).strip()
    if re.match(r'^#[0-9a-fA-F]{3,8}$', v):        return v
    if re.match(r'^rgba?\(\s*[\d.,\s%]+\)$', v):    return v
    if re.match(r'^hsl[a]?\(\s*[\d.,\s%]+\)$', v): return v
    return default

def _load_settings() -> dict:
    cfg = _copy.deepcopy(_SETTINGS_DEFAULTS)
    try:
        obj   = s3.get_object(Bucket=R2_BUCKET, Key="settings/config.json")
        saved = json.loads(obj["Body"].read())
        for k, v in saved.items():
            if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg

def _generate_theme_css(cfg: dict, is_admin: bool) -> str:
    if is_admin:
        c      = cfg.get("admin", {})
        accent = _safe_color(c.get("accent",   "#F5C518"), "#F5C518")
        page   = _safe_color(c.get("page_bg",  "#0f1117"), "#0f1117")
        panel  = _safe_color(c.get("panel_bg", "#0a1320"), "#0a1320")
        return (
            "/* Crystal Images — Admin Theme (generated) */\n"
            f"body{{background:{page}!important}}\n"
            f"#topbar{{background:{panel}!important}}\n"
            f"#sidebar{{background:{panel}!important}}\n"
            f".nav-link.active-page{{color:{accent}!important}}\n"
            f"#topbar h1,.page-title,.stat .val{{color:{accent}!important}}\n"
            f".btn-save,.export-btn,.abtn.pri,.rpt-go{{background:{accent}!important;color:#07151f!important}}\n"
            f".tab-btn.active,.rpt-qbtn.active{{border-color:{accent}!important;color:{accent}!important}}\n"
        )
    else:
        c      = cfg.get("frontend", {})
        accent = _safe_color(c.get("accent",   "#F5C518"), "#F5C518")
        page   = _safe_color(c.get("page_bg",  "#0c2336"), "#0c2336")
        panel  = _safe_color(c.get("panel_bg", "#0a1e2e"), "#0a1e2e")
        return (
            "/* Crystal Images — Frontend Theme (generated) */\n"
            f"body{{background:{page}!important}}\n"
            f".custom-header{{background:{panel}!important}}\n"
            f".co-card{{background:{panel}!important}}\n"
            f".co-header{{background:{page}!important;filter:brightness(.85)}}\n"
        )

def _write_theme_files(cfg: dict) -> None:
    os.makedirs("static", exist_ok=True)
    with open("static/theme-frontend.css", "w") as fh:
        fh.write(_generate_theme_css(cfg, is_admin=False))
    with open("static/theme-admin.css", "w") as fh:
        fh.write(_generate_theme_css(cfg, is_admin=True))

def _save_settings(cfg: dict) -> None:
    s3.put_object(Bucket=R2_BUCKET, Key="settings/config.json",
                  Body=json.dumps(cfg).encode(), ContentType="application/json")
    _write_theme_files(cfg)

# Write theme CSS files on every server start so templates always have current styles
_write_theme_files(_load_settings())


# ── Admin Folders (phase 2) ──────────────────────────────────────────────────

_KNOWN_LOCATIONS = [
    "Adventure Zip", "Adventure Zip Line", "Nature Zip Line",
    "Mountain Biking", "Lone Peak Portraits", "Explorer Gondola", "Ramcharger Portraits",
]
_SLUG_TO_LOC = {loc.lower().replace(" ", "-"): loc for loc in _KNOWN_LOCATIONS}

# ── Admin Studio (unified page) ──────────────────────────────────────────────

# ── PRINT PRICING ─────────────────────────────────────────────────────────────

PRINT_PRICING_KEY = "meta/print_pricing.json"

_DEFAULT_PRINT_SIZES = [
    {"label": '5×7"',  "price": 25, "ship_print": 8,  "ship_framed": 15, "size_idx": 0},
    {"label": '8×10"', "price": 30, "ship_print": 8,  "ship_framed": 15, "size_idx": 1},
    {"label": '10×13"',"price": 45, "ship_print": 13, "ship_framed": 25, "size_idx": 2},
    {"label": '16×20"',"price": 90, "ship_print": 13, "ship_framed": 50, "size_idx": 3},
]

_print_pricing_doc = JsonDoc(s3, R2_BUCKET, PRINT_PRICING_KEY, ttl=30,   # read on every gallery/checkout load
                             default=lambda: {"sizes": list(_DEFAULT_PRINT_SIZES), "free_ship_threshold": 3})

def _load_print_pricing():  return _print_pricing_doc.load()
def _save_print_pricing(d): _print_pricing_doc.save(d)

# ── PORTRAIT POSE PRICING ──────────────────────────────────────────────────────
PORTRAIT_PRICING_KEY = "meta/portrait_pricing.json"
_DEFAULT_PORTRAIT_PRICING = {
    "single_photo": 40.0,
    "pose_tiers": [65.0, 57.5, 50.0],
}

_PORTRAIT_PRICING_TTL = 30
_portrait_pricing_doc = JsonDoc(s3, R2_BUCKET, PORTRAIT_PRICING_KEY, ttl=_PORTRAIT_PRICING_TTL, indent=2,
                                default=lambda: dict(_DEFAULT_PORTRAIT_PRICING))
_portrait_pricing_cache = _portrait_pricing_doc.cache

def _load_portrait_pricing():  return _portrait_pricing_doc.load()
def _save_portrait_pricing(d): _portrait_pricing_doc.save(d)

def _server_portrait_price(full_poses: int, single_photos: int) -> float:
    pp = _load_portrait_pricing()
    tiers        = pp.get("pose_tiers", _DEFAULT_PORTRAIT_PRICING["pose_tiers"])
    single_price = float(pp.get("single_photo", _DEFAULT_PORTRAIT_PRICING["single_photo"]))
    pose_price   = 0.0
    if full_poses > 0:
        pose_price = float(tiers[min(full_poses - 1, len(tiers) - 1)])
    return round(pose_price * full_poses + single_price * single_photos, 2)

# ── SEASONS ─────────────────────────────────────────────────────────────────────
# ── FOLDER POSES ────────────────────────────────────────────────────────────────
# ── PRIVATE SESSION PRICING ───────────────────────────────────────────────────
PRIVATE_SESSION_PRICING_KEY = "meta/private_session_pricing.json"

_PRIVATE_SESSION_DEFAULT = [
    {"label": "Private On Mountain Shoot", "type": "private_people", "basePrice": 550},
    {"label": "Private Portrait Session",  "type": "private_fixed",  "basePrice": 550},
    {"label": "Proposal",                  "type": "private_fixed",  "basePrice": 550},
]

_private_session_doc = JsonDoc(s3, R2_BUCKET, PRIVATE_SESSION_PRICING_KEY, indent=2,
                               default=lambda: {"sessions": list(_PRIVATE_SESSION_DEFAULT)})

def _load_private_session_pricing():  return _private_session_doc.load()
def _save_private_session_pricing(d): _private_session_doc.save(d)

# ── ITEMS BUNDLES ─────────────────────────────────────────────────────────────
ITEMS_BUNDLES_KEY = "meta/items_bundles.json"

_items_bundles_doc = JsonDoc(s3, R2_BUCKET, ITEMS_BUNDLES_KEY, ttl=30, indent=2,   # read on every checkout quote
                             default=lambda: {"bundles": []})

def _load_items_bundles():  return _items_bundles_doc.load()
def _save_items_bundles(d): _items_bundles_doc.save(d)

# ─────────────────────────────────────────
# DOWNLOAD PACKAGES (admin-created)
# ─────────────────────────────────────────

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "Crystal Images <downloads@bigskyphotos.com>")

# ── Email Templates ────────────────────────────────────────────────────────────
EMAIL_TEMPLATES_KEY = "settings/email_templates.json"

_EMAIL_TEMPLATE_DEFAULTS: dict = {
    "package_download": {
        "id": "package_download", "name": "Package Download", "system": True,
        "subject": "Your {{title}} photos are ready — Crystal Images",
        "body": (
            "Hi {{first_name}},\n\n"
            "Your photos from Crystal Images are ready. You can view and download all {{photo_count}} file(s) using the link below.\n\n"
            "{{download_button}}\n\n"
            "This link expires {{expire_date}}. If you have any trouble, reply to this email or contact us at info@bigskyphotos.com.\n\n"
            "Photos are for personal use — print, frame, and share freely."
        ),
        "tokens": ["first_name", "title", "photo_count", "expire_date", "download_button"],
    },
    "pos_receipt": {
        "id": "pos_receipt", "name": "POS Receipt", "system": True,
        "subject": "Your Crystal Images Receipt — Order #{{order_id}}",
        "body": (
            "Hi {{first_name}},\n\n"
            "Thank you for your purchase! Here is a summary of your order.\n\n"
            "{{items_table}}\n\n"
            "{{download_button}}\n\n"
            "Questions? Contact us at info@bigskyphotos.com.\n\n"
            "Photos are for personal use — print, frame, and share freely."
        ),
        "tokens": ["first_name", "order_id", "items_table", "download_button"],
    },
    "review_request": {
        "id": "review_request", "name": "Review Request", "system": False,
        "subject": "We'd love your feedback — Crystal Images",
        "body": (
            "Hi {{first_name}},\n\n"
            "Thank you for choosing Crystal Images! We loved capturing your moments.\n\n"
            "If you have a minute, leaving us a quick review would mean the world to us.\n\n"
            "{{review_button}}\n\n"
            "Thank you so much!\n— The Crystal Images Team"
        ),
        "tokens": ["first_name", "review_button"],
    },
    "discount_code": {
        "id": "discount_code", "name": "Discount Code", "system": False,
        "subject": "A special offer just for you — Crystal Images",
        "body": (
            "Hi {{first_name}},\n\n"
            "Thank you for being a Crystal Images customer! Here's a special discount just for you.\n\n"
            "{{code_box}}\n\n"
            "Use this at checkout on your next order.\n\n"
            "— The Crystal Images Team"
        ),
        "tokens": ["first_name", "code_box", "code", "discount_amount"],
    },
}

_email_templates_cache: dict = {"data": None}

def _load_email_templates() -> dict:
    """Stored templates merged over defaults. Kept in memory after the first read
    (this app is the only writer); a fresh copy is returned so callers can edit it."""
    if _email_templates_cache["data"] is None:
        try:
            obj  = s3.get_object(Bucket=R2_BUCKET, Key=EMAIL_TEMPLATES_KEY)
            _email_templates_cache["data"] = json.loads(obj["Body"].read())
        except s3.exceptions.NoSuchKey:
            _email_templates_cache["data"] = {}
    data = json.loads(json.dumps(_email_templates_cache["data"]))
    merged = {k: dict(v) for k, v in _EMAIL_TEMPLATE_DEFAULTS.items()}
    for tid, tmpl in data.items():
        if tid in merged:
            merged[tid].update({k: v for k, v in tmpl.items() if k != "system"})
        else:
            merged[tid] = tmpl
    return merged

def _save_email_templates(templates: dict):
    s3.put_object(Bucket=R2_BUCKET, Key=EMAIL_TEMPLATES_KEY,
                  Body=json.dumps(templates, indent=2).encode(), ContentType="application/json")
    _email_templates_cache["data"] = json.loads(json.dumps(templates))

def _customer_first_name(email: str) -> str:
    data = _customers_cache.get("data") or []
    c = next((x for x in data if (x.get("email") or "").lower() == email.lower()), None)
    return (c.get("first") or "").strip() if c else ""

def _render_email_body(body: str, plain_vars: dict, html_blocks: dict) -> tuple:
    """Return (html, text) from a template body with token substitution.
    Block tokens in html_blocks (buttons, code boxes) stay as tokens until their own
    paragraph is rendered; a plain_vars entry with the same name is only the
    plain-text fallback for the text version of the email."""
    def fill(text: str, escape: bool) -> str:
        # Values (names people typed, codes) go into the HTML version as plain text
        for key, val in plain_vars.items():
            if key not in html_blocks:
                v = str(val) if val is not None else ""
                text = text.replace(f"{{{{{key}}}}}", _html_escape(v) if escape else v)
        return text
    parts_html, parts_text = [], []
    for para in body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("{{") and para.endswith("}}"):
            token = para[2:-2]
            if token in html_blocks:
                parts_html.append(html_blocks[token])
                parts_text.append(plain_vars.get(token, ""))
                continue
        inner = fill(para, True).replace("\n", "<br>")
        parts_html.append(f'<p style="margin:0 0 .9rem;font-size:.9rem;color:#333;line-height:1.65">{inner}</p>')
        parts_text.append(fill(para, False))
    return "".join(parts_html), "\n\n".join(parts_text)

def _discount_detail(code_obj: dict) -> str:
    """e.g. '20% off — expires 2026-12-31'."""
    amt = float(code_obj.get("amount", 0) or 0)
    if code_obj.get("type", "percent") == "percent":
        label = f"{amt:g}% off"                                    # 20% off, 12.5% off
    else:
        label = f"${int(amt)} off" if amt.is_integer() else f"${amt:.2f} off"   # $10 off, $7.50 off
    expires = code_obj.get("expires") or ""
    return label + (f" — expires {expires}" if expires else "")

def _code_box_html(code: str, detail: str = "") -> str:
    """Discount code box with a Shop Now button to the photo site."""
    return (
        f'<div style="background:#f5f5f5;border-radius:8px;padding:1.25rem;text-align:center;margin:0 0 1rem">'
        f'<div style="font-size:.78rem;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:.3rem">Your discount code</div>'
        f'<div style="font-size:2rem;font-weight:700;letter-spacing:.12em;color:#111">{code}</div>'
        + (f'<div style="font-size:.9rem;color:#555;margin-top:.35rem">{detail}</div>' if detail else '') +
        f'<div style="margin-top:1rem"><a href="{SITE_URL}" '
        f'style="background:#1a73e8;color:#fff;padding:.65rem 1.4rem;border-radius:6px;'
        f'font-size:.9rem;font-weight:600;text-decoration:none;display:inline-block">Shop Now</a></div>'
        f'</div>'
    )

def _review_button_html(review_url: str) -> str:
    return (
        f'<p style="margin:0 0 1rem"><a href="{review_url}" '
        f'style="background:#1a73e8;color:#fff;padding:.65rem 1.4rem;border-radius:6px;'
        f'font-size:.9rem;font-weight:600;text-decoration:none;display:inline-block">'
        f'Leave a Review</a></p>'
    )

def _wrap_email_html(body_html: str, marketing_to: str = "", marketing_why: str = "") -> str:
    """The email shell. marketing_to = the recipient of a marketing email (follow-ups,
    offers): adds the business address and an unsubscribe link, as the law requires.
    marketing_why: why they get it, when it isn't that they bought photos."""
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f9f9f9;font-family:Arial,sans-serif">'
        '<div style="max-width:560px;margin:0 auto;padding:2rem 1rem">'
        '<div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:2rem">'
        '<p style="margin:0 0 1.25rem;font-size:.75rem;color:#999;text-transform:uppercase;letter-spacing:.5px">Crystal Images</p>'
        + body_html +
        '<hr style="border:none;border-top:1px solid #eee;margin:1.5rem 0 1rem">'
        '<p style="margin:0;font-size:.72rem;color:#aaa">Questions? <a href="mailto:info@bigskyphotos.com" style="color:#1a73e8">info@bigskyphotos.com</a></p>'
        + (_marketing_footer_html(marketing_to, marketing_why) if marketing_to else '') +
        '</div></div></body></html>'
    )


# ── Unlimited Action Photo Packages (POS only) ───────────────────────────────
# A customer pays once for a set number of days; staff add their action photos to one download
# link as they come into the store. Names, days and prices are edited in Admin > Pricing.
UNLIMITED_PACKAGES_KEY = "meta/unlimited_packages.json"
_UNLIMITED_DEFAULTS = {
    "tiers": [
        {"id": "unl-3", "name": "3-Day Unlimited Action Photos", "days": 3, "price": 205},
        {"id": "unl-5", "name": "5-Day Unlimited Action Photos", "days": 5, "price": 275},
        {"id": "unl-7", "name": "7-Day Unlimited Action Photos", "days": 7, "price": 345},
    ],
    "included_people": 4,         # people covered by the package price
    "extra_person_price": 30,     # each person beyond that
    "link_days_after": 30,        # the download link keeps working this many days after the last day
    "print_discount_pct": 60,     # in person: prints of photos in a package are this much off
}
_unlimited_doc = JsonDoc(s3, R2_BUCKET, UNLIMITED_PACKAGES_KEY, ttl=30, indent=2,
                         default=lambda: _copy.deepcopy(_UNLIMITED_DEFAULTS),
                         valid=lambda d: isinstance(d, dict) and isinstance(d.get("tiers"), list))


def _load_unlimited_packages() -> dict:
    d = _unlimited_doc.load()
    # Settings saved before a setting existed get its default
    return {**{k: v for k, v in _UNLIMITED_DEFAULTS.items() if k != "tiers"}, **d}


def _clean_unlimited_packages(body: dict) -> dict:
    """Checked settings from the Pricing page; raises ValueError with a message for the page."""
    def num(v, what, lo, hi, integer=False):
        try:
            n = int(v) if integer else round(float(v), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{what} must be a number")
        if not lo <= n <= hi:
            raise ValueError(f"{what} must be between {lo} and {hi}")
        return n
    tiers, ids = [], set()
    for i, t in enumerate(body.get("tiers") or []):
        name = str(t.get("name") or "").strip()[:80]
        if not name:
            raise ValueError(f"Package {i + 1} needs a name")
        tid = str(t.get("id") or "").strip() or f"unl-{uuid.uuid4().hex[:6]}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", tid) or tid in ids:
            tid = f"unl-{uuid.uuid4().hex[:6]}"
        ids.add(tid)
        tiers.append({"id": tid, "name": name, "days": num(t.get("days"), f"Days for {name}", 1, 30, True),
                      "price": num(t.get("price"), f"Price for {name}", 0, 10000)})
    if not tiers:
        raise ValueError("Add at least one package")
    return {"tiers": tiers,
            "included_people": num(body.get("included_people"), "People included", 1, 50, True),
            "extra_person_price": num(body.get("extra_person_price"), "Price per extra person", 0, 1000),
            "link_days_after": num(body.get("link_days_after"), "Download link days", 1, 365, True),
            "print_discount_pct": num(body.get("print_discount_pct", _UNLIMITED_DEFAULTS["print_discount_pct"]),
                                      "Print discount", 0, 100, True)}


def _save_unlimited_packages(d: dict) -> None:
    _unlimited_doc.save(d)


# A sold package: the customer's download page (packages/<token>/meta.json, whose photo list
# grows during the trip) plus a small record per package (unlimited_sales/<token>.json) that
# the POS lists and searches. Days run in Big Sky time: a 3-day package bought today covers
# today and the next two days; the link keeps working link_days_after days past the last day.
UNLIMITED_SALES_PREFIX = "unlimited_sales/"
_MOUNTAIN = ZoneInfo("America/Denver")


def _unlimited_cart_items(items: list) -> list:
    return [i for i in (items or []) if isinstance(i, dict) and i.get("category") == "unlimited_package"]


def _unlimited_price(settings: dict, tier: dict, people: int) -> float:
    extra = max(0, people - int(settings.get("included_people") or 0))
    return round(float(tier["price"]) + extra * float(settings.get("extra_person_price") or 0), 2)


def _check_unlimited_cart(items: list, strict: bool = True):
    """The package in a POS cart, checked. Returns (package or None, error or "").
    strict (before charging): the package and price must match the current Pricing settings.
    Not strict (after Square has taken payment): a package renamed or repriced in the
    meantime still goes through, using the days and name the POS showed."""
    pkgs = _unlimited_cart_items(items)
    if not pkgs:
        return None, ""
    if len(pkgs) > 1:
        return None, "Only one Unlimited package per sale"
    if any(i.get("category") == "action_photos" for i in items):
        return None, "Sell the Unlimited package on its own — add photos to it after the sale"
    item = pkgs[0]
    try:
        people = int(item.get("package_people"))
    except (TypeError, ValueError):
        people = 0
    if not 1 <= people <= 50:
        return None, "Number of people for the package is missing"
    settings = _load_unlimited_packages()
    tier = next((t for t in settings["tiers"] if t.get("id") == item.get("package_tier_id")), None)
    custom = bool(item.get("package_custom_price"))
    try:
        price = round(float(item.get("price") or 0), 2)
    except (TypeError, ValueError):
        price = -1
    if not 0 <= price <= 10000 or int(item.get("qty") or 1) != 1:
        return None, "The package price isn't valid"
    if strict:
        if not tier:
            return None, "That package is no longer offered — refresh the POS"
        # Staff may set a custom price (typed in on the POS); otherwise it must match Pricing
        if not custom and abs(price - _unlimited_price(settings, tier, people)) > 0.005:
            return None, "Package prices changed — remove the package and add it again"
    if not tier:
        try:
            days = int(item.get("package_days"))
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= 30:
            return None, "That package is no longer offered"
        tier = {"id": str(item.get("package_tier_id") or ""), "name": str(item.get("label") or "Unlimited Package"),
                "days": days}
    return {"tier_id": tier["id"], "tier_name": tier["name"], "days": int(tier["days"]), "people": people,
            "price": price, "custom_price": custom,
            "link_days_after": int(settings.get("link_days_after") or 30)}, ""


def _unlimited_dates(days: int, link_days_after: int, now: datetime | None = None) -> dict:
    """Start day, last day to add photos, and when the link stops working (end of that day)."""
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(_MOUNTAIN).date()
    last_day = start + timedelta(days=days - 1)
    end_of_link = datetime.combine(last_day + timedelta(days=link_days_after), datetime.max.time(),
                                   tzinfo=_MOUNTAIN).replace(microsecond=0)
    return {"start": start.isoformat(), "last_day": last_day.isoformat(),
            "expires": end_of_link.astimezone(timezone.utc).isoformat()}


# Package records are few (hundreds a season), so the POS list and the commission page work
# from all of them in memory, re-read from storage at most once a minute; every save here
# updates the memory too. Changes to one package (adding photos, closing) take its lock.
UNLIMITED_RECORDS_SECONDS = 60
_unlimited_records_cache = {"records": None, "ts": 0.0}
_unlimited_records_guard = threading.Lock()
_unlimited_locks = _KeyedLocks()


def _store_unlimited_sale(record: dict) -> None:
    s3.put_object(Bucket=R2_BUCKET, Key=f"{UNLIMITED_SALES_PREFIX}{record['token']}.json",
                  Body=json.dumps(record).encode(), ContentType="application/json")
    with _unlimited_records_guard:
        if _unlimited_records_cache["records"] is not None:
            _unlimited_records_cache["records"][record["token"]] = record


def _load_unlimited_sale(token: str):
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", token or ""):
        return None
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"{UNLIMITED_SALES_PREFIX}{token}.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _unlimited_records() -> dict:
    """Every package ever sold, token -> record."""
    with _unlimited_records_guard:
        c = _unlimited_records_cache
        if c["records"] is not None and _time.monotonic() - c["ts"] < UNLIMITED_RECORDS_SECONDS:
            return dict(c["records"])
    tokens = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=UNLIMITED_SALES_PREFIX):
        tokens += [o["Key"][len(UNLIMITED_SALES_PREFIX):-5] for o in page.get("Contents", [])
                   if o["Key"].endswith(".json")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        records = {r["token"]: r for r in ex.map(_load_unlimited_sale, tokens) if r}
    with _unlimited_records_guard:
        _unlimited_records_cache.update(records=records, ts=_time.monotonic())
    return dict(records)


def _mountain_today() -> str:
    return datetime.now(_MOUNTAIN).date().isoformat()


def _unlimited_adding_until(record: dict) -> str:
    """Last day staff can add photos: the package's last day (the package closes after it)."""
    return record["last_day"]


def _unlimited_is_open(record: dict, today: str | None = None) -> bool:
    return record.get("status") != "closed" and (today or _mountain_today()) <= _unlimited_adding_until(record)


def _photo_prefix(path: str) -> str:
    """The photographer's JPEG code: the first three letters of the file name (dsc, ams, jrm...)."""
    return os.path.basename(path or "")[:3].lower()


def _close_due_unlimited() -> None:
    """Close every package whose adding days are over: freeze how many of its photos each
    photographer took, which splits the sale for commission. Safe to call often."""
    today = _mountain_today()
    for rec in _unlimited_records().values():
        if rec.get("status") == "closed" or _unlimited_is_open(rec, today):
            continue
        with _unlimited_locks.hold(rec["token"]):
            current = _load_unlimited_sale(rec["token"]) or rec
            if current.get("status") == "closed":
                continue
            pkg = _load_pkg(rec["token"]) or {}
            split: dict = {}
            for p in pkg.get("paths") or []:
                split[_photo_prefix(p)] = split.get(_photo_prefix(p), 0) + 1
            _store_unlimited_sale({**current, "status": "closed", "split": split,
                                   "photo_count": len(pkg.get("paths") or []),
                                   "closed_at": datetime.now(timezone.utc).isoformat()})


def _unlimited_shares(record: dict | None, photographers: list) -> list | None:
    """A closed package's sale split by photographer: [(name, photographer id, fraction)].
    Photos with an unknown code, or a package closed with no photos, count for the owner.
    None while the package is still open."""
    if not record or record.get("status") != "closed":
        return None
    by_prefix = {(p.get("file_prefix") or "").strip().lower(): p for p in photographers
                 if (p.get("file_prefix") or "").strip()}
    owner = next((p for p in photographers if (p.get("name") or "").strip().lower() == OWNER_NAME.lower()),
                 {"id": "", "name": OWNER_NAME})
    split = record.get("split") or {}
    total = sum(split.values())
    if not total:
        return [(owner["name"], owner.get("id", ""), 1.0)]
    merged: dict = {}
    for prefix, n in split.items():
        p = by_prefix.get(prefix, owner)
        key = p.get("id") or p["name"]
        name, pid, frac = merged.get(key, (p["name"], p.get("id", ""), 0.0))
        merged[key] = (name, pid, frac + n / total)
    return sorted(merged.values(), key=lambda t: (-t[2], t[0].lower()))


def _unlimited_photog_label(order_row: dict, photographers: list, records: dict) -> str | None:
    """What the Orders page and CSV show as the photographer of a package sale (None if not one)."""
    if not order_row.get("unlimited"):
        return None
    shares = _unlimited_shares(records.get(order_row.get("package_token") or ""), photographers)
    if shares is None:
        return "Unlimited package (split when it closes)"
    return " & ".join(f"{name} {round(frac * 100)}%" for name, _, frac in shares)


def _unlimited_summary(rec: dict, today: str | None = None) -> dict:
    """What the POS shows for a package in its list."""
    today = today or _mountain_today()
    start = datetime.fromisoformat(rec["start"]).date()
    day_num = (datetime.fromisoformat(today).date() - start).days + 1
    return {k: rec.get(k) for k in ("token", "name", "phone", "email", "tier_name", "people", "days",
                                    "start", "last_day")} | {
        "photo_count": int(rec.get("photo_count") or 0),
        "open": _unlimited_is_open(rec, today),
        "adding_until": _unlimited_adding_until(rec),
        "day_num": day_num,
    }


def _unlimited_photo_problem(rec: dict, item: dict | None) -> str:
    """Why a photo can't go in this package ('' if it can)."""
    if not item:
        return "isn't a published photo"
    if _is_portrait_location(item.get("location", "")):
        return "is a portrait (packages are action photos only)"
    if item.get("date", "") > rec["last_day"]:
        return "was taken after the package's last day"
    return ""


def _unlimited_change_photos(token: str, add: list, remove: list) -> dict:
    """Add and/or remove photos in an open package. Raises ValueError with a message for staff."""
    with _unlimited_locks.hold(token):
        rec = _load_unlimited_sale(token)
        if not rec:
            raise LookupError("Package not found")
        if not _unlimited_is_open(rec):
            raise ValueError("This package is closed — its days are over")
        pkg = _load_pkg(token)
        if not pkg:
            raise LookupError("Package not found")
        live = _folder_index()["all_by_path"]      # POS: partner photos count too
        paths = list(pkg.get("paths") or [])
        have = set(paths)
        new = []
        for p in dict.fromkeys(x for x in (add or []) if isinstance(x, str) and x):
            if p in have:
                continue                                 # already in the package
            problem = _unlimited_photo_problem(rec, live.get(p))
            if problem:
                raise ValueError(f"{os.path.basename(p)} {problem}")
            new.append(p)
        gone = {x for x in (remove or []) if isinstance(x, str)}
        paths = sorted([p for p in paths if p not in gone] + new)   # by day, then folder and file
        _store_pkg(token, {**pkg, "paths": paths})
        _store_unlimited_sale({**rec, "photo_count": len(paths)})
        return {"added": len(new), "removed": len(have & gone), "paths": paths}


def _remap_unlimited_paths(remap: dict) -> None:
    """Photos moved or renamed in the studio: keep every package that has them pointing at them."""
    if not remap:
        return
    now = datetime.now(timezone.utc)
    for rec in _unlimited_records().values():
        try:
            if datetime.fromisoformat(rec["expires"]) < now:
                continue
        except Exception:
            continue
        with _unlimited_locks.hold(rec["token"]):
            pkg = _load_pkg(rec["token"])
            if not pkg or not any(p in remap for p in pkg.get("paths") or []):
                continue
            _store_pkg(rec["token"], {**pkg, "paths": sorted(remap.get(p, p) for p in pkg["paths"])})


def _remap_unlimited_paths_soon(remap: dict) -> None:
    if remap:
        threading.Thread(target=_remap_unlimited_paths, args=(dict(remap),), daemon=True).start()


def _mountain_date_str(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(_MOUNTAIN).strftime("%B %d, %Y")


def _unlimited_note(last_day: str, expire_str: str) -> str:
    """One line for the receipt and download page about how the package link works."""
    try:
        last = datetime.fromisoformat(last_day).strftime("%A, %B %d").replace(" 0", " ")
    except Exception:
        last = last_day
    return (f"We'll add your action photos to this page during your trip, through {last}. "
            f"Check back anytime. Your link works until {expire_str}.")


# ── Saved carts ───────────────────────────────────────────────────────────────
# A customer can save their gallery selection (the same cart the Select button builds) with
# their first name and email, then load it on any device by typing that email. One file per
# email (saved_carts/<hash>.json); the studio lists them under Customers > Saved Carts. A paid
# online order empties that email's saved cart; the studio clears them all at season's end.
SAVED_CARTS_PREFIX = "saved_carts/"
SAVED_CART_MAX_BYTES = 400_000
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
_saved_cart_save_limiter = _RateLimiter(max_calls=60, window=600)    # per visitor
_saved_cart_load_limiter = _RateLimiter(max_calls=10, window=600)    # per visitor: no guessing emails
_saved_carts_cache = {"records": None, "ts": 0.0}
_saved_carts_guard = threading.Lock()


def _saved_cart_id(email: str) -> str:
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()[:32]


def _saved_cart_key(cart_id: str) -> str:
    return f"{SAVED_CARTS_PREFIX}{cart_id}.json"


def _cart_summary(cart: dict) -> dict:
    """What's in a gallery cart: photo paths (selected photos, poses, prints) and counts."""
    paths, photos, poses, prints = [], 0, 0, 0
    folders = cart.get("folderCarts") if isinstance(cart.get("folderCarts"), dict) else {}
    labels = []
    for fc in folders.values():
        if not isinstance(fc, dict):
            continue
        sel = [p for p in (fc.get("selectedPaths") or []) if isinstance(p, str)]
        photos += len(sel)
        paths += sel
        pose_list = fc.get("poses") if isinstance(fc.get("poses"), list) else []
        for color, state in (fc.get("poseState") or {}).items():
            pose = next((p for p in pose_list if isinstance(p, dict) and p.get("color") == color), {})
            pose_paths = [p for p in (pose.get("paths") or []) if isinstance(p, str)]
            if state == "pose":
                poses += 1
                paths += pose_paths
            elif isinstance(state, list):
                picked = [pose_paths[i] for i in state if isinstance(i, int) and 0 <= i < len(pose_paths)]
                photos += len(picked)
                paths += picked
        fprints = [p for p in (fc.get("printCart") or []) if isinstance(p, dict)]
        prints += len(fprints)
        paths += [p.get("photoPath") for p in fprints if isinstance(p.get("photoPath"), str)]
        if fc.get("label"):
            labels.append(str(fc["label"])[:80])
    frames = sum(int(f.get("quantity") or 1) for f in (cart.get("frameCart") or []) if isinstance(f, dict))
    return {"photos": photos, "poses": poses, "prints": prints, "frames": frames,
            "items": photos + poses + prints + frames, "folders": labels,
            "paths": list(dict.fromkeys(p for p in paths if p.startswith("images/")))[:400]}


def _load_saved_cart(cart_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", cart_id or ""):
        return None
    try:
        return json.loads(s3.get_object(Bucket=R2_BUCKET, Key=_saved_cart_key(cart_id))["Body"].read())
    except Exception:
        return None


def _remember_saved_cart(cart_id: str, record: dict | None) -> None:
    with _saved_carts_guard:
        if _saved_carts_cache["records"] is not None:
            if record is None:
                _saved_carts_cache["records"].pop(cart_id, None)
            else:
                _saved_carts_cache["records"][cart_id] = record


def _save_cart_record(email: str, first_name: str, cart: dict) -> dict:
    """Save (or, for an empty cart, remove) this email's cart. Returns the summary."""
    cart_id = _saved_cart_id(email)
    summary = _cart_summary(cart)
    if not summary["items"]:
        _delete_saved_cart(cart_id)
        return summary
    old = _load_saved_cart(cart_id) or {}
    now = datetime.now(timezone.utc).isoformat()
    record = {"id": cart_id, "email": email.strip().lower(), "first_name": first_name.strip()[:40],
              "cart": cart, "summary": summary, "created": old.get("created") or now, "updated": now,
              "reminders": old.get("reminders") or []}
    s3.put_object(Bucket=R2_BUCKET, Key=_saved_cart_key(cart_id), Body=json.dumps(record).encode(),
                  ContentType="application/json")
    _remember_saved_cart(cart_id, {k: v for k, v in record.items() if k != "cart"})
    return summary


def _delete_saved_cart(cart_id: str) -> None:
    try:
        s3.delete_object(Bucket=R2_BUCKET, Key=_saved_cart_key(cart_id))
    except Exception as e:
        print(f"Saved cart not removed ({cart_id}): {e}")
    _remember_saved_cart(cart_id, None)


def _clear_saved_cart_for(email: str) -> None:
    """An order was paid: that customer's saved cart is done."""
    if email and _EMAIL_RE.match(email.strip()):
        cart_id = _saved_cart_id(email)
        if _load_saved_cart(cart_id):
            _delete_saved_cart(cart_id)


def _saved_cart_order_body(rec: dict) -> dict:
    """A saved gallery cart as the online checkout's order request (what checkout.html sends)."""
    cart = rec.get("cart") or {}
    albums, poses, singles, prints = [], [], [], []
    first_loc = first_date = first_family = ""
    for key, fc in (cart.get("folderCarts") or {}).items():
        if not isinstance(fc, dict):
            continue
        parts = str(key).split("||")
        date = parts[0] if parts else ""
        location = fc.get("location") or (parts[1] if len(parts) > 1 else "")
        family = parts[2] if len(parts) > 2 else ""
        first_loc, first_date, first_family = first_loc or location, first_date or date, first_family or family
        sel = [p for p in (fc.get("selectedPaths") or []) if isinstance(p, str)]
        if sel:
            photos = fc.get("selectedPhotos") or []
            albums.append({"last_name": (photos[0].get("last_name") if photos and isinstance(photos[0], dict) else "") or location or "Photos",
                           "location": location, "date": date, "count": len(sel),
                           "filenames": [os.path.basename(p) for p in sel], "paths": sel})
        pose_list = fc.get("poses") if isinstance(fc.get("poses"), list) else []
        for color, state in (fc.get("poseState") or {}).items():
            pose = next((p for p in pose_list if isinstance(p, dict) and p.get("color") == color), {})
            pp = [p for p in (pose.get("paths") or []) if isinstance(p, str)]
            if state == "pose":
                poses.append({"pose_label": pose.get("label", ""), "color": color, "files": pose.get("files") or [],
                              "paths": pp, "family": family, "date": date, "location": location})
            elif isinstance(state, list):
                singles += [{"path": pp[i], "pose_label": pose.get("label", ""), "color": color, "family": family}
                            for i in state if isinstance(i, int) and 0 <= i < len(pp)]
        for p in fc.get("printCart") or []:
            if isinstance(p, dict):
                prints.append({"filename": p.get("filename", ""), "size": p.get("size", ""), "size_idx": p.get("sizeIdx", 0),
                               "orientation": p.get("orientation") or "landscape", "frame": p.get("frame", ""),
                               "path": p.get("photoPath", ""), "location": location, "logo": bool(p.get("_logo"))})
    return {"digital_albums": albums, "portrait_poses": poses, "portrait_singles": singles, "prints": prints,
            "frames": [{"variation_id": f.get("variation_id"), "quantity": f.get("quantity") or 1}
                       for f in (cart.get("frameCart") or []) if isinstance(f, dict)],
            "digital_logo_paths": [p for p in (cart.get("digitalLogoPaths") or []) if isinstance(p, str)],
            "location": first_loc, "date": first_date, "last_name": first_family}


def _send_invoice_email(to_email: str, first_name: str, pay_url: str, total: float) -> bool:
    if not RESEND_API_KEY:
        return False
    body = (f'<p style="margin:0 0 1rem">Hi {_html_escape(first_name or "there")},</p>'
            f'<p style="margin:0 0 1rem">Here is your order from Crystal Images, ready to pay. Total: <strong>${total:.2f}</strong> '
            f'(any tax is added at payment).</p>'
            f'<p style="margin:0 0 1.2rem"><a href="{_html_escape(pay_url)}" style="background:#1a73e8;color:#fff;padding:.6rem 1.3rem;'
            f'border-radius:6px;font-weight:600;text-decoration:none;display:inline-block">Pay now</a></p>'
            f'<p style="margin:0;font-size:.85rem;color:#666">Once it is paid, your photos are emailed to you right away. '
            f'Questions? Just reply to this email.</p>')
    try:
        r = http_requests.post("https://api.resend.com/emails",
                               headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                               json={"from": RESEND_FROM, "to": [to_email], "subject": "Your photo order from Crystal Images",
                                     "html": _wrap_email_html(body)}, timeout=10)
        return r.ok
    except Exception as e:
        print(f"Invoice email failed ({to_email}): {e}")
        return False


# Invoices sent from saved carts: one small record each (invoices/<WooCommerce order id>.json);
# whether it's been paid is read from WooCommerce when the list is opened.
INVOICES_PREFIX = "invoices/"
_INVOICE_STATUS = {"pending": "Waiting for payment", "processing": "Paid", "completed": "Paid",
                   "cancelled": "Cancelled", "failed": "Payment failed", "on-hold": "On hold",
                   "refunded": "Refunded"}


def _save_invoice(rec: dict) -> None:
    s3.put_object(Bucket=R2_BUCKET, Key=f"{INVOICES_PREFIX}{rec['order_id']}.json",
                  Body=json.dumps(rec).encode(), ContentType="application/json")


def _load_invoices() -> list:
    """Every invoice sent, newest first, with its current status from WooCommerce."""
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=INVOICES_PREFIX):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json")]

    def read(k):
        try:
            return json.loads(s3.get_object(Bucket=R2_BUCKET, Key=k)["Body"].read())
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        recs = [r for r in ex.map(read, keys) if isinstance(r, dict)]
    status = {}
    ids = [str(r["order_id"]) for r in recs]
    for i in range(0, len(ids), 100):          # WooCommerce answers up to 100 orders per request
        try:
            resp = http_requests.get(f"{WC_BASE}/orders", params={"include": ",".join(ids[i:i + 100]), "per_page": 100},
                                     auth=(WC_KEY, WC_SECRET), timeout=15)
            if resp.ok:
                status.update({str(o["id"]): o.get("status", "") for o in resp.json()})
        except Exception as e:
            print(f"Invoice statuses not read: {e}")
    for r in recs:
        st = status.get(str(r["order_id"]))
        r["wc_status"] = st or ""
        r["status"] = _INVOICE_STATUS.get(st, st.capitalize() if st else "Unknown (shop not reachable)")
    return sorted(recs, key=lambda r: r.get("created", ""), reverse=True)


def _cancel_invoice(order_id: str):
    """Cancel an unpaid invoice's WooCommerce order so its Pay now link stops working.
    Returns an error message, or '' when cancelled."""
    try:
        resp = http_requests.get(f"{WC_BASE}/orders/{order_id}", auth=(WC_KEY, WC_SECRET), timeout=15)
        if not resp.ok:
            return "The shop couldn't find this order"
        st = resp.json().get("status", "")
        if st in ("processing", "completed", "refunded"):
            return "This invoice is already paid, so it can't be cancelled (refund it in the shop instead)"
        if st == "cancelled":
            return ""
        resp = http_requests.put(f"{WC_BASE}/orders/{order_id}", json={"status": "cancelled"},
                                 auth=(WC_KEY, WC_SECRET), timeout=15)
        return "" if resp.ok else f"The shop refused: {resp.text[:200]}"
    except Exception as e:
        return f"The shop couldn't be reached: {e}"


def _saved_cart_link_token(cart_id: str) -> str:
    """For the link in a reminder email: opens that saved cart without typing the email (the
    link only ever goes to the customer's own inbox)."""
    sig = hmac.new((os.getenv("UNSUBSCRIBE_SECRET") or ADMIN_PASSWORD or "").encode(),
                   f"savedcart|{cart_id}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{cart_id}.{sig}"


def _saved_cart_from_link(token: str) -> str | None:
    cart_id, _, sig = (token or "").partition(".")
    if re.fullmatch(r"[0-9a-f]{32}", cart_id) and hmac.compare_digest(_saved_cart_link_token(cart_id), token or ""):
        return cart_id
    return None


def _send_saved_cart_reminder(rec: dict) -> str:
    """Email the customer about their saved photos (a marketing email: business address,
    unsubscribe link and headers; never to someone who unsubscribed). Returns '' or a problem."""
    email = rec.get("email", "")
    if _opted_out(email):
        return "This customer unsubscribed from marketing emails, so no reminder was sent"
    if not RESEND_API_KEY:
        return "Email isn't set up on this server"
    summary = rec.get("summary") or {}
    link = f"{SITE_URL}/?saved={_saved_cart_link_token(rec['id'])}"
    n = summary.get("items", 0)                 # no photos in the email: they're seen on the site
    body = (f'<p style="margin:0 0 1rem">Hi {_html_escape(rec.get("first_name") or "there")},</p>'
            f'<p style="margin:0 0 1rem">Your saved photos are still waiting for you ({n} item{"s" if n != 1 else ""}).</p>'
            f'<p style="margin:0 0 1.2rem"><a href="{_html_escape(link)}" style="background:#1a73e8;color:#fff;'
            f'padding:.6rem 1.3rem;border-radius:6px;font-weight:600;text-decoration:none;display:inline-block">View your photos</a></p>'
            f'<p style="margin:0;font-size:.85rem;color:#666">Questions? Just reply to this email.</p>')
    try:
        r = http_requests.post("https://api.resend.com/emails",
                               headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                               json={"from": RESEND_FROM, "to": [email], "subject": "Your saved photos are waiting",
                                     "html": _wrap_email_html(body, marketing_to=email,
                                                              marketing_why="you saved photos on photos.bigskyphotos.com"),
                                     "headers": _list_unsubscribe_headers(email)}, timeout=10)
        if not r.ok:
            return "The email service refused the reminder"
    except Exception as e:
        print(f"Reminder failed ({email}): {e}")
        return "Couldn't reach the email service"
    rec["reminders"] = (rec.get("reminders") or []) + [datetime.now(timezone.utc).isoformat()]
    s3.put_object(Bucket=R2_BUCKET, Key=_saved_cart_key(rec["id"]), Body=json.dumps(rec).encode(),
                  ContentType="application/json")
    _remember_saved_cart(rec["id"], {k: v for k, v in rec.items() if k != "cart"})
    return ""


def _saved_cart_list() -> list:
    """Every saved cart without its full contents (for the studio list), newest first."""
    with _saved_carts_guard:
        c = _saved_carts_cache
        if c["records"] is not None and _time.monotonic() - c["ts"] < 60:
            return sorted(c["records"].values(), key=lambda r: r.get("updated", ""), reverse=True)
    ids = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=SAVED_CARTS_PREFIX):
        ids += [o["Key"][len(SAVED_CARTS_PREFIX):-5] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = [r for r in ex.map(_load_saved_cart, ids) if r]
    records = {r["id"]: {k: v for k, v in r.items() if k != "cart"} for r in found}
    with _saved_carts_guard:
        _saved_carts_cache.update(records=records, ts=_time.monotonic())
    return sorted(records.values(), key=lambda r: r.get("updated", ""), reverse=True)


# ── Partner programs (Eagle Mount) ────────────────────────────────────────────
# A partner's participants get their photos free on their own page. Photographers mark those
# photos while culling with the partner's color label (written into the photo file); winter
# action photos uploaded with that label are flagged item["partner"] = <partner id>.
PARTNERS_KEY = "meta/partners.json"
_PARTNER_FIELDS = {                 # every partner has these (older saved partners get the defaults)
    "slug": "",                     # page address: /<slug> (e.g. /eaglemount)
    "password": "",                 # shared password; the page stays closed while it's empty
    "welcome": "",                  # line at the top of the page
    "value_per_photo": 25,          # in-kind donation value of each photo downloaded
    "print_discount_pct": 40,       # prints of their photos
    "family_code": "",              # discount code shown on the page for families
    "family_code_pct": 25,
    "family_code_message": "",      # the box's wording; blank = the standard wording below
}
FAMILY_CODE_MESSAGE_DEFAULT = ("Family and friends: use code {code} for {pct}% off digital photos, "
                               "online in {gallery} and in our store.")
_PARTNER_DEFAULTS = [{"id": "eagle-mount", "name": "Eagle Mount", "label": "Orange", "slug": "eaglemount",
                      "welcome": "Welcome! Your photos are free to download. Thank you for skiing with Eagle Mount."}]
_partners_doc = JsonDoc(s3, R2_BUCKET, PARTNERS_KEY, ttl=30, indent=2,
                        default=lambda: _copy.deepcopy(_PARTNER_DEFAULTS),
                        valid=lambda d: isinstance(d, list))
PARTNER_SESSION_DAYS = 180          # a device that entered the password stays in for the season


def _load_partners() -> list:
    return [{**_PARTNER_FIELDS, **p} for p in _partners_doc.load() if isinstance(p, dict)]


def _save_partners(partners: list) -> None:
    _partners_doc.save(partners)


def _family_code_message(partner: dict) -> str:
    """The family code box text ('' when there's no code). {code} and {pct} are filled in;
    {gallery} is left for the page, which shows it as a link to the gallery."""
    code = (partner.get("family_code") or "").strip()
    if not code:
        return ""
    msg = (partner.get("family_code_message") or "").strip() or FAMILY_CODE_MESSAGE_DEFAULT
    return msg.replace("{code}", code).replace("{pct}", f"{partner.get('family_code_pct', 0):g}")


def _clean_partners(body: list) -> list:
    """Checked partner settings from the Settings page; raises ValueError with a message for the page."""
    if not isinstance(body, list) or not body:
        raise ValueError("Nothing to save")
    current = {p["id"]: p for p in _load_partners()}

    def num(v, what, lo, hi):
        try:
            n = round(float(v), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{what} must be a number")
        if not lo <= n <= hi:
            raise ValueError(f"{what} must be between {lo} and {hi}")
        return int(n) if n == int(n) else n
    out = []
    for b in body:
        pid = str(b.get("id") or "")
        if pid not in current:
            raise ValueError("Unknown partner")
        name = str(b.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("The partner needs a name")
        label = str(b.get("label") or "").strip()[:30]
        if not label:
            raise ValueError(f"{name}: the color label can't be empty")
        password = str(b.get("password") or "").strip()
        if password and len(password) < 6:
            raise ValueError(f"{name}: the password needs at least 6 characters (or leave it empty to keep the page closed)")
        code = re.sub(r"\s+", "", str(b.get("family_code") or "")).upper()[:30]
        out.append({**current[pid], "name": name, "label": label, "password": password[:100],
                    "welcome": str(b.get("welcome") or "").strip()[:500],
                    "family_code": code,
                    "family_code_pct": num(b.get("family_code_pct", 25), f"{name}: family code %", 0, 100),
                    "family_code_message": str(b.get("family_code_message") or "").strip()[:500],
                    "print_discount_pct": num(b.get("print_discount_pct", 40), f"{name}: print discount %", 0, 100),
                    "value_per_photo": num(b.get("value_per_photo", 25), f"{name}: value per photo", 0, 1000)})
    return out


def _sync_family_code(partner: dict, old_code: str = "") -> str:
    """Keep the partner's family code working everywhere: a percent-off code in Discounts
    (online checkout and the POS read the same codes). Creates it, updates its percent, and
    turns off the partner's previous code when the code is changed. A code with the same name
    that was made by hand in Discounts is left alone. Returns a note for the Settings page."""
    code = (partner.get("family_code") or "").strip().upper()
    old_code = (old_code or "").strip().upper()
    if not code and not old_code:
        return ""
    with _discount_codes_lock:
        try:
            codes = json.loads(s3.get_object(Bucket=R2_BUCKET, Key="meta/discount_codes.json")["Body"].read())
        except Exception as e:
            if str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) not in ("404", "NoSuchKey", "NotFound"):
                return "Couldn't reach Discounts to set up the family code — save again in a minute"
            codes = []
        if not isinstance(codes, list):
            return "Discounts couldn't be read, so the family code wasn't set up"
        note, touched = "", []
        if old_code and old_code != code:
            for c in codes:
                if c.get("code", "").upper() == old_code and c.get("partner_id") == partner["id"] and c.get("active", True):
                    c["active"] = False
                    touched.append(c)
        if code:
            c = next((x for x in codes if x.get("code", "").upper() == code), None)
            pct = float(partner.get("family_code_pct") or 0)
            desc = f"{partner['name']} family code"
            if c is None:
                c = {"id": str(uuid.uuid4())[:8], "code": code,
                     "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                     "description": desc, "source": "partner", "partner_id": partner["id"],
                     "type": "percent", "amount": pct, "free_shipping": False, "applies_to": "digital",
                     "min_order": 0.0, "min_qty": 0, "usage_limit": None, "usage_limit_per_user": None,
                     "expires": None, "start_date": None, "active": True,
                     "usage_count": 0, "total_saved": 0.0, "uses": []}
                codes.append(c)
                touched.append(c)
                note = f"Code {code} was added to Discounts ({pct:g}% off)."
            elif c.get("partner_id") == partner["id"]:
                if c.get("amount") != pct or not c.get("active", True) or c.get("type") != "percent" \
                        or c.get("applies_to") != "digital":
                    c.update(type="percent", amount=pct, active=True, applies_to="digital")   # digital photos only
                    touched.append(c)
            else:
                note = (f"A code named {code} already exists in Discounts, so its own settings there are used "
                        f"({c.get('type')}, {c.get('amount')}).")
        for c in touched:                       # WooCommerce needs the same codes for the online checkout
            wc = _wc_sync_coupon(c)
            if "wc_id" in wc:
                c["wc_id"] = wc["wc_id"]
                c.pop("wc_sync_error", None)
            else:
                c["wc_sync_error"] = wc.get("error", "")
        if touched:
            _save_discount_codes(codes)
        return note


def _partner_ids() -> set:
    return {p.get("id") for p in _load_partners() if p.get("id")}


def _partner_by_slug(slug: str) -> dict | None:
    slug = (slug or "").strip().lower()
    return next((p for p in _load_partners() if slug and (p.get("slug") or "").lower() == slug), None)


def _partner_cookie_name(partner: dict) -> str:
    return "ci_partner_" + re.sub(r"[^a-z0-9]", "", partner["id"].lower())


def _partner_sig(partner: dict, expires: int) -> str:
    # Tied to the current password: changing it signs every device out
    pw = hashlib.sha256((partner.get("password") or "").encode()).hexdigest()[:16]
    return hmac.new((ADMIN_PASSWORD or "").encode(), f"partner|{partner['id']}|{pw}|{expires}".encode(),
                    hashlib.sha256).hexdigest()


def _partner_token(partner: dict) -> tuple[str, int]:
    expires = int(_time.time()) + PARTNER_SESSION_DAYS * 86400
    return f"{expires}.{_partner_sig(partner, expires)}", PARTNER_SESSION_DAYS * 86400


def _partner_signed_in(request: Request, partner: dict) -> bool:
    if not partner.get("password"):
        return False
    raw = request.cookies.get(_partner_cookie_name(partner), "")
    exp, _, sig = raw.partition(".")
    try:
        expires = int(exp)
    except ValueError:
        return False
    return expires > _time.time() and hmac.compare_digest(sig, _partner_sig(partner, expires))


def _partner_photos(partner: dict) -> dict:
    """{date: [photo items]} for this partner's published photos."""
    return _folder_index()["partners"].get(partner["id"], {})


def _photo_taken(path: str) -> str:
    """When a photo was taken, 'YYYY:MM:DD HH:MM:SS[.sub]' from its EXIF, or '' if it has none."""
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=to_r2_key(path), Range="bytes=0-131071")
        exif = Image.open(BytesIO(obj["Body"].read()))._getexif() or {}
        taken = str(exif.get(36867) or exif.get(306) or "").strip()      # DateTimeOriginal, else DateTime
        sub = str(exif.get(37521) or "").strip()                         # fractions of a second, for bursts
        return f"{taken}.{sub}" if taken and sub.isdigit() else taken
    except Exception:
        return ""


def _fill_taken_times(items: list) -> None:
    """Remember the capture time on each photo that doesn't have one yet (item["taken"])."""
    todo = [i for i in items if "taken" not in i]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        for item, taken in zip(todo, ex.map(lambda i: _photo_taken(i["path"]), todo)):
            item["taken"] = taken
    _save_photo_list_soon()


def _prepare_partner_photos(items: list) -> None:
    """Background work for photos that just became a partner's: their capture times (the page
    shows them in the order taken) and the large no-watermark version its enlarged view uses,
    made ahead so it opens right away."""
    _fill_taken_times(items)
    for item in items:
        key = to_r2_key(item["path"])
        if not _stored_thumb_link(key, "large"):
            _thumb_pool.submit(_make_thumbs_quietly, key, ["large"])


def _taken_order(item: dict) -> tuple:
    """Sort key: by time taken; photos with no time go last, then by file name."""
    taken = item.get("taken") or ""
    return (0 if taken else 1, taken, natural_sort_key(item["path"]))


def _partner_photo(partner: dict, path: str) -> dict | None:
    item = _folder_index()["all_by_path"].get(path or "")
    return item if item and item.get("partner") == partner["id"] else None


# Downloads are logged for the partner's in-kind donation report: each photo counts once
# (the first download), with how many times it was downloaded. One file per partner; a read
# that fails for any reason other than "not there yet" stops the save, so it's never wiped.
_partner_log_lock = threading.Lock()
_partner_logs: dict = {}


def _partner_log_key(pid: str) -> str:
    return f"meta/partner_downloads/{pid}.json"


def _load_partner_log(pid: str) -> dict:
    if pid in _partner_logs:
        return _partner_logs[pid]
    try:
        log = json.loads(s3.get_object(Bucket=R2_BUCKET, Key=_partner_log_key(pid))["Body"].read())
    except Exception as e:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise
        log = {"photos": {}}
    _partner_logs[pid] = log
    return log


def _partner_report(partner: dict, date_from: str = "", date_to: str = "") -> dict:
    """In-kind donation report: photos first downloaded between the dates (Big Sky days,
    inclusive; blank = no limit), each counted once at the partner's value per photo."""
    with _partner_log_lock:
        log = _load_partner_log(partner["id"])
        photos = dict(log.get("photos") or {})
        sold = list(log.get("prints") or [])
    value = float(partner.get("value_per_photo") or 0)
    rows = []
    for path, rec in photos.items():
        try:
            day = datetime.fromisoformat(rec["first"]).astimezone(_MOUNTAIN).date().isoformat()
        except Exception:
            continue
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        rows.append({"first_download": day, "photo_date": rec.get("photo_date", ""),
                     "file": os.path.basename(path), "path": path, "downloads": int(rec.get("count") or 0)})
    rows.sort(key=lambda r: (r["first_download"], r["photo_date"], r["file"]))
    by_day: dict = {}
    for r in rows:
        by_day[r["first_download"]] = by_day.get(r["first_download"], 0) + 1
    prints = []
    for rec in sold:
        try:
            day = datetime.fromisoformat(rec["date"]).astimezone(_MOUNTAIN).date().isoformat()
        except Exception:
            continue
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        prints.append({"date": day, "source": rec.get("source", ""), "file": os.path.basename(rec.get("path", "")),
                       "size": rec.get("size", ""), "price": float(rec.get("price") or 0), "full": float(rec.get("full") or 0)})
    prints.sort(key=lambda r: (r["date"], r["file"]))
    paid = round(sum(r["price"] for r in prints), 2)
    regular = round(sum(r["full"] for r in prints), 2)
    return {"partner": partner["name"], "date_from": date_from, "date_to": date_to,
            "value_per_photo": value, "photos": len(rows), "total_value": round(len(rows) * value, 2),
            "downloads": sum(r["downloads"] for r in rows),
            "by_day": [{"date": d, "photos": n, "value": round(n * value, 2)} for d, n in sorted(by_day.items())],
            "rows": rows,
            # prints of their photos sold at the partner discount (online and in the store)
            "prints": {"count": len(prints), "paid": paid, "regular": regular, "discount": round(regular - paid, 2)},
            "print_rows": prints}


def _log_partner_downloads(partner: dict, items: list, how: str) -> None:
    if not items:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _partner_log_lock:
            log = _load_partner_log(partner["id"])
            for item in items:
                rec = log["photos"].setdefault(item["path"], {"first": now, "photo_date": item.get("date", ""),
                                                              "count": 0})
                rec["count"] += 1
                rec["last"] = now
                rec["how"] = how
            s3.put_object(Bucket=R2_BUCKET, Key=_partner_log_key(partner["id"]),
                          Body=json.dumps(log).encode(), ContentType="application/json")
    except Exception as e:
        print(f"Partner download log not saved ({partner['id']}): {e}")


def _log_partner_prints(pid: str, prints: list, order_key: str) -> None:
    """Prints of a partner's photos sold at the partner discount, for their report: one entry
    per print ({source, path, size, price, full}). An order is recorded once (the shop can
    report a paid order more than once)."""
    if not prints:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _partner_log_lock:
            log = _load_partner_log(pid)
            sold = log.setdefault("prints", [])
            if any(x.get("order") == order_key for x in sold):
                return
            sold += [{"order": order_key, "date": now, **p} for p in prints]
            s3.put_object(Bucket=R2_BUCKET, Key=_partner_log_key(pid),
                          Body=json.dumps(log).encode(), ContentType="application/json")
    except Exception as e:
        print(f"Partner print sales not saved ({pid}, {order_key}): {e}")


def _log_partner_pos_sale(items: list, photo_paths: list, order_id: str) -> None:
    """A POS sale: partner photos given as free digital files count in the donation report, and
    prints of partner photos sold at the partner discount are recorded."""
    idx = _folder_index()["all_by_path"]
    partners = {p["id"]: p for p in _load_partners()}
    given = {}
    for path in photo_paths:
        item = idx.get(path)
        pid = (item or {}).get("partner")
        if pid in partners:
            given.setdefault(pid, []).append(item)
    for pid, photos in given.items():
        _log_partner_downloads(partners[pid], photos, "pos")
    sizes = _load_print_pricing().get("sizes", list(_DEFAULT_PRINT_SIZES))
    sold = {}
    for it in items:
        if it.get("category") != "prints" or not it.get("_partnerPrint"):
            continue
        path = (it.get("photo_paths") or [""])[0]
        pid = (idx.get(path) or {}).get("partner")
        if pid not in partners:
            continue
        si = int(it.get("_size_idx") or 0)
        full = float(sizes[si].get("price", 0)) if 0 <= si < len(sizes) else 0.0
        one = {"source": "pos", "path": path, "size": it.get("label", ""), "full": full,
               "price": round(float(it.get("price") or 0) - float(it.get("_logoPrice") or 0), 2)}
        sold.setdefault(pid, []).extend(dict(one) for _ in range(max(1, int(it.get("qty") or 1))))
    for pid, prints in sold.items():
        _log_partner_prints(pid, prints, f"pos:{order_id}")


def _log_partner_online_prints(order: dict) -> None:
    """A paid online order: its prints of partner photos (marked _partner_print when priced)."""
    known = {p["id"] for p in _load_partners()}
    sold = {}
    for li in order.get("line_items") or []:
        meta = {m.get("key"): m.get("value") for m in li.get("meta_data") or []}
        raw = meta.get("_partner_print")
        if not raw:
            continue
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            continue
        if info.get("partner") not in known:
            continue
        price = round(float(li.get("total") or 0) / max(1, int(li.get("quantity") or 1)) - float(info.get("logo") or 0), 2)
        sold.setdefault(info["partner"], []).append({"source": "online", "path": info.get("path", ""),
                                                     "size": info.get("size", ""), "price": price,
                                                     "full": float(info.get("full") or 0)})
    for pid, prints in sold.items():
        _log_partner_prints(pid, prints, f"wc:{order.get('id')}")


# ── Marketing email opt-outs ──────────────────────────────────────────────────
# Follow-ups, review requests and offers are marketing email: each one carries the business
# mailing address and a working unsubscribe link (one click, also from Gmail/Apple Mail's own
# button), and unsubscribed addresses are never sent marketing again. Receipts, download links
# and sign-in codes are transactional and always go out.
EMAIL_OPTOUTS_KEY = "meta/email_optouts.json"
_email_optouts_doc = JsonDoc(s3, R2_BUCKET, EMAIL_OPTOUTS_KEY, ttl=30, default=dict)
_email_optouts_lock = threading.Lock()


def _unsub_sig(part: str) -> str:
    secret = (os.getenv("UNSUBSCRIBE_SECRET") or ADMIN_PASSWORD or "").encode()
    return hmac.new(secret, f"unsub|{part}".encode(), hashlib.sha256).hexdigest()[:24]


def _unsubscribe_token(email: str) -> str:
    e = base64.urlsafe_b64encode(email.strip().lower().encode()).decode().rstrip("=")
    return f"{e}.{_unsub_sig(e)}"


def _email_from_unsubscribe_token(token: str) -> str | None:
    e, _, sig = (token or "").partition(".")
    if not e or not hmac.compare_digest(sig, _unsub_sig(e)):
        return None
    try:
        return base64.urlsafe_b64decode(e + "=" * (-len(e) % 4)).decode()
    except Exception:
        return None


def _unsubscribe_url(email: str) -> str:
    return f"{SITE_URL}/unsubscribe/{_unsubscribe_token(email)}"


def _opted_out(email: str) -> dict | None:
    return _email_optouts_doc.load().get((email or "").strip().lower())


def _record_optout(email: str, how: str) -> None:
    key = (email or "").strip().lower()
    if not key:
        return
    with _email_optouts_lock:
        _email_optouts_doc.cache["data"] = None          # read the latest before changing it
        optouts = _email_optouts_doc.load()
        if key not in optouts:
            optouts[key] = {"date": datetime.now(timezone.utc).isoformat(), "how": how}
            _email_optouts_doc.save(optouts)


def _business_details() -> dict:
    saved = _load_settings().get("business") or {}
    return {**_SETTINGS_DEFAULTS["business"], **{k: v for k, v in saved.items() if v}}


def _marketing_footer_html(email: str, why: str = "") -> str:
    b = _business_details()
    where = ", ".join(x for x in (b.get("name"), b.get("address")) if x)
    return (f'<p style="margin:.75rem 0 0;font-size:.7rem;color:#aaa;line-height:1.5">{_html_escape(where)}<br>'
            f'You\'re receiving this because {_html_escape(why or "you bought photos from us")}. '
            f'<a href="{_unsubscribe_url(email)}" style="color:#888">Unsubscribe</a></p>')


def _marketing_footer_text(email: str) -> str:
    b = _business_details()
    where = ", ".join(x for x in (b.get("name"), b.get("address")) if x)
    return f"\n\n--\n{where}\nUnsubscribe: {_unsubscribe_url(email)}"


def _list_unsubscribe_headers(email: str) -> dict:
    return {"List-Unsubscribe": f"<{_unsubscribe_url(email)}>, <mailto:info@bigskyphotos.com?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}

def _send_package_email(to_emails: list, title: str, download_url: str, photo_count: int, expire_str: str):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY not set — skipping email")
        return
    tmpl = _load_email_templates().get("package_download", _EMAIL_TEMPLATE_DEFAULTS["package_download"])
    dl_btn_html = (
        f'<p style="margin:0 0 1rem"><a href="{download_url}" '
        f'style="background:#1a73e8;color:#fff;padding:.65rem 1.5rem;border-radius:6px;'
        f'font-size:.9rem;font-weight:600;text-decoration:none;display:inline-block">'
        f'View &amp; Download Photos</a></p>'
    )
    for email in to_emails:
        first = _customer_first_name(email) or "there"
        plain_vars = {
            "first_name": first,
            "title": title,
            "photo_count": str(photo_count),
            "expire_date": expire_str,
        }
        html_blocks = {"download_button": dl_btn_html}
        subject = tmpl["subject"]
        for k, v in plain_vars.items():
            subject = subject.replace(f"{{{{{k}}}}}", v)
        body_html, body_text = _render_email_body(tmpl["body"], plain_vars, html_blocks)
        html = _wrap_email_html(body_html)
        try:
            r = http_requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": RESEND_FROM, "to": [email], "subject": subject, "html": html, "text": body_text},
                timeout=10,
            )
            if not r.ok:
                print(f"Resend error ({email}): {r.text}")
        except Exception as e:
            print(f"Email send failed ({email}): {e}")


def _send_pos_receipt_email(to_email: str, customer_name: str, order_id: str,
                            items: list, total: float, coupon_code: str,
                            coupon_discount: float, download_url: str | None,
                            expire_str: str | None = None,
                            square: dict | None = None, comp: bool = False,
                            package_note: str = ""):
    if not RESEND_API_KEY or not to_email:
        return
    tmpl = _load_email_templates().get("pos_receipt", _EMAIL_TEMPLATE_DEFAULTS["pos_receipt"])
    # Square sales: use what Square actually charged (resort tax + tip included)
    if square:
        tax         = round(float(square.get("square_tax") or 0), 2)
        tip         = round(float(square.get("square_tip") or 0), 2)
        grand_total = round(float(square.get("square_paid") or 0), 2)
    else:
        tax = round(total * 0.04, 2)
        tip = 0.0
        grand_total = round(total + tax, 2)
    rows = ""
    for item in items:
        label   = item.get("label", "Item")
        qty     = item.get("qty", 1)
        price   = float(item.get("price", 0))
        subtotal = float(item.get("subtotal", price * qty))
        detail  = item.get("detail", "")
        rows += (
            f'<tr>'
            f'<td style="padding:.45rem .5rem;border-bottom:1px solid #eee;font-size:.88rem">'
            f'{label}'
            + (f' <span style="font-size:.78rem;color:#888">— {detail}</span>' if detail else '') +
            f'</td>'
            f'<td style="padding:.45rem .5rem;border-bottom:1px solid #eee;font-size:.88rem;text-align:center;color:#666">{qty}</td>'
            f'<td style="padding:.45rem .5rem;border-bottom:1px solid #eee;font-size:.88rem;text-align:right;font-weight:600">${subtotal:.2f}</td>'
            f'</tr>'
        )
    coupon_row = ""
    if coupon_code and coupon_discount > 0:
        coupon_row = (
            f'<tr><td colspan="2" style="padding:.35rem .5rem;font-size:.83rem;color:#888">Coupon {coupon_code.upper()}</td>'
            f'<td style="padding:.35rem .5rem;font-size:.83rem;text-align:right;color:#e05252">-${coupon_discount:.2f}</td></tr>'
        )
    items_table_html = (
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:.75rem">'
        f'<thead><tr style="border-bottom:2px solid #eee">'
        f'<th style="padding:.4rem .5rem;font-size:.75rem;color:#999;text-align:left;font-weight:500">ITEM</th>'
        f'<th style="padding:.4rem .5rem;font-size:.75rem;color:#999;text-align:center;font-weight:500">QTY</th>'
        f'<th style="padding:.4rem .5rem;font-size:.75rem;color:#999;text-align:right;font-weight:500">TOTAL</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        f'{coupon_row}'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:1.25rem">'
        f'<tr><td style="padding:.25rem .5rem;font-size:.83rem;color:#888">Subtotal</td>'
        f'<td style="padding:.25rem .5rem;font-size:.83rem;text-align:right">${total:.2f}</td></tr>'
        + (
            '<tr style="border-top:2px solid #eee">'
            '<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700">Complimentary</td>'
            '<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700;text-align:right">No charge</td></tr>'
            if comp else
            f'<tr><td style="padding:.25rem .5rem;font-size:.83rem;color:#888">4% Resort Tax</td>'
            f'<td style="padding:.25rem .5rem;font-size:.83rem;text-align:right">${tax:.2f}</td></tr>'
            + (f'<tr><td style="padding:.25rem .5rem;font-size:.83rem;color:#888">Tip</td>'
               f'<td style="padding:.25rem .5rem;font-size:.83rem;text-align:right">${tip:.2f}</td></tr>' if tip else '')
            + '<tr style="border-top:2px solid #eee">'
            '<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700">Total Charged</td>'
            f'<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700;text-align:right">${grand_total:.2f}</td></tr>'
        )
        + '</table>'
    )
    dl_btn_html = ""
    if download_url:
        dl_btn_html = (
            f'<p style="margin:0 0 .5rem;font-size:.9rem;font-weight:600;color:#111">Your Photos</p>'
            f'<p style="margin:0 0 1rem"><a href="{download_url}" '
            f'style="background:#1a73e8;color:#fff;padding:.55rem 1.25rem;border-radius:6px;'
            f'font-size:.88rem;font-weight:600;text-decoration:none;display:inline-block">'
            f'{"Open Your Photo Page" if package_note else "Download Your Photos"}</a></p>'
            + (f'<p style="margin:0 0 1rem;font-size:.8rem;color:#888">{_html_escape(package_note)}</p>' if package_note else
               f'<p style="margin:0 0 1rem;font-size:.8rem;color:#888">Your download link is available until {expire_str}.</p>'
               if expire_str else '')
        )
    body_tmpl = tmpl["body"]
    if dl_btn_html and "{{download_button}}" not in body_tmpl:
        # The receipt is the only email with the photo link, so never let a template edit drop it
        body_tmpl += "\n\n{{download_button}}"
    first_name = (customer_name.split()[0] if customer_name else "") or _customer_first_name(to_email) or "there"
    plain_vars = {"first_name": first_name, "order_id": order_id}
    html_blocks = {"items_table": items_table_html, "download_button": dl_btn_html}
    subject = tmpl["subject"]
    for k, v in plain_vars.items():
        subject = subject.replace(f"{{{{{k}}}}}", v)
    body_html, _ = _render_email_body(body_tmpl, plain_vars, html_blocks)
    html = _wrap_email_html(body_html)
    try:
        r = http_requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": html},
            timeout=10,
        )
        if not r.ok:
            print(f"Receipt email error ({to_email}): {r.text}")
    except Exception as e:
        print(f"Receipt email failed ({to_email}): {e}")


def _store_pkg(token: str, payload: dict):
    s3.put_object(Bucket=R2_BUCKET, Key=f"packages/{token}/meta.json",
                  Body=json.dumps(payload).encode(), ContentType="application/json")

def _load_pkg(token: str):
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"packages/{token}/meta.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None

def _list_pkgs():
    pkgs = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="packages/", Delimiter="/"):
            for pfx in page.get("CommonPrefixes", []):
                token = pfx["Prefix"].replace("packages/", "").rstrip("/")
                pkg = _load_pkg(token)
                if pkg:
                    pkgs.append(pkg)
    except Exception as e:
        print(f"_list_pkgs: {e}")
    return sorted(pkgs, key=lambda p: p.get("created", ""), reverse=True)

def _delete_pkg_r2(token: str):
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"packages/{token}/"):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": keys})
    except Exception as e:
        print(f"_delete_pkg_r2: {e}")


def _pkg_error(msg: str, status: int, sub: str = "") -> str:
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:2rem">
<div><h2 style="color:#{'c0392b' if status>=400 else 'F2C94C'};margin-bottom:.75rem">{msg}</h2>
{"<p style='color:rgba(255,255,255,.5);font-size:.9rem'>"+sub+"</p>" if sub else ""}
</div></body></html>"""


class _ZipStream:
    """Write-only target for zipfile that hands back what's been written so far, so a
    zip can be streamed without keeping the whole file in memory."""
    def __init__(self):
        self._parts, self._pos = [], 0

    def write(self, b):
        self._parts.append(bytes(b))
        self._pos += len(b)
        return len(b)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def take(self) -> bytes:
        out, self._parts = b"".join(self._parts), []
        return out


# ── Background startup work that needs every function above to exist ─────────
threading.Thread(target=_backfill_square_id_markers, daemon=True).start()

from routes import orders as _routes_orders  # noqa: E402
app.include_router(_routes_orders.router)

from routes import auth as _routes_auth  # noqa: E402
app.include_router(_routes_auth.router)

from routes import checkout as _routes_checkout  # noqa: E402
app.include_router(_routes_checkout.router)

from routes import email as _routes_email  # noqa: E402
app.include_router(_routes_email.router)

from routes import discounts as _routes_discounts  # noqa: E402
app.include_router(_routes_discounts.router)

from routes import customers as _routes_customers  # noqa: E402
app.include_router(_routes_customers.router)

from routes import staff as _routes_staff  # noqa: E402
app.include_router(_routes_staff.router)

from routes import pos as _routes_pos  # noqa: E402
app.include_router(_routes_pos.router)

from routes import downloads as _routes_downloads  # noqa: E402
app.include_router(_routes_downloads.router)

from routes import gallery as _routes_gallery  # noqa: E402
app.include_router(_routes_gallery.router)

from routes import trash as _routes_trash  # noqa: E402
app.include_router(_routes_trash.router)

from routes import folders as _routes_folders  # noqa: E402
app.include_router(_routes_folders.router)

from routes import admin_pages as _routes_admin_pages  # noqa: E402
app.include_router(_routes_admin_pages.router)

from routes import admin_pricing as _routes_admin_pricing  # noqa: E402
app.include_router(_routes_admin_pricing.router)

from routes import admin_settings as _routes_admin_settings  # noqa: E402
app.include_router(_routes_admin_settings.router)

from routes import admin_photos as _routes_admin_photos  # noqa: E402
app.include_router(_routes_admin_photos.router)

from routes import unsubscribe as _routes_unsubscribe  # noqa: E402
app.include_router(_routes_unsubscribe.router)

from routes import legal as _routes_legal  # noqa: E402
app.include_router(_routes_legal.router)

from routes import unlimited as _routes_unlimited  # noqa: E402
app.include_router(_routes_unlimited.router)

from routes import partners as _routes_partners  # noqa: E402
app.include_router(_routes_partners.router)

from routes import saved_carts as _routes_saved_carts  # noqa: E402
app.include_router(_routes_saved_carts.router)

from routes import qr_codes as _routes_qr_codes  # noqa: E402
app.include_router(_routes_qr_codes.router)

from routes import store as _routes_store  # noqa: E402
app.include_router(_routes_store.router)

# Last on purpose: partner pages at /<slug> (e.g. /eaglemount) match any one-part address, so
# every other page has to be registered before them
app.include_router(_routes_partners.page_router)
