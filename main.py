import threading
import zipfile
import time as _time
import numpy as np
from collections import OrderedDict, deque
from contextlib import contextmanager
from html import escape as _html_escape
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
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
from rapidfuzz import fuzz
import os
import base64
from io import BytesIO
from PIL import Image, ImageOps
from functools import lru_cache
import boto3
import requests as http_requests
from core.images import (admin_thumb_r2_key as _admin_thumb_r2_key, fix_orientation,
                         grid_thumb_r2_key as _grid_thumb_r2_key, nowm_thumb_r2_key as _nowm_thumb_r2_key,
                         thumb_r2_key as _thumb_r2_key)
from core.storage import JsonDoc
from core.text import (clean_location, folder_key as _folder_key, item_group_from_path as _item_group_from_path,
                       natural_sort_key, pick_four as _pick_four, to_r2_key)
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
    files = ["model_v3.onnx", "model_v3.onnx.data", "model_v3_meta.json"]
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
SEARCHABLE_GROUP_LOCATIONS = {"mountain biking"}

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

_HAIKU_COLOR_ENUM = [
    "red", "orange", "yellow", "green", "blue", "purple", "pink",
    "white", "black", "gray", "brown", "tan", "teal", "camo", "none", "unclear",
]

# Color families — similar shades treated as equivalent during filtering
_COLOR_FAMILY: dict[str, set] = {
    "blue":   {"blue", "navy"},         # navy kept for existing labeled photos
    "tan":    {"tan"},
    "red":    {"red", "maroon"},
    "maroon": {"maroon", "red"},
    "green":  {"green"},
    "teal":   {"teal"},
    "gray":   {"gray"},
    "purple": {"purple", "violet"},
    "violet": {"violet", "purple"},
}

def _color_family(color: str) -> set:
    return _COLOR_FAMILY.get(color, {color})


# ── ONNX Model Inference ──────────────────────────────────────────────────────

_onnx_session = None
_onnx_meta    = None
_onnx_lock    = threading.Lock()

_CLIP_MEAN = np.array([0.48145466, 0.4578275,  0.40821073], dtype=np.float32).reshape(3, 1, 1)
_CLIP_STD  = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32).reshape(3, 1, 1)


def _load_onnx_model():
    global _onnx_session, _onnx_meta
    with _onnx_lock:
        if _onnx_session is not None:
            return True
        onnx_path = "model_v3.onnx"
        meta_path = "model_v3_meta.json"
        if not (os.path.exists(onnx_path) and os.path.exists(meta_path)):
            return False
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            _onnx_session = ort.InferenceSession(onnx_path, sess_options=opts, providers=["CPUExecutionProvider"])
            with open(meta_path) as f:
                _onnx_meta = json.load(f)
            print(f"ONNX model loaded (best_score={_onnx_meta.get('best_score', '?'):.3f})")
            return True
        except Exception as e:
            print(f"ONNX load failed: {e}")
            return False


MODEL_LABEL = "model_v3"     # stamped on each photo the model labels

# Decision rules, tuned 2026-10-18 on 2,000 labeled photos v3 never trained on (6/6 + 6/7):
# photos needing no fix went from 60.7% to 62.9%, and every folder improved.
#  - always give the most likely color (when v3 used to say "none" it was almost always a real color)
#  - add a second jacket color only when it scores at least 0.50 and within 0.15 of the first
#  - pants get one color
_SECOND_JACKET_MIN = 0.50
_SECOND_JACKET_GAP = 0.15


def _preprocess_image(img_bytes: bytes) -> np.ndarray:
    img = Image.open(BytesIO(img_bytes))
    if img.format in ("JPEG", "MPO"):   # MPO = camera JPEG with a built-in preview
        # Decode at reduced scale (still >= 900px): about half the time, same answers
        # (checked on the same 2,000 photos)
        img.draft("RGB", (900, 900))
    pil = img.convert("RGB").resize((224, 224), Image.LANCZOS)
    arr = np.array(pil, dtype=np.float32).transpose(2, 0, 1) / 255.0  # HWC -> CHW
    arr = (arr - _CLIP_MEAN) / _CLIP_STD
    return arr[np.newaxis]  # (1, 3, 224, 224)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def classify_photo_colors_onnx(img_bytes: bytes) -> dict | None:
    if not _load_onnx_model():
        return None
    try:
        inp = _preprocess_image(img_bytes)
        j_logits, p_logits, s_logits = _onnx_session.run(
            ["jacket_logits", "pants_logits", "sport_logits"],
            {"pixel_values": inp},
        )
        colors     = _onnx_meta["all_colors"]
        sports     = _onnx_meta["sports"]

        j_prob = _sigmoid(j_logits[0])
        p_prob = _sigmoid(p_logits[0])

        j_sorted = np.argsort(j_prob)[::-1]
        jacket = [colors[j_sorted[0]]]
        if (len(j_sorted) > 1 and j_prob[j_sorted[1]] >= _SECOND_JACKET_MIN
                and j_prob[j_sorted[0]] - j_prob[j_sorted[1]] < _SECOND_JACKET_GAP):
            jacket.append(colors[j_sorted[1]])

        pants = [colors[int(np.argmax(p_prob))]]

        sport_idx = int(np.argmax(s_logits[0]))
        sport = sports[sport_idx]

        def _remap(colors):
            return ['blue' if c == 'teal' else c for c in colors]
        return {"sport": sport, "jacket_colors": _remap(jacket), "pants_colors": _remap(pants)}
    except Exception as e:
        print(f"ONNX classify failed: {e}")
        return None


def classify_photo_colors(img_bytes: bytes) -> dict | None:
    """Label sport, jacket and pants colors with the ONNX model (v3)."""
    return classify_photo_colors_onnx(img_bytes)


def embed_items_fast(items: list, job_id: str = None) -> int:
    """
    Parallel R2 fetch + color classification with the ONNX model.
    Returns count processed.
    """
    from concurrent.futures import ThreadPoolExecutor

    def process_one(item):
        try:
            resp      = s3.get_object(Bucket=R2_BUCKET, Key=item["path"])
            img_bytes = resp["Body"].read()
            _is_portrait_loc = item.get("location", "").strip().lower() in {
                loc.strip().lower() for loc in PORTRAIT_LOCATIONS
            }
            if item.get("jacket_colors") is None and not _is_portrait_loc:
                colors = classify_photo_colors(img_bytes)
                if colors:
                    item["sport"]         = colors.get("sport", "unclear")
                    item["jacket_colors"] = colors.get("jacket_colors", [])
                    item["pants_colors"]  = colors.get("pants_colors", [])
                    item["classifier"] = MODEL_LABEL
                    print(f"{MODEL_LABEL}: {item['path'].split('/')[-1]} → sport={item['sport']} jacket={item['jacket_colors']} pants={item['pants_colors']}")
            return True
        except Exception as e:
            print(f"Process failed {item['path']}: {e}")
            return False

    count = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(process_one, item): item for item in items}
        for fut in futures:
            if fut.result():
                count += 1
                if job_id and job_id in _embed_jobs:
                    _embed_jobs[job_id]["done"] = count

    if job_id and job_id in _embed_jobs:
        _embed_jobs[job_id]["finished"] = True
    return count


data: list = []
_data_ready = threading.Event()

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
            s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                          Body=json.dumps(data).encode(), ContentType="application/json")
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
_lastname_limiter  = _RateLimiter(max_calls=30, window=60)   # 30 req/min per IP

# ─────────────────────────────────────────
# CHECKOUT PRICING HELPERS
# ─────────────────────────────────────────

def _best_tier_price(count: int, tiers: list) -> float:
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


def _server_digital_price(album: dict, order_date: str) -> float:
    """Price for a digital album: the tiers the shop shows for that folder, with the
    shop's best-price rule. Raises ValueError if the folder has no prices set up."""
    count = int(album.get("count", 0))
    tiers = _pricing_for((album.get("location") or "").strip() or None,
                         (album.get("date") or order_date or "").strip() or None,
                         (album.get("last_name") or "").strip() or None).get("tiers") or []
    if count > 0 and not tiers:
        raise ValueError(f"Prices for {album.get('location') or 'these photos'} aren't set up yet")
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


def _build_folder_index(items: list) -> dict:
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


def _folder_photos(date: str, location: str) -> list:
    """Live (non-draft) photos in a folder, in photo-list order. location = cleaned name."""
    return _folder_index()["folders"].get((date, location), [])


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

@app.get("/api/days")
def get_days():
    return _folder_index()["days"]


@app.get("/api/dates")
def get_dates():
    dates = sorted(set(item["date"] for item in data), reverse=True)
    return {"dates": dates}


@app.get("/api/locations")
def get_locations(date: str = Query(None)):
    if date:
        items = _folder_index()["by_date"].get(date, [])
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

    locations = []
    for display in sorted(loc_map.keys()):
        locations.append({
            "name":     display,
            "previews": _pick_four(all_previews[display])
        })
    return {"locations": locations}


@app.get("/api/subfolders")
def get_subfolders(date: str, location: str):
    """Return sub-folders for a location — last names for portrait locations, groups for activities."""
    is_portrait = _is_portrait_location(location)
    subfolders = {}
    for item in _folder_photos(date, location):
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


@app.get("/api/last-name-search")
def last_name_search(request: Request, q: str = Query("")):
    """Autocomplete: return unique (last_name, date, location) combos matching q."""
    ip = _client_ip(request)
    if not _lastname_limiter.is_allowed(ip):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})
    q_lower = q.strip().lower()
    if len(q_lower) < 2:
        return {"results": []}

    index = _folder_index()
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


@app.get("/api/browse")
def browse(date: str, location: str, family: str = Query(None), group: str = Query(None),
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

    pool = [item for item in _folder_photos(date, location)
            if (not family or item.get("last_name","").strip().lower() == family.lower()
                 or (family and not item.get("last_name","").strip() and item.get("group","").strip().lower() == family.lower()))
            and group_matches(item)]
    pool.sort(key=lambda x: natural_sort_key(x["path"]))
    paths = [item["path"] for item in pool]
    sizes = _dims_for_paths(paths) if dims else {}
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
    # Attach pose group index from folder_meta when available
    if family and results:
        fk = _folder_key(date, location, family)
        fm = _load_folder_meta()
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


@app.get("/api/pos-dates")
def pos_dates():
    """POS: return unique (date, location) pairs for the date/location dropdowns."""
    return _folder_index()["pos_dates"]


@app.get("/api/pos-families")
def pos_families(date: str = Query(""), location: str = Query("")):
    """POS: return unique families/groups for a given date+location with preview paths."""
    from collections import defaultdict
    families: dict = {}
    previews: dict = defaultdict(list)
    for item in _folder_photos(date, location):
        ln = item.get("last_name", "").strip()
        grp = item.get("group", "").strip()
        name = ln or grp
        key = name.lower() if name else "__flat__"
        if not name:
            if "__flat__" not in families:
                families["__flat__"] = {"name": "", "last_name": "", "group": "", "date": date, "location": location}
        else:
            if key not in families:
                families[key] = {"name": name, "last_name": ln, "group": grp, "date": date, "location": location}
        if len(previews[key]) < 4:
            path = item.get("path", "")
            if path:
                previews[key].append(path)
    results = [
        {**v, "previews": previews[k]}
        for k, v in sorted(families.items(), key=lambda x: x[0] if x[0] != "__flat__" else "")
    ]
    return {"families": results}


@app.get("/api/search")
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
    ip = _client_ip(request)
    if not _search_limiter.is_allowed(ip):
        return JSONResponse(status_code=429, content={"error": "Too many requests — please wait a moment"})
    try:
        return _search(last_name, date, location, group, time_from, time_to,
                       jacket_color=jacket_color, pants_color=pants_color, sport=sport,
                       ski_instructor=ski_instructor)
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(e)})

def _search(last_name, date, location, group=None, time_from=None, time_to=None,
            jacket_color=None, pants_color=None, sport=None, ski_instructor=None):
    jacket_colors_filter = [c for c in (jacket_color or []) if c] if isinstance(jacket_color, list) else ([jacket_color] if jacket_color else [])
    has_labels = any([jacket_colors_filter, pants_color, sport, ski_instructor])
    if not last_name and not has_labels:
        return JSONResponse(status_code=400, content={"error": "Provide last_name or color filter"})

    # Build time lookup if a range was supplied
    time_map = None
    if time_from is not None and time_to is not None and date and location:
        cache_key = (date, location.strip().lower())
        cached = _folder_times_cache.get(cache_key)
        if cached:
            time_map = cached

    ln_filter = last_name.strip().lower() if last_name else ""

    if has_labels:
        # Color chip only — scan folder, filter strictly by Haiku labels
        results = []
        pool = _folder_photos(date, location) if date and location else data
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
            if jacket_colors_filter and not all(
                any(c in _color_family(jf) for c in item_jc) for jf in jacket_colors_filter
            ):
                continue
            if pants_color and not any(c in _color_family(pants_color) for c in (item.get("pants_colors") or [])):
                continue
            if sport and item.get("sport") != sport:
                continue
            if ski_instructor and not item.get("ski_instructor"):
                continue
            results.append((0.0, item))
        results.sort(key=lambda x: natural_sort_key(x[1]["path"]))

    else:
        # Last name only — scan data, sort by filename
        results = []
        for item in data:
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


@app.get("/api/pricing")
def get_pricing(request: Request, location: str = Query(None), date: str = Query(None), family: str = Query(None)):
    return _pricing_for(location, date, family)


def _pricing_for(location: str = None, date: str = None, family: str = None) -> dict:
    """The pricing tiers the shop shows for a folder (location, date, family): zip group
    pricing, date overrides, category pricing, the location's own pricing, then its
    item list. Checkout prices with the same tiers, so what's shown is what's charged."""
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
                fm = _load_folder_meta()
                fk = _folder_key(date, location.strip(), family.strip() if family else "")
                item_list_name = fm.get(fk, {}).get("item_list", "")
                if not item_list_name:
                    # Try prefix match when family is not in the request
                    prefix = f"{date}|{location.strip()}|"
                    for k, v in fm.items():
                        if k.startswith(prefix) and v.get("item_list"):
                            item_list_name = v["item_list"]
                            break
                if item_list_name:
                    bundles_data = _load_items_bundles()
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


# ── Thumbnails ────────────────────────────────────────────────────────────────
# Every photo has small versions, cached in R2 next to the original:
#   grid   — 700px WebP, watermarked: customer gallery    (thumbs/grid/)
#   pos    — 700px, no watermark: iPad POS               (thumbs/nowm/)
#   admin  — 450px, no watermark: studio, download pages (admin_thumbs/)
#   public — 700px JPEG, watermarked: the gallery version before WebP (thumbs/v2/).
#            No longer made; photos that already have one keep using it.
# They're made in the background when photos are uploaded or go live, and on
# demand (as a fallback) the first time one is requested.
_THUMB_VARIANTS = {
    # name:   (cache key,            max px, quality, watermark, Cache-Control,              format)
    "grid":   (_grid_thumb_r2_key,   700, 80, True,  "public, max-age=31536000", "WEBP"),
    "pos":    (_nowm_thumb_r2_key,   700, 80, False, "public, max-age=31536000", "JPEG"),
    "admin":  (_admin_thumb_r2_key,  450, 72, False, "private, max-age=86400",   "JPEG"),
    "public": (_thumb_r2_key,        700, 80, True,  "public, max-age=31536000", "JPEG"),
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
    buf = BytesIO()
    if fmt == "WEBP":
        t.save(buf, format="WEBP", quality=quality, method=6)
    else:
        t.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _make_thumbs(r2_key: str, variants, original: bytes = None) -> dict:
    """Make the given thumbnail variants from one download of the original and save
    them to R2. Returns {variant: jpeg bytes}. Saving is best-effort."""
    if original is None:
        original = s3.get_object(Bucket=R2_BUCKET, Key=r2_key)["Body"].read()
    img = _open_for_thumbnail(BytesIO(original), max(_THUMB_VARIANTS[v][1] for v in variants))
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


def _pos_signed_in(request: Request) -> bool:
    token = request.cookies.get(POS_COOKIE, "")
    if not ADMIN_PASSWORD or not token:
        return False
    try:
        expires = int(token.split(".", 1)[0])
    except ValueError:
        return False
    return expires > _time.time() and hmac.compare_digest(token, _pos_token(expires))


def _nowm_allowed(request: Request) -> bool:
    return _pos_signed_in(request) or _admin_authed(request)


def _make_thumbs_quietly(r2_key: str, variants, original: bytes = None) -> bool:
    try:
        _make_thumbs(r2_key, variants, original)
        return True
    except Exception as e:
        print(f"[thumbs] failed {r2_key}: {e}")
        return False


def _start_thumb_job(work: list) -> str | None:
    """Make thumbnails in the background. work = [(r2_key, [variants]), ...].
    Progress is visible at /api/admin/embed-status?job_id=…"""
    work = [(k, v) for k, v in work if v]
    if not work:
        return None
    job_id = "thumbs-" + str(uuid.uuid4())[:8]
    _embed_jobs[job_id] = {"total": len(work), "done": 0, "finished": False, "failed": 0}

    def _run():
        job = _embed_jobs[job_id]
        for ok in _thumb_pool.map(lambda kv: _make_thumbs_quietly(kv[0], kv[1]), work):
            job["done"] += 1
            if not ok:
                job["failed"] += 1
        job["finished"] = True
        _save_dim_cache_bg()   # sizes recorded while making the thumbnails
        print(f"[thumbs] job {job_id} done: {job['done'] - job['failed']}/{job['total']} ok")

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def _queue_new_photo_thumbs(r2_keys: list) -> str | None:
    """All thumbnail variants for newly uploaded / published / moved photos."""
    return _start_thumb_job([(to_r2_key(k), list(_NEW_PHOTO_VARIANTS)) for k in r2_keys])

@app.get("/api/photo")
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
    if nowm and not _nowm_allowed(request):
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
                    variant, link = "pos", _stored_thumb_link(key, "pos")
                else:
                    # Photos from before the WebP version keep their JPEG; new ones have WebP
                    variant, link = "grid", _stored_thumb_link(key, "grid") or _stored_thumb_link(key, "public")
                if link:
                    return _thumb_redirect(link)
                data = _thumb_on_demand(key, variant)
                return SR(BytesIO(data), media_type=_THUMB_MEDIA_TYPES[_THUMB_VARIANTS[variant][5]],
                          headers={"Cache-Control": f"{'private' if nowm else 'public'}, max-age={cache_secs}"})

            obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
            img = Image.open(obj["Body"]).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            if not nowm:
                img = apply_watermark(img, size)
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
                img = apply_watermark(img, size)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            buf.seek(0)
            return SR(buf, media_type="image/jpeg")
        else:
            return JSONResponse(status_code=404, content={"error": "File not found"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


_folder_times_cache: dict = {}  # in-memory cache: (date, location) → {filename: minutes}

def _times_r2_key(date: str, location: str) -> str:
    slug = location.strip().lower().replace(" ", "-")
    return f"meta/times/{date}/{slug}.json"

@app.get("/api/admin/color-labels")
async def admin_color_labels(request: Request, date: str = Query(""), location: str = Query(""), group: str = Query("")):
    """Return per-photo color labels for a folder. Admin only."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    loc_lower = location.strip().lower()
    grp_lower = group.strip().lower()
    results = []
    for item in data:
        if item.get("draft"):
            continue
        if date and item.get("date") != date:
            continue
        if loc_lower and clean_location(item.get("location", "")).lower() != loc_lower:
            continue
        if grp_lower and (item.get("group", "") or item.get("last_name", "")).strip().lower() != grp_lower:
            continue
        results.append({
            "file": item["path"].split("/")[-1],
            "jacket": item.get("jacket_colors"),
            "pants":  item.get("pants_colors"),
        })
    return {"count": len(results), "photos": results}


@app.get("/api/admin/folder-photos")
async def admin_folder_photos(request: Request, date: str = Query(""), location: str = Query(""), group: str = Query("")):
    """Return photo paths and thumbnail URLs for a folder. Admin only."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    loc_lower = location.strip().lower()
    grp_lower = group.strip().lower()
    results = []
    for item in data:
        if item.get("draft"):
            continue
        if date and item.get("date") != date:
            continue
        if loc_lower and clean_location(item.get("location", "")).lower() != loc_lower:
            continue
        if grp_lower and (item.get("group", "") or item.get("last_name", "")).strip().lower() != grp_lower:
            continue
        results.append({
            "path":       item["path"],
            "file":       item["path"].split("/")[-1],
            "jacket":     item.get("jacket_colors"),
            "pants":      item.get("pants_colors"),
            "sport":      item.get("sport"),
            "instructor": bool(item.get("ski_instructor")),
        })
    results.sort(key=lambda x: natural_sort_key(x["file"]))
    return {"photos": results}


@app.post("/api/admin/reclassify-photos")
async def admin_reclassify_photos(request: Request):
    """Clear and re-run the labeling model on a specific list of photo paths."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not _load_onnx_model():
        return JSONResponse(status_code=503, content={"error": "The labeling model isn't available"})
    body  = await request.json()
    paths = body.get("paths", [])
    if not paths:
        return JSONResponse(status_code=400, content={"error": "No paths provided"})

    path_set = set(paths)
    targets  = [it for it in data if it["path"] in path_set]
    if not targets:
        return JSONResponse(status_code=404, content={"error": "No matching photos found"})

    for it in targets:
        it["sport"]         = None
        it["jacket_colors"] = None
        it["pants_colors"]  = None

    job_id = str(uuid.uuid4())[:8]
    _embed_jobs[job_id] = {"total": len(targets), "done": 0, "finished": False}

    def _bg(jid=job_id):
        done = 0
        for it in targets:
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=it["path"])
                raw  = resp["Body"].read()
                colors = classify_photo_colors(raw)
                if colors:
                    it["sport"]         = colors.get("sport", "unclear")
                    it["jacket_colors"] = colors.get("jacket_colors", [])
                    it["pants_colors"]  = colors.get("pants_colors", [])
                    it["classifier"]    = MODEL_LABEL
                    print(f"Re-classify: {it['path'].split('/')[-1]} → sport={it['sport']} jacket={it['jacket_colors']} pants={it['pants_colors']}")
                done += 1
                if jid in _embed_jobs:
                    _embed_jobs[jid]["done"] = done
            except Exception as e:
                print(f"reclassify failed {it['path']}: {e}")
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
        if jid in _embed_jobs:
            _embed_jobs[jid]["finished"] = True

    threading.Thread(target=_bg, daemon=True).start()
    return {"queued": len(targets), "job_id": job_id}


@app.post("/api/admin/set-photo-colors")
async def admin_set_photo_colors(request: Request):
    """Manually set jacket/pants/sport labels on a single photo."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse(status_code=400, content={"error": "path required"})
    item = next((it for it in data if it["path"] == path), None)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Photo not found"})
    if "jacket_colors" in body: item["jacket_colors"] = body["jacket_colors"] or []
    if "pants_colors"  in body: item["pants_colors"]  = body["pants_colors"]  or []
    if "sport"         in body: item["sport"]          = body["sport"] or None
    if "ski_instructor"  in body: item["ski_instructor"]  = bool(body["ski_instructor"])
    item["manually_corrected"] = True
    def _flush():
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    threading.Thread(target=_flush, daemon=True).start()
    return {"ok": True, "path": path}


@app.post("/api/admin/set-photos-colors-bulk")
async def admin_set_photos_colors_bulk(request: Request):
    """Set jacket/pants/sport labels on multiple photos in one R2 write."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    paths = body.get("paths") or []
    if not paths:
        return JSONResponse(status_code=400, content={"error": "paths required"})
    path_index = {it["path"]: it for it in data}
    updated = []
    for path in paths:
        item = path_index.get(path.strip())
        if not item:
            continue
        if "jacket_colors" in body: item["jacket_colors"] = body["jacket_colors"] or []
        if "pants_colors"  in body: item["pants_colors"]  = body["pants_colors"]  or []
        if "sport"         in body: item["sport"]          = body["sport"] or None
        if "ski_instructor" in body: item["ski_instructor"] = bool(body["ski_instructor"])
        item["manually_corrected"] = True
        updated.append(path)
    if updated:
        def _flush():
            s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                          Body=json.dumps(data).encode(), ContentType="application/json")
        threading.Thread(target=_flush, daemon=True).start()
    return {"ok": True, "updated": len(updated)}


@app.get("/api/folder-colors")
def get_folder_colors(date: str = Query(""), location: str = Query(""), group: str = Query("")):
    """Return distinct jacket/pants colors, sport types, and valid combos for a folder."""
    jacket, pants = set(), set()
    has_ski = has_snowboard = has_instructor = False
    j_to_p: dict = {}
    p_to_j: dict = {}
    j_to_j: dict = {}
    loc_lower = location.strip().lower()
    grp_lower = group.strip().lower()
    for item in data:
        if item.get("draft"):
            continue
        if date and item.get("date") != date:
            continue
        if loc_lower and clean_location(item.get("location", "")).lower() != loc_lower:
            continue
        if grp_lower and (item.get("group", "") or item.get("last_name", "")).strip().lower() != grp_lower:
            continue
        _valid = set(_HAIKU_COLOR_ENUM) - {"none", "unclear"}
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


@app.get("/api/folder-dates")
def get_folder_dates(location: str = Query("")):
    """Return distinct dates that have photos at a given location, newest first."""
    loc_lower = location.strip().lower()
    if not loc_lower:
        return {"dates": []}
    dates = sorted(
        {item["date"] for item in data
         if not item.get("draft")
         and clean_location(item.get("location", "")).lower() == loc_lower},
        reverse=True,
    )
    return {"dates": dates}


@app.get("/api/folder-times")
def get_folder_times(date: str = Query(...), location: str = Query(...)):
    """Return EXIF capture times (minutes since midnight) for photos in a time-search folder.
    Checks upload-time cache first, then a persisted R2 key, then reads EXIF from each photo."""
    cache_key = (date, location.strip().lower())
    if cache_key in _folder_times_cache:
        return {"times": _folder_times_cache[cache_key]}

    # Check persisted times written during upload (avoids per-photo R2 reads on cold start)
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=_times_r2_key(date, location))
        persisted = json.loads(obj["Body"].read())
        if persisted:
            _folder_times_cache[cache_key] = persisted
            return {"times": persisted}
    except Exception:
        pass

    loc_lower = location.strip().lower()
    paths = [
        item["path"] for item in data
        if not item.get("draft")
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
                obj = s3.get_object(Bucket=R2_BUCKET, Key=key, Range="bytes=0-131071")
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
        _folder_times_cache[cache_key] = times
    return {"times": times}


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
            data = _cached_thumb(key, "admin") or _thumb_on_demand(key, "admin")
            return SR(BytesIO(data), media_type="image/jpeg",
                      headers={"Cache-Control": "private, max-age=86400"})
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        img = Image.open(obj["Body"]).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        buf.seek(0)
        return SR(buf, media_type="image/jpeg",
                  headers={"Cache-Control": "private, max-age=86400"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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

@app.get("/api/frames")
def get_frames():
    try:
        variations = _frame_variations()
    except CheckoutError:
        return JSONResponse(status_code=502, content={"error": "WooCommerce unavailable"})
    frames = []
    for size_idx, label in enumerate(_FRAME_SIZE_LABELS):
        entry = {"size": label, "landscape": None, "portrait": None}
        for orient in ("landscape", "portrait"):
            var_id = FRAME_VARIATION_MAP.get((size_idx, orient))
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


@app.get("/api/shop-prices")
def api_shop_prices():
    """Print, frame, shipping and logo prices for the gallery and checkout pages, from
    the Pricing settings and WooCommerce, so the pages show what checkout charges."""
    pp = _load_print_pricing()
    sizes = pp.get("sizes", list(_DEFAULT_PRINT_SIZES))
    try:
        variations = _frame_variations()
    except CheckoutError:
        variations = {}
    def frame(i, orient):
        v = variations.get(FRAME_VARIATION_MAP.get((i, orient)))
        return v["price"] if v else None
    return {
        "print_sizes": [{"label": s.get("label", ""), "price": float(s.get("price", 0)),
                         "ship_print": float(s.get("ship_print", 8)), "ship_framed": float(s.get("ship_framed", 15))}
                        for s in sizes],
        "free_ship_threshold": int(pp.get("free_ship_threshold", 3)),
        "frame_prices": [{"landscape": frame(i, "landscape"), "portrait": frame(i, "portrait")} for i in range(len(sizes))],
        "logo_fee": float(_load_pricing().get("logo_fee", 2) or 0),
    }


@app.post("/api/admin/frames/prices")
async def update_frame_prices(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    if not WC_KEY or not WC_SECRET:
        return JSONResponse(status_code=503, content={"error": "WooCommerce credentials not configured"})
    body = await request.json()
    # body: list of {variation_id: int, price: str}
    updates = body if isinstance(body, list) else body.get("updates", [])
    results = []
    for u in updates:
        var_id = u.get("variation_id")
        if not var_id:
            continue
        try:
            price_num = float(u.get("price"))
        except (TypeError, ValueError):
            price_num = 0.0
        if price_num <= 0:
            results.append({"variation_id": var_id, "ok": False, "error": "Price must be above $0"})
            continue
        price = f"{price_num:.2f}"
        resp = http_requests.put(
            f"{WC_BASE}/products/{WC_FRAME_PARENT_ID}/variations/{var_id}",
            json={"regular_price": price},
            auth=(WC_KEY, WC_SECRET),
            timeout=15,
        )
        results.append({"variation_id": var_id, "ok": resp.status_code in (200, 201)})
    _frame_prices_cache["data"] = None     # the shop and checkout see the new prices right away
    return {"results": results}


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


@app.get("/api/validate-coupon")
def validate_coupon(code: str = Query(""), email: str = Query("")):
    """Validate a discount code against our own code store."""
    c, problem = _check_coupon(code, email)
    if not c:
        return JSONResponse(status_code=404 if problem == "Invalid coupon code" else 400,
                            content={"valid": False, "error": problem})
    return {
        "valid":          True,
        "code":           c["code"].lower(),
        "discount_type":  c.get("type", "percent"),
        "amount":         float(c.get("amount", 0)),
        "description":    c.get("description", ""),
        "free_shipping":  c.get("free_shipping", False),
        "min_order":      float(c.get("min_order", 0)),
        "min_qty":        int(c.get("min_qty", 0)),
        "applies_to":     c.get("applies_to", "all"),
    }


@app.post("/api/checkout")
async def create_checkout(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    # Pricing lookups and the WooCommerce order request are slow; run them off the
    # main loop so other visitors aren't held up while this checkout is created
    return await run_in_threadpool(_create_checkout, body)


# ── Online checkout pricing ───────────────────────────────────────────────────
# One calculation for everything an online order costs. The checkout page shows it
# (/api/checkout/quote) and /api/checkout charges exactly it, so what customers see
# is what WooCommerce charges. No price, discount or bundle is taken from the browser.

class CheckoutError(ValueError):
    """A cart the server can't price: an unknown item, prices not set up, or
    WooCommerce unavailable. The message is shown to the customer."""


_CATEGORIES = ("digital", "prints", "frames")
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


def _activity_groups(pricing: dict = None) -> dict:
    """Activity group name -> its activities (location names): the studio's categories,
    with explicit pricing["activity_groups"] entries winning."""
    pricing = pricing if pricing is not None else _load_pricing()
    derived = {cat["name"]: cat.get("acts", []) for cat in pricing.get("activity_categories", []) if cat.get("name")}
    return {**derived, **pricing.get("activity_groups", {})}


def _group_in_cart(name: str, acts: list, locations: set, has_portraits: bool) -> bool:
    """Same test as the checkout page used: a folder from one of the group's activities
    is in the cart, or (for a portrait group) portrait photos are."""
    acts_lower = {a.lower() for a in acts}
    if acts_lower & locations:
        return True
    is_portrait_group = "portrait" in name.lower() or any("portrait" in a for a in acts_lower)
    return is_portrait_group and has_portraits


def _bundle_for_cart(locations: set, has_portraits: bool):
    """(active bundle or None, upsell or None). A bundle is active when the cart has
    something from every one of its activity groups; the upsell names what's missing
    when only some are there."""
    pricing = _load_pricing()
    groups = _activity_groups(pricing)
    upsell = None
    for combo in pricing.get("combos", []):
        names, pct = combo.get("activity_groups") or [], combo.get("pct_off")
        if not names or pct is None:
            continue
        present = [n for n in names if _group_in_cart(n, groups.get(n, []), locations, has_portraits)]
        if len(present) == len(names):
            return {"label": combo.get("label") or "Bundle deal", "pct": float(pct)}, None
        if present and upsell is None:
            short = lambda g: re.sub(r"^(Winter|Summer|Fall|Spring|Autumn)\s+", "", g, flags=re.I)
            upsell = {"missing": [short(n) for n in names if n not in present], "pct": float(pct)}
    return None, upsell


def _check_coupon(code: str, email: str = ""):
    """(discount code, None) if `code` can be used now, else (None, reason)."""
    code_clean = (code or "").strip().upper()
    if not code_clean:
        return None, "No code provided"
    c = next((x for x in _load_discount_codes() if x.get("code", "").upper() == code_clean), None)
    if not c:
        return None, "Invalid coupon code"
    if not c.get("active", True):
        return None, "This code is no longer active"

    def _when(value):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    try:
        if c.get("start_date") and now < _when(c["start_date"]):
            return None, "This code is not yet active"
    except Exception:
        pass
    try:
        if c.get("expires") and now > _when(c["expires"]):
            return None, "This code has expired"
    except Exception:
        pass
    limit = c.get("usage_limit")
    if limit and int(c.get("usage_count", 0)) >= int(limit):
        return None, "This code has reached its usage limit"
    per_user = c.get("usage_limit_per_user")
    if per_user and email:
        if sum(1 for u in c.get("uses", []) if u.get("email", "").lower() == email.lower()) >= int(per_user):
            return None, "You have already used this code"
    return c, None


def _coupon_scope(code_obj: dict) -> set:
    at = code_obj.get("applies_to", "all")
    if at in ("digital", "portraits"):
        return {"digital"}
    if at in ("prints", "frames"):
        return {at}
    return set(_CATEGORIES)


def _coupon_item_discount(code_obj: dict, base: float) -> float:
    """What a code takes off items worth `base` (a free-shipping code takes nothing off items)."""
    ctype, amount = code_obj.get("type", "percent"), float(code_obj.get("amount", 0) or 0)
    if ctype == "percent":
        return round(base * amount / 100, 2)
    if ctype == "fixed":
        return round(min(amount, base), 2)
    return 0.0


def _int(value, default: int, lo: int, hi: int, what: str) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise CheckoutError(f"Something in your cart isn't right ({what})")
    if not lo <= n <= hi:
        raise CheckoutError(f"Something in your cart isn't right ({what})")
    return n


def _price_order(body: dict) -> dict:
    """Price an online cart. Returns the WooCommerce order parts (line_items,
    fee_lines, coupon_lines, meta) and the summary the checkout page shows."""
    pricing = _load_pricing()
    logo_fee = float(pricing.get("logo_fee", 2) or 0)
    date = (body.get("date") or "").strip()
    location = body.get("location", "")
    last_name = (body.get("last_name") or "").strip()

    items, rows = [], []          # items become WooCommerce lines; rows are what the page lists

    def add(cat, full, line, row=None, name=None, sub=""):
        if row is None:
            rows.append({"name": name or line["name"], "sub": sub, "full": 0.0, "price": 0.0})
            row = len(rows) - 1
        items.append({"cat": cat, "full": round(full, 2), "line": line, "row": row})
        return row

    # ── Digital albums (priced like the gallery shows them) ──
    albums = [a for a in (body.get("digital_albums") or []) if int(a.get("count") or 0) > 0]
    album_paths, locations = [], set()
    for a in albums:
        count = _int(a.get("count"), 0, 1, 10000, "photo count")
        try:
            price = _server_digital_price(a, date)
        except ValueError as e:
            raise CheckoutError(str(e))
        label = a.get("last_name") or a.get("location") or "Photos"
        add("digital", price, {"product_id": WC_DIGITAL_PRODUCT_ID, "quantity": count,
                               "name": f"Digital Photos — {label}",
                               "meta_data": [{"key": "Photos", "value": f"{count} digital photo{'s' if count != 1 else ''}"}]},
            sub=f"{count} photo{'s' if count != 1 else ''}")
        album_paths += [p for p in (a.get("paths") or []) if p]
        if a.get("location"):
            locations.add(a["location"].strip().lower())

    # ── Portrait poses and single photos ──
    poses = body.get("portrait_poses") or []
    singles = body.get("portrait_singles") or []
    portrait_paths = []
    if poses or singles:
        pp = _load_portrait_pricing()
        pose_tiers = pp.get("pose_tiers", _DEFAULT_PORTRAIT_PRICING["pose_tiers"])
        single_price = float(pp.get("single_photo", _DEFAULT_PORTRAIT_PRICING["single_photo"]))
        pose_price = float(pose_tiers[min(len(poses) - 1, len(pose_tiers) - 1)]) if poses else 0.0
        for pose in poses:
            n = len(pose.get("files", []))
            paths = [p for p in pose.get("paths", []) if p]
            add("digital", pose_price, {
                "product_id": WC_DIGITAL_PRODUCT_ID, "quantity": 1,
                "name": f"Portrait {pose.get('pose_label', 'Pose')} ({n} photo{'s' if n != 1 else ''}) — {pose.get('family', last_name or location)}",
                "meta_data": [{"key": "Photos", "value": f"{n} digital photo{'s' if n != 1 else ''}"},
                              {"key": "_photo_paths", "value": "|".join(paths)}]})
            portrait_paths += paths
        for single in singles:
            add("digital", single_price, {
                "product_id": WC_DIGITAL_PRODUCT_ID, "quantity": 1,
                "name": f"Portrait Single Photo — {single.get('family', last_name or location)}",
                "meta_data": [{"key": "Photos", "value": single.get("filename", "Photo")},
                              {"key": "_photo_paths", "value": single.get("path", "")}]},
                sub=single.get("filename", ""))
            if single.get("path"):
                portrait_paths.append(single["path"])

    bought_digital = set(album_paths) | set(portrait_paths) | {p for p in (body.get("digital_paths") or []) if p}

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
    for p in prints:
        size_idx = _int(p.get("size_idx"), 0, 0, len(sizes) - 1, "print size")
        size_label = p.get("size") or sizes[size_idx].get("label", "")
        half = bool(p.get("path")) and p["path"] in bought_digital
        logo = bool(p.get("logo"))
        price = float(sizes[size_idx].get("price", 0)) / (2 if half else 1) + (logo_fee if logo else 0)
        meta = []
        if half:
            meta.append({"key": "Price", "value": "50% off (digital photo also purchased)"})
        if logo:
            meta.append({"key": "Logo", "value": "Big Sky Resort logo"})
        frame = p.get("frame") if p.get("frame") not in ("", None, "No Frame") else None
        row = add("prints", price, {"product_id": WC_PRINT_PRODUCT_ID, "quantity": 1,
                                    "name": f"{size_label} Print — {p.get('filename', '')}" + (" + Big Sky Resort logo" if logo else ""),
                                    "meta_data": meta},
                  name=f"{size_label} Print" + (f" + {frame}" if frame else "") + (" + Logo" if logo else ""),
                  sub=p.get("filename", ""))
        if p.get("location"):
            locations.add(str(p["location"]).strip().lower())
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
        vid = _int(fr.get("variation_id"), 0, 1, 10 ** 9, "frame")
        if vid not in _FRAME_VARIATION_INFO:
            raise CheckoutError("One of the frames in your cart isn't available right now")
        qty = _int(fr.get("quantity"), 1, 1, 20, "frame quantity")
        size_idx, orientation = _FRAME_VARIATION_INFO[vid]
        label = _FRAME_SIZE_LABELS[size_idx] if size_idx < len(_FRAME_SIZE_LABELS) else ""
        add("frames", _frame_price(vid) * qty, {"product_id": WC_FRAME_PARENT_ID, "variation_id": vid, "quantity": qty,
                                                "name": f"Frame — {label} ({orientation.capitalize()})"},
            name=f"{label} Frame ({orientation}) ×{qty}")
        frame_ship_sizes += [size_idx] * qty

    # ── Shipping ──
    print_ship = [s.get("ship_print", 8) for s in sizes]
    frame_ship = [s.get("ship_framed", 15) for s in sizes]
    framed = [p for p in prints if p.get("frame") not in ("", None, "No Frame")]
    unframed = [p for p in prints if p.get("frame") in ("", None, "No Frame")]
    frame_rates = sorted([frame_ship[int(p.get("size_idx", 0))] for p in framed] +
                         [frame_ship[i] for i in frame_ship_sizes], reverse=True)
    free_threshold = _load_print_pricing().get("free_ship_threshold", 3)
    shipping, free_shipping = 0.0, False
    if frame_rates:
        shipping = round(frame_rates[0] + sum(r * 0.5 for r in frame_rates[1:]), 2)
    elif unframed:
        if len(unframed) >= free_threshold:
            free_shipping = True
        else:
            shipping = float(max(print_ship[int(p.get("size_idx", 0))] for p in unframed))

    # ── Discounts: a bundle takes its % off every item, except items a code takes more off ──
    def worth(cats, key="full"):
        return round(sum(it[key] for it in items if it["cat"] in cats), 2)

    bundle, upsell = _bundle_for_cart(locations, bool(poses or singles))
    coupon, coupon_note = None, ""
    code_in = (body.get("coupon_code") or "").strip()
    if code_in:
        coupon, coupon_note = _check_coupon(code_in, body.get("email", ""))
    bundle_cats = set(_CATEGORIES) if bundle else set()
    if bundle and coupon:
        scope = _coupon_scope(coupon) & {it["cat"] for it in items}
        code_off = _coupon_item_discount(coupon, worth(scope))
        if code_off > 0:
            if code_off > round(worth(scope) * bundle["pct"] / 100, 2):
                bundle_cats -= scope            # the code gives these items more
            else:
                coupon_note = f"The {bundle['label']} ({bundle['pct']:g}% off) saves you more than this code"
                coupon = None
    for it in items:
        pct = bundle["pct"] if it["cat"] in bundle_cats else 0
        it["price"] = round(it["full"] * (1 - pct / 100), 2)
        rows[it["row"]]["full"] = round(rows[it["row"]]["full"] + it["full"], 2)
        rows[it["row"]]["price"] = round(rows[it["row"]]["price"] + it["price"], 2)
    bundle_savings = round(worth(_CATEGORIES) - worth(_CATEGORIES, "price"), 2)
    coupon_off = _coupon_item_discount(coupon, worth(_coupon_scope(coupon), "price")) if coupon else 0.0
    subtotal = worth(_CATEGORIES, "price")

    # ── WooCommerce order parts ──
    line_items = [{**it["line"], "subtotal": str(it["price"]), "total": str(it["price"])} for it in items]
    fee_lines = [{"name": "Shipping", "total": str(shipping)}] if shipping else []
    filenames = list(body.get("digital_filenames") or []) + [s.get("filename", "") for s in singles]
    meta = [
        {"key": "_photo_location", "value": location},
        {"key": "_photo_date",     "value": date},
        {"key": "_photo_files",    "value": ", ".join(filenames)},
        {"key": "_photo_paths",    "value": "|".join(list(body.get("digital_paths") or []) + portrait_paths)},
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


@app.post("/api/checkout/quote")
def checkout_quote(body: dict = Body(...)):
    """The checkout page's totals: exactly what /api/checkout will charge."""
    try:
        return _price_order(body)["summary"]
    except CheckoutError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


def _create_checkout(body: dict):
    try:
        priced = _price_order(body)
    except CheckoutError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
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
        return {"checkout_url": pay_url, "total": priced["summary"]["total"]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/pos-checkout")
async def pos_checkout(request: Request):
    """Record a completed in-person (Square) sale and deliver the download link."""
    try:
        body = await request.json()
        return await run_in_threadpool(_record_pos_sale, body)
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

    # Collect photo_paths from action_photos items; fall back to legacy fields
    photo_paths, filenames, location, photo_date = [], [], "", ""
    for item in items:
        if item.get("category") == "action_photos":
            photo_paths.extend(item.get("photo_paths", []))
            filenames.extend(item.get("filenames", []))
            if not location:
                location = item.get("location", "")
            if not photo_date:
                photo_date = item.get("date", "")
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
    else:
        title = "In-Person Sale"

    order_id = f"pos-{token[:8]}"
    order_record = {
        "id":                order_id,
        "date":              now.strftime("%Y-%m-%d"),
        "created":           now.isoformat(),
        "source":            "pos",
        "photographer_pin":  pin,
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
        "package_token":     token if photo_paths else "",
        "items":             items,
        **square_check,
    }
    _store_pos_order(order_record)
    if square_check:
        _mark_square_id_used(square_id, order_id)
    if square_check and not square_check["square_fee_final"]:
        # Square fills the card fee in shortly after the payment; record it then
        fee_check = threading.Timer(30, _refresh_square_fee, args=(order_record,))
        fee_check.daemon = True
        fee_check.start()

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
    if photo_paths:
        # The package was saved above, so this link works the moment the email arrives
        download_url = f"{SITE_URL}/package/{token}"
        expire_str   = (now + timedelta(days=expire_days_req)).strftime("%B %d, %Y")
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


@app.post("/api/pos/square-pending")
def pos_square_pending(request: Request, body: dict = Body(...)):
    """Save a sale just before the POS hands it to Square."""
    state = str(body.get("state") or "")
    sale = body.get("sale") or {}
    if not _POS_STATE_RE.match(state):
        return JSONResponse(status_code=400, content={"error": "Invalid sale reference"})
    photog = _pos_pin_photographer(request)
    if not photog:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    if not (sale.get("email") or "").strip():
        return JSONResponse(status_code=400, content={"error": "Customer email required"})
    existing = _load_pos_pending(state)
    if existing and existing.get("status") == "completed":
        return JSONResponse(status_code=409, content={"error": "This sale was already completed"})
    sale = {**sale, "pin": request.headers.get("X-POS-PIN", "").strip()}
    sale.pop("square_payment_id", None)
    _save_pos_pending(state, {"state": state, "status": "pending", "sale": sale,
                              "photographer_id": photog.get("id", ""),
                              "created": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


@app.post("/api/pos/square-complete")
def pos_square_complete(body: dict = Body(...)):
    """Finish a sale when Square returns. No PIN is needed: the state is a random id
    only the POS and Square know, and the payment is verified with Square."""
    state = str(body.get("state") or "")
    transaction_id = str(body.get("transaction_id") or "")
    if not _POS_STATE_RE.match(state):
        return JSONResponse(status_code=404, content={"error": "This sale wasn't found — check the Square app"})
    # If Safari and the POS app both finish the same sale, the second waits and
    # then gets the first one's result instead of recording it again
    with _sale_locks.hold("state:" + state):
        return _complete_pending_sale(state, transaction_id)


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


@app.get("/api/pos/square-pending/{state}")
def pos_square_pending_status(state: str, request: Request):
    """Lets the Home Screen POS learn that its sale was finished in Safari."""
    if not _pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    pending = _load_pos_pending(state)
    if not pending:
        return JSONResponse(status_code=404, content={"status": "unknown"})
    return {"status": pending.get("status"), "result": pending.get("result")}


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


@app.post("/api/pos/square-prepare")
def pos_square_prepare(request: Request, body: dict = Body(...)):
    """Everything the POS needs to open the Square app for a sale: app ID + customer id."""
    if not _pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    if not SQUARE_APP_ID:
        return JSONResponse(status_code=503, content={"error": "Square isn't set up (SQUARE_APP_ID is missing)"})
    if not SQUARE_ACCESS_TOKEN:
        return JSONResponse(status_code=503, content={
            "error": "Square isn't set up (SQUARE_ACCESS_TOKEN is missing) — payments couldn't be confirmed"})
    customer_id, customer_problem = None, None
    try:
        customer_id, customer_problem = _square_customer_id(body.get("customer") or {})
    except SquareTokenRejected:
        return JSONResponse(status_code=503, content={
            "error": "Square rejected the access token, so payments couldn't be confirmed. "
                     "Check it in Dashboard > Settings > Test Square connection before charging."})
    except Exception as e:
        print(f"Square customer lookup failed: {e}")
        customer_problem = "Couldn't reach Square to add the customer"
    return {"client_id": SQUARE_APP_ID, "customer_id": customer_id, "customer_problem": customer_problem}


@app.get("/api/admin/square/test")
def admin_square_test(request: Request):
    """Studio check: does the Square access token work? Never returns the token."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    result = {"app_id_set": bool(SQUARE_APP_ID), "token_set": bool(SQUARE_ACCESS_TOKEN),
              "location_id_set": bool(SQUARE_LOCATION_ID)}
    if not SQUARE_ACCESS_TOKEN:
        return {**result, "ok": False, "error": "SQUARE_ACCESS_TOKEN is missing"}
    try:
        r = http_requests.get(f"{SQUARE_API_BASE}/locations", headers=_square_headers(), timeout=8)
    except Exception as e:
        return {**result, "ok": False, "error": f"Could not reach Square: {e}"}
    if not r.ok:
        reason = "token rejected — check it's the Production access token" if r.status_code == 401 else r.text[:200]
        return {**result, "ok": False, "error": f"Square answered {r.status_code}: {reason}"}
    locations = r.json().get("locations") or []
    return {**result, "ok": True,
            "locations": [l.get("name", "") for l in locations],
            "location_id_matches": any(l.get("id") == SQUARE_LOCATION_ID for l in locations) if SQUARE_LOCATION_ID else None}


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

@app.get("/api/pos-theme")
def api_pos_theme():
    cfg = _load_settings()
    c = cfg.get("frontend", {})
    return {
        "accent":  _safe_color(c.get("accent",   "#F5C430"), "#F5C430"),
        "bg":      _safe_color(c.get("page_bg",  "#093A52"), "#093A52"),
        "surface": _safe_color(c.get("panel_bg", "#093A53"), "#093A53"),
    }

@app.get("/api/pos-products")
def api_pos_products():
    return _load_pos_products()

@app.post("/api/admin/pos-products")
async def api_save_pos_products(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body = await request.json()
    _save_pos_products(body)
    return {"ok": True}

@app.get("/api/pos-recent-sales")
def api_pos_recent_sales(request: Request, limit: int = Query(50)):
    if not (_admin_authed(request) or _pos_pin_photographer(request)):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    orders = _fetch_pos_orders()
    return {"orders": orders[:limit]}

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

@app.get("/api/pos-customers")
def api_pos_customers(request: Request, q: str = Query("")):
    """Search POS customer directory. Returns up to 60 matches.
    Admin (studio) may list everything; the POS needs a PIN and a search term."""
    is_admin = _admin_authed(request)
    if not is_admin:
        if not _pos_pin_photographer(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        if len(q.strip()) < 2:
            return {"customers": [], "total": 0, "search_required": True}
    customers = _load_customers()
    if not q:
        return {"customers": customers[:60], "total": len(customers)}
    results = [c for c in customers if _customer_name_matches(c, q)]
    return {"customers": results[:60], "total": len(results)}

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

@app.get("/api/admin/customers")
def api_admin_customers(request: Request, q: str = Query(""),
                        offset: int = Query(0), limit: int = Query(100)):
    """Paged, searchable customer list for the studio CRM."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    customers = _load_customers()
    if q.strip():
        customers = [c for c in customers if _customer_admin_matches(c, q)]
    customers = sorted(customers, key=lambda c: ((c.get("last") or "").lower(),
                                                 (c.get("first") or "").lower()))
    offset = max(0, offset)
    limit  = max(1, min(limit, 500))
    return {"customers": customers[offset:offset + limit], "total": len(customers),
            "offset": offset, "limit": limit}

@app.get("/api/admin/customers/{cid}")
def api_admin_customer_get(cid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    c = next((c for c in _load_customers() if c.get("id") == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Customer not found"})
    return {"customer": c}

@app.post("/api/admin/customers")
async def api_admin_customer_create(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    fields = _clean_customer_fields(await request.json())
    if not (fields.get("first") or fields.get("last") or fields.get("email")):
        return JSONResponse(status_code=400, content={"error": "Name or email required"})
    with _customers_lock:
        customers = _load_customers()
        dup = _customer_by_email(customers, fields.get("email", ""))
        if dup:
            return JSONResponse(status_code=409, content={"error": "A customer with that email already exists",
                                                          "id": dup.get("id")})
        c = {"id": _new_customer_id(), **fields, "source": "studio",
             "created": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        customers.append(c)
        _save_customers(customers)
    return {"customer": c}

@app.put("/api/admin/customers/{cid}")
async def api_admin_customer_update(cid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    fields = _clean_customer_fields(await request.json())
    with _customers_lock:
        customers = _load_customers()
        c = next((c for c in customers if c.get("id") == cid), None)
        if not c:
            return JSONResponse(status_code=404, content={"error": "Customer not found"})
        dup = _customer_by_email(customers, fields.get("email", ""))
        if dup and dup is not c:
            return JSONResponse(status_code=409, content={"error": "Another customer already has that email"})
        c.update(fields)
        _save_customers(customers)
    return {"customer": c}

@app.delete("/api/admin/customers/{cid}")
def api_admin_customer_delete(cid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    with _customers_lock:
        customers = _load_customers()
        remaining = [c for c in customers if c.get("id") != cid]
        if len(remaining) == len(customers):
            return JSONResponse(status_code=404, content={"error": "Customer not found"})
        _save_customers(remaining)
    return {"deleted": True}

@app.get("/api/admin/customer/{email}/history")
def api_customer_history(email: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    email = email.lower().strip()
    wc_orders = []
    if WC_KEY and WC_SECRET:
        try:
            # Search by email covers both guest and registered WC customers
            ord_resp = http_requests.get(
                f"{WC_BASE}/orders",
                params={"search": email, "per_page": 100, "orderby": "date", "order": "desc", "status": "any"},
                auth=(WC_KEY, WC_SECRET),
                timeout=15,
            )
            if ord_resp.ok:
                wc_orders = [
                    o for o in ord_resp.json()
                    if (o.get("billing", {}).get("email") or "").lower() == email
                ]
        except Exception as e:
            print(f"WC order fetch error for {email}: {e}")

    orders_out = []
    for o in wc_orders:
        order_id = o.get("id")
        date_str = (o.get("date_created") or "")[:10]
        total = float(o.get("total") or 0)
        items = [li.get("name", "") for li in o.get("line_items", [])][:4]
        gallery_url = None
        gallery_expired = False
        try:
            token, rec, exp = _get_best_token_for_order(str(order_id))
            if token and exp:
                gallery_url = f"/download/{token}"
                now = datetime.utcnow()
                exp_naive = exp.replace(tzinfo=None) if hasattr(exp, "tzinfo") and exp.tzinfo else exp
                gallery_expired = exp_naive < now
        except Exception:
            pass
        orders_out.append({
            "id": order_id,
            "date": date_str,
            "total": total,
            "status": o.get("status", ""),
            "items": items,
            "gallery_url": gallery_url,
            "gallery_expired": gallery_expired,
            "source": "woocommerce",
        })

    # POS orders filtered by email
    pos_orders_out = []
    for o in _fetch_pos_orders():
        if (o.get("email") or "").lower() == email:
            pos_orders_out.append({
                "id": o.get("id"),
                "date": (o.get("date") or o.get("created", ""))[:10],
                "total": float(o.get("total") or 0),
                "location": o.get("location", ""),
                "items": [it.get("label", "") for it in (o.get("items") or [])],
                "source": "pos",
            })

    all_totals = [o["total"] for o in orders_out + pos_orders_out]
    all_dates  = sorted([o["date"] for o in orders_out + pos_orders_out if o.get("date")], reverse=True)
    stats = {
        "total_spend": round(sum(all_totals), 2),
        "order_count": len(all_totals),
        "last_purchase": all_dates[0] if all_dates else None,
    }
    return {"stats": stats, "orders": orders_out, "pos_orders": pos_orders_out}


def _build_followup_email(email: str, body: dict):
    """Build a follow-up email for one customer.
    Returns (ftype, subject, html, text), or a JSONResponse describing what's missing."""
    ftype = body.get("type")
    if ftype not in ("review", "discount", "custom"):
        return JSONResponse(status_code=400, content={"error": "type must be review, discount, or custom"})

    # Load customer for personalisation
    customer = _customer_by_email(_load_customers(), email)
    first_name = (customer.get("first") or "").strip() if customer else ""
    greeting   = f"Hi {first_name}," if first_name else "Hi,"

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
        text_inner = f"{greeting}\n\n{message}"

    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f9f9f9;font-family:Arial,sans-serif">
<div style="max-width:560px;margin:0 auto;padding:2rem 1rem">
  <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:2rem">
    <p style="margin:0 0 1.5rem;font-size:.78rem;color:#999;text-transform:uppercase;letter-spacing:.5px">Crystal Images</p>
    {html_inner}
    <hr style="border:none;border-top:1px solid #eee;margin:1.5rem 0 1rem">
    <p style="margin:0;font-size:.75rem;color:#aaa">Questions? <a href="mailto:info@bigskyphotos.com" style="color:#1a73e8">info@bigskyphotos.com</a></p>
  </div>
</div></body></html>"""

    return ftype, subject, html, text_inner


@app.post("/api/admin/customer/{email}/followup/preview")
def api_customer_followup_preview(email: str, request: Request, body: dict = Body(...)):
    """The exact email a follow-up would send — nothing is sent."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    built = _build_followup_email(email, body)
    if isinstance(built, JSONResponse):
        return built
    _, subject, html, _ = built
    return {"to": email, "subject": subject, "html": html}


@app.post("/api/admin/customer/{email}/followup")
def api_customer_followup(email: str, request: Request, body: dict = Body(...)):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not RESEND_API_KEY:
        return JSONResponse(status_code=503, content={"error": "Email not configured — RESEND_API_KEY is missing"})
    built = _build_followup_email(email, body)
    if isinstance(built, JSONResponse):
        return built
    ftype, subject, html, text_inner = built

    try:
        r = http_requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [email], "subject": subject, "html": html, "text": text_inner},
            timeout=10,
        )
        if not r.ok:
            return JSONResponse(status_code=502, content={"error": f"Send failed: {r.text}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Record the follow-up on the customer record
    with _customers_lock:
        customers = _load_customers()
        customer  = _customer_by_email(customers, email)
        if customer:
            customer.setdefault("followups", []).append({
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "type": ftype,
                "subject": subject,
            })
            _save_customers(customers)

    return {"ok": True}


@app.get("/api/admin/customers/filtered")
def api_customers_filtered(request: Request):
    """Return customer emails + basic info matching date range and/or product type filters."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    date_from  = request.query_params.get("date_from", "")   # YYYY-MM-DD
    date_to    = request.query_params.get("date_to", "")     # YYYY-MM-DD
    types_raw  = request.query_params.get("types", "")       # comma-separated
    types      = {t.strip().lower() for t in types_raw.split(",") if t.strip()} if types_raw else set()

    want_pos     = not types or "in-person" in types
    want_digital = not types or "digital"  in types
    want_prints  = not types or "prints"   in types
    want_frames  = not types or "frames"   in types
    want_wc      = want_digital or want_prints or want_frames

    # Load customer directory for name/tag enrichment
    cust_by_email = {(c.get("email") or "").lower(): c for c in _load_customers()}

    results: dict = {}  # email -> {email, name, sources: set, order_count, total_spend}

    def _upsert(email_raw: str, name: str, source: str, amount: float):
        key = email_raw.lower()
        if key not in results:
            cust = cust_by_email.get(key, {})
            results[key] = {
                "id": cust.get("id"),
                "email": email_raw,
                "name": name or f"{cust.get('first','')} {cust.get('last','')}".strip() or email_raw,
                "tags": cust.get("tags", []),
                "sources": set(),
                "order_count": 0,
                "total_spend": 0.0,
            }
        results[key]["sources"].add(source)
        results[key]["order_count"] += 1
        results[key]["total_spend"] += amount

    # ── POS orders ────────────────────────────────────────────────────────────
    if want_pos:
        for order in _fetch_pos_orders():
            odate = (order.get("date") or "")[:10]
            if date_from and odate < date_from:
                continue
            if date_to and odate > date_to:
                continue
            email = (order.get("email") or "").strip()
            if not email:
                continue
            first = order.get("first", "")
            last  = order.get("last", "")
            name  = f"{first} {last}".strip()
            total = float(order.get("net") or order.get("total") or 0)
            _upsert(email, name, "pos", total)

    # ── WooCommerce orders ─────────────────────────────────────────────────────
    if want_wc and WC_KEY and WC_SECRET:
        try:
            params: dict = {"per_page": 100, "page": 1, "status": "completed"}
            if date_from:
                params["after"] = f"{date_from}T00:00:00"
            if date_to:
                params["before"] = f"{date_to}T23:59:59"
            while True:
                resp = http_requests.get(
                    f"{WC_BASE}/orders", params=params,
                    auth=(WC_KEY, WC_SECRET), timeout=20,
                )
                if not resp.ok:
                    break
                orders = resp.json()
                if not orders:
                    break
                for o in orders:
                    email = (o.get("billing") or {}).get("email", "").strip()
                    if not email:
                        continue
                    name = f"{(o.get('billing') or {}).get('first_name','')} {(o.get('billing') or {}).get('last_name','')}".strip()
                    total = float(o.get("total") or 0)
                    line_items = o.get("line_items", [])
                    def _cat(item_name: str) -> str:
                        n = item_name.lower()
                        if "frame" in n:
                            return "frames"
                        if "print" in n or "4x6" in n or "5x7" in n or "8x10" in n or "11x14" in n:
                            return "prints"
                        return "digital"
                    for li in line_items:
                        cat = _cat(li.get("name", ""))
                        if cat == "digital"  and not want_digital:
                            continue
                        if cat == "prints"   and not want_prints:
                            continue
                        if cat == "frames"   and not want_frames:
                            continue
                        _upsert(email, name, cat, total / max(len(line_items), 1))
                        break  # count order once per type match
                if len(orders) < 100:
                    break
                params["page"] += 1
        except Exception as e:
            print(f"WC filtered query error: {e}")

    out = []
    for r in results.values():
        out.append({
            "id":          r["id"],
            "email":       r["email"],
            "name":        r["name"],
            "tags":        r["tags"],
            "sources":     list(r["sources"]),
            "order_count": r["order_count"],
            "total_spend": round(r["total_spend"], 2),
        })
    out.sort(key=lambda x: x["name"].lower())
    return {"customers": out, "count": len(out)}


@app.post("/api/admin/customers/bulk-followup")
def api_bulk_followup(request: Request, body: dict = Body(...)):
    """Send a follow-up email to multiple customers using a saved template."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not RESEND_API_KEY:
        return JSONResponse(status_code=503, content={"error": "RESEND_API_KEY not configured"})

    emails      = body.get("emails", [])
    template_id = (body.get("template_id") or "").strip()
    extra_vars  = body.get("variables", {})   # e.g. {review_url, discount_code}

    if not emails:
        return JSONResponse(status_code=400, content={"error": "emails is required"})
    if not template_id:
        return JSONResponse(status_code=400, content={"error": "template_id is required"})

    templates = _load_email_templates()
    if template_id not in templates:
        return JSONResponse(status_code=404, content={"error": "Template not found"})
    tmpl = templates[template_id]
    review_url = (extra_vars.get("review_url") or tmpl.get("review_url") or "").strip()
    if "{{review_button}}" in (tmpl.get("body") or "") and not review_url:
        return JSONResponse(status_code=400, content={
            "error": f"Add your Google review link to the '{tmpl.get('name', template_id)}' template in Settings > Email Templates"})

    customers = _load_customers()
    code_obj_bulk = None
    if extra_vars.get("discount_code"):
        _code = str(extra_vars["discount_code"]).strip().upper()
        code_obj_bulk = next((c for c in _load_discount_codes() if c.get("code", "").upper() == _code), None)

    results = []
    for email in emails:
        cust = _customer_by_email(customers, email)
        first_name = (cust.get("first") or "").strip() if cust else ""

        plain_vars = {"first_name": first_name or "there", **{k: str(v) for k, v in extra_vars.items()}}

        # Build html_blocks for known action tokens
        html_blocks: dict = {}
        if review_url:
            html_blocks["review_button"] = _review_button_html(review_url)
            plain_vars["review_button"] = f"Leave us a review: {review_url}"
        if "discount_code" in extra_vars:
            code   = str(extra_vars["discount_code"]).upper()
            detail = _discount_detail(code_obj_bulk) if code_obj_bulk else ""
            html_blocks["code_box"] = _code_box_html(code, detail)
            plain_vars.setdefault("code", code)
            plain_vars.setdefault("discount_amount", detail)
            plain_vars["code_box"] = (f"Your discount code: {code}" + (f" ({detail})" if detail else "")
                                      + f"\nShop now: {SITE_URL}")

        subject = tmpl["subject"]
        for k, v in plain_vars.items():
            subject = subject.replace(f"{{{{{k}}}}}", str(v))
        body_html, body_text = _render_email_body(tmpl["body"], plain_vars, html_blocks)
        html = _wrap_email_html(body_html)

        ok = False
        err = ""
        try:
            r = http_requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": RESEND_FROM, "to": [email], "subject": subject, "html": html, "text": body_text},
                timeout=10,
            )
            ok = r.ok
            if not ok:
                err = r.text[:200]
        except Exception as exc:
            err = str(exc)

        results.append({"email": email, "ok": ok, "error": err, "subject": subject})

    # Record follow-ups on customer records (one save, after all sends)
    if any(r["ok"] for r in results):
        try:
            with _customers_lock:
                customers = _load_customers()
                for r in results:
                    cust = _customer_by_email(customers, r["email"]) if r["ok"] else None
                    if cust:
                        cust.setdefault("followups", []).append({
                            "date": datetime.utcnow().strftime("%Y-%m-%d"),
                            "type": "bulk",
                            "subject": r["subject"],
                        })
                _save_customers(customers)
        except Exception as e:
            print(f"bulk-followup customer save error: {e}")
    for r in results:
        r.pop("subject", None)

    sent = sum(1 for r in results if r["ok"])
    return {"ok": True, "sent": sent, "total": len(results), "results": results}


@app.post("/api/pos-resend")
def api_pos_resend(request: Request, body: dict = Body(...)):
    """Recent Sales: send the customer their receipt again (the same single email
    they got at checkout, with the download button when photos were bought)."""
    pin = body.get("pin", "").strip()
    if _pin_locked_out(request):
        return JSONResponse(status_code=429, content={"error": "Too many wrong PINs — wait 10 minutes"})
    if not _auth_photographer(pin):
        _record_pin_failure(request)
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    order = _find_pos_order(str(body.get("order_id") or ""), str(body.get("date") or ""),
                            str(body.get("token") or ""))
    if not order:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    note = _resend_pos_receipt(order)
    return {"ok": True, "sent_to": [order.get("email")], "note": note}


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
    square = None
    if order.get("square_verified"):
        square = {k: order.get(k) for k in ("square_paid", "square_tax", "square_tip")}
    _send_pos_receipt_email(
        to_email=order.get("email", ""), customer_name=order.get("name", ""), order_id=order.get("id", ""),
        items=order.get("items") or [], total=float(order.get("total") or 0),
        coupon_code=order.get("coupon_code", ""), coupon_discount=float(order.get("coupon_discount") or 0),
        download_url=download_url, expire_str=expire_str, square=square,
        comp=(order.get("square_payment_id") == "COMP"))
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

@app.post("/webhook")
async def wc_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-WC-Webhook-Signature", "")

    # Only WooCommerce, which signs each message with the shared secret, may tell us an
    # order was paid: an unsigned "paid" message would get photos sent for free. Without
    # the secret nothing can be checked, so refuse (loudly) rather than trust anything.
    if not WC_WEBHOOK_SECRET:
        print("ORDER WEBHOOK REFUSED: WC_WEBHOOK_SECRET is not set on the server — "
              "paid online orders are NOT getting their download emails until it is restored")
        return Response(status_code=503)
    expected = base64.b64encode(
        hmac.new(WC_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(expected, sig):
        return Response(status_code=401)

    try:
        order = json.loads(body)
    except Exception:
        return Response(status_code=400)
    return await run_in_threadpool(_handle_paid_wc_order, order)


def _handle_paid_wc_order(order: dict):
    """A paid online order: create the download link and have WooCommerce email it."""
    # Act on processing (manual/physical orders) or completed (auto-completed digital orders)
    if order.get("status") not in ("processing", "completed"):
        return Response(status_code=200)
    # WooCommerce calls this when an order is paid and again when it's completed
    # (often seconds apart). One link and one email per order: the second call waits
    # for the first, then sees the order already has a valid link.
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
STRIPE_RATE     = 0.029
STRIPE_FIXED    = 0.30

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
            _admin_auth_cache["data"] = d
        return _admin_auth_cache["data"]


def _save_admin_auth(d: dict):
    """Drop anything expired, then save (caller holds _admin_auth_lock)."""
    now = _now_iso()
    d["sessions"] = {k: v for k, v in d["sessions"].items() if v.get("expires", "") > now}
    d["devices"] = {k: v for k, v in d["devices"].items() if v.get("expires", "") > now}
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


ADMIN_CODE_HTML = ADMIN_LOGIN_HTML.replace("input[type=password]", "input.code").replace(
    """  <form method="post" action="/admin/login">
    <input type="hidden" name="next" value="{next}" />
    <input type="password" name="password" placeholder="Password" autofocus />
    <button type="submit">Sign In</button>
    {error}
  </form>""",
    """  <p style="color:rgba(255,255,255,.7);font-size:.9rem;text-align:center;margin:-.5rem 0 1.2rem;line-height:1.5">
    New device: we emailed a 6-digit code to {email}</p>
  <form method="post" action="/admin/verify">
    <input type="hidden" name="challenge" value="{challenge}" />
    <input class="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
           placeholder="6-digit code" autofocus style="text-align:center;letter-spacing:.4rem;font-size:1.3rem" />
    <button type="submit">Verify</button>
    {error}
  </form>
  <p style="text-align:center;margin-top:1rem"><a href="/admin" style="color:rgba(255,255,255,.45);font-size:.8rem">Start over</a></p>""")


def _safe_next(url) -> str:
    """Where to go after signing in: only a page on this site. A link like
    /admin?next=https://elsewhere could otherwise send you to another site."""
    url = str(url or "")
    if (not url.startswith("/") or url.startswith("//") or url.startswith("/\\")
            or any(c in url for c in "\r\n\t\\") or len(url) > 500):
        return "/admin/dashboard"
    return url


@app.get("/admin", response_class=HTMLResponse)
def admin_get(request: Request):
    if not _admin_authed(request):
        next_url = _safe_next(request.query_params.get("next"))
        return HTMLResponse(ADMIN_LOGIN_HTML.format(error="", next=_html_escape(next_url)))
    return RedirectResponse("/admin/dashboard")

@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    pw   = form.get("password", "")
    next_url = _safe_next(form.get("next"))
    def again(msg, status=401):
        return HTMLResponse(ADMIN_LOGIN_HTML.format(error=f'<p class="err">{msg}</p>',
                                                    next=_html_escape(next_url)), status_code=status)
    if _admin_pw_fail_limiter.count(_client_ip(request)) >= _admin_pw_fail_limiter.max_calls:
        return again("Too many wrong passwords — wait 10 minutes.", 429)
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw, ADMIN_PASSWORD):
        _admin_pw_fail_limiter.is_allowed(_client_ip(request))
        return again("Incorrect password.")
    info = _request_info(request)
    device_hash = _device_approved(request)
    if device_hash or not ADMIN_LOGIN_CODES_ON:
        return _signed_in_response(next_url, device_hash=device_hash or "", info=info)
    # A new device: email a code before signing in
    challenge = secrets.token_urlsafe(16)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    now = _time.time()
    for k in [k for k, v in _login_challenges.items() if v["expires"] < now]:
        _login_challenges.pop(k, None)
    _login_challenges[challenge] = {"code": _token_hash(code), "expires": now + LOGIN_CODE_MINUTES * 60,
                                    "tries": 0, "next": next_url, "info": info}
    if not _send_login_code_email(code, info):
        _login_challenges.pop(challenge, None)
        return again("Couldn't email the sign-in code — try again in a minute.", 503)
    return HTMLResponse(ADMIN_CODE_HTML.format(email=_html_escape(_masked_email(OWNER_EMAIL)),
                                               challenge=challenge, error=""))


@app.post("/admin/verify")
async def admin_verify(request: Request):
    form = await request.form()
    challenge = str(form.get("challenge") or "")
    code = str(form.get("code") or "").strip()
    pending = _login_challenges.get(challenge)
    if not pending or pending["expires"] < _time.time():
        _login_challenges.pop(challenge, None)
        return HTMLResponse(ADMIN_LOGIN_HTML.format(
            error='<p class="err">That code has expired — sign in again.</p>', next="/admin/dashboard"), status_code=401)
    pending["tries"] += 1
    if not hmac.compare_digest(_token_hash(code), pending["code"]):
        if pending["tries"] >= LOGIN_CODE_TRIES:
            _login_challenges.pop(challenge, None)
            return HTMLResponse(ADMIN_LOGIN_HTML.format(
                error='<p class="err">Too many wrong codes — sign in again.</p>', next="/admin/dashboard"), status_code=401)
        return HTMLResponse(ADMIN_CODE_HTML.format(email=_html_escape(_masked_email(OWNER_EMAIL)),
                                                   challenge=challenge, error='<p class="err">Incorrect code.</p>'),
                            status_code=401)
    _login_challenges.pop(challenge, None)
    # Approve this device for 90 days, then sign in
    device_token = secrets.token_urlsafe(32)
    device_hash = _token_hash(device_token)
    d = _load_admin_auth()
    with _admin_auth_lock:
        d["devices"][device_hash] = {
            "approved": _now_iso(), "describe": pending["info"].get("device", ""), "ip": pending["info"].get("ip", ""),
            "expires": (datetime.now(timezone.utc) + timedelta(days=ADMIN_DEVICE_DAYS)).isoformat()}
        _save_admin_auth(d)
    return _signed_in_response(pending["next"], device_token, device_hash, pending["info"])

@app.get("/admin/logout")
def admin_logout(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    if token:
        d = _load_admin_auth()
        with _admin_auth_lock:
            if d["sessions"].pop(_token_hash(token), None) is not None:
                _save_admin_auth(d)
    resp = RedirectResponse("/admin")
    resp.delete_cookie(ADMIN_COOKIE)        # this device stays approved: next time, just the password
    resp.delete_cookie(OWNER_COOKIE)     # signing out also locks the PIN-protected pages
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

def _fetch_pos_orders(after: str = None, before: str = None) -> list:
    orders = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="pos_orders/"):
            for obj in page.get("Contents", []):
                try:
                    data = s3.get_object(Bucket=R2_BUCKET, Key=obj["Key"])
                    orders.append(json.loads(data["Body"].read()))
                except Exception:
                    pass
    except Exception:
        pass
    if after or before:
        a, b = (after or "")[:10], (before or "")[:10]
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


@app.post("/api/admin/orders/refund")
def admin_record_refund(request: Request, body: dict = Body(...)):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key = str(body.get("key") or "")
    if not _REFUND_KEY_RE.match(key):
        return JSONResponse(status_code=400, content={"error": "Unknown order"})
    try:
        amount = round(float(body.get("amount")), 2)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return JSONResponse(status_code=400, content={"error": "Enter the amount refunded"})
    with _order_refunds_lock:
        refunds = _load_order_refunds(strict=True)
        refunds[key] = {"amount": amount, "full": bool(body.get("full")),
                        "note": str(body.get("note") or "").strip()[:300],
                        "date": datetime.now(timezone.utc).isoformat()}
        _save_order_refunds(refunds)
    return {"ok": True}


@app.delete("/api/admin/orders/refund")
def admin_undo_refund(request: Request, key: str = Query("")):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    with _order_refunds_lock:
        refunds = _load_order_refunds(strict=True)
        if refunds.pop(key, None) is not None:
            _save_order_refunds(refunds)
    return {"ok": True}


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
    }


@app.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/orders")
    return RedirectResponse("/admin/orders?tab=reports")


@app.get("/api/admin/reports")
def api_admin_reports(request: Request, days: int = 30,
                      date_from: str = "", date_to: str = "",
                      all_time: int = 0):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from collections import defaultdict
    if all_time:
        orders = _fetch_wc_orders_all()
    elif date_from and date_to:
        orders = _fetch_wc_orders_all(after=f"{date_from}T00:00:00",
                                      before=f"{date_to}T23:59:59")
    else:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = _fetch_wc_orders_all(after=after)

    rows = [_order_row(o) for o in orders]

    # Summary
    total_rev  = sum(r["total"]      for r in rows)
    total_net  = sum(r["net"]        for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    n          = len(rows)
    avg_order  = total_rev / n if n else 0

    # Daily buckets
    daily: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "fees": 0.0, "orders": 0})
    for r in rows:
        d = r["date"]
        if d:
            daily[d]["revenue"] += r["total"]
            daily[d]["net"]     += r["net"]
            daily[d]["fees"]    += r["stripe_fee"]
            daily[d]["orders"]  += 1
    daily_series = [{"date": k, "revenue": round(v["revenue"], 2),
                     "net": round(v["net"], 2), "fees": round(v["fees"], 2), "orders": v["orders"]}
                    for k, v in sorted(daily.items())]

    # Monthly buckets
    monthly: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "fees": 0.0, "orders": 0})
    for r in rows:
        if r["date"] and len(r["date"]) >= 7:
            m = r["date"][:7]
            monthly[m]["revenue"] += r["total"]
            monthly[m]["net"]     += r["net"]
            monthly[m]["fees"]    += r["stripe_fee"]
            monthly[m]["orders"]  += 1
    monthly_series = [{"month": k, "revenue": round(v["revenue"], 2),
                       "net": round(v["net"], 2), "fees": round(v["fees"], 2), "orders": v["orders"]}
                      for k, v in sorted(monthly.items())]

    # By activity
    by_act: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "orders": 0})
    for r in rows:
        loc = clean_location(r["location"]) if r["location"] else "Unknown"
        by_act[loc]["revenue"] += r["total"]
        by_act[loc]["net"]     += r["net"]
        by_act[loc]["orders"]  += 1
    by_activity = [{"name": k, "revenue": round(v["revenue"], 2),
                    "net": round(v["net"], 2), "orders": v["orders"]}
                   for k, v in sorted(by_act.items(), key=lambda x: x[1]["revenue"], reverse=True)]

    # By day of week
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow: dict = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    for r in rows:
        try:
            dt  = datetime.strptime(r["date"], "%Y-%m-%d")
            day = dow_names[dt.weekday()]
            by_dow[day]["revenue"] += r["total"]
            by_dow[day]["orders"]  += 1
        except Exception:
            pass
    dow_series = [{"day": d, "revenue": round(by_dow[d]["revenue"], 2),
                   "orders": by_dow[d]["orders"]} for d in dow_names]

    return {
        "summary": {
            "revenue":   round(total_rev, 2),
            "net":       round(total_net, 2),
            "fees":      round(total_fees, 2),
            "orders":    n,
            "avg_order": round(avg_order, 2),
        },
        "daily":       daily_series,
        "monthly":     monthly_series,
        "by_activity": by_activity,
        "by_dow":      dow_series,
    }


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, days: int = 30,
                 date_from: str = "", date_to: str = "",
                 tab: str = ""):
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
    # In-person (POS) sales in the same period, listed with online orders, newest first
    pos_rows   = [_pos_order_row(o) for o in _fill_pending_square_fees(
        _fetch_pos_orders(after=after[:10], before=before[:10] if custom else None))]
    rows       = sorted(rows + pos_rows, key=lambda r: r.get("date") or "", reverse=True)
    _apply_refunds(rows, _load_order_refunds())
    total_rev  = sum(r.get("charged", r["total"]) for r in rows)
    total_refunds = sum(r.get("refunded", 0) for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    total_net  = sum(r["net"]        for r in rows)
    total_tips = sum(r.get("tip", 0) for r in rows)

    # Location breakdown
    loc_stats = {}
    for r in rows:
        loc = r["location"] or "Unknown"
        if loc not in loc_stats:
            loc_stats[loc] = {"count": 0, "revenue": 0.0, "net": 0.0}
        loc_stats[loc]["count"]   += 1
        loc_stats[loc]["revenue"] += r.get("charged", r["total"])
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
    chart_script = '''<script>
function toggleNg(name){
  var el=document.getElementById('ng-'+name);
  var arr=document.getElementById('ng-arr-'+name);
  var hdr=arr&&arr.closest('.nav-group-hdr');
  if(!el||!arr)return;
  var open=el.classList.toggle('open');
  arr.classList.toggle('open',open);
  if(hdr)hdr.classList.toggle('open',open);
}
window._rptPinDone=false;window._pendingTabBtn=null;
function switchTab(n,b){
  if(n==='reports'&&!window._rptPinDone){
    window._pendingTabBtn=b;
    document.getElementById('rpt-pin-gate').style.display='flex';
    return;
  }
  document.querySelectorAll('.tab-btn').forEach(function(x){x.classList.remove('active');});
  b.classList.add('active');
  document.getElementById('view-orders').style.display=n==='orders'?'':'none';
  var rv=document.getElementById('view-reports');
  rv.style.display=n==='reports'?'':'none';
  if(n==='reports'&&!window._rptLoaded)loadReports();
}
window._rptDays=30;window._rptFrom='';window._rptTo='';window._rptAllTime=0;window._rptLoaded=false;
function setRptRange(days,btn){
  document.querySelectorAll('.rpt-qbtn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  window._rptDays=days;window._rptFrom='';window._rptTo='';window._rptAllTime=0;
  loadReports();
}
function setRptAllTime(btn){
  document.querySelectorAll('.rpt-qbtn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  window._rptAllTime=1;window._rptFrom='';window._rptTo='';
  loadReports();
}
function applyRptCustom(){
  var from=document.getElementById('rpt-from').value;
  var to=document.getElementById('rpt-to').value;
  if(!from||!to)return;
  document.querySelectorAll('.rpt-qbtn').forEach(function(b){b.classList.remove('active');});
  window._rptFrom=from;window._rptTo=to;window._rptAllTime=0;
  loadReports();
}
async function loadReports(){
  window._rptLoaded=true;
  var loading=document.getElementById('rpt-loading');
  var content=document.getElementById('rpt-content');
  loading.style.display='block';loading.textContent='Loading…';content.style.display='none';
  var qs='';
  if(window._rptAllTime)qs='all_time=1';
  else if(window._rptFrom&&window._rptTo)qs='date_from='+window._rptFrom+'&date_to='+window._rptTo;
  else qs='days='+window._rptDays;
  try{
    var res=await fetch('/api/admin/reports?'+qs);
    if(!res.ok)throw new Error();
    var d=await res.json();
    loading.style.display='none';content.style.display='block';
    renderRpt(d);
  }catch(e){
    loading.textContent='Failed to load reports. Please try again.';
  }
}
function renderRpt(d){
  var s=d.summary;
  var feeRate=s.revenue>0?(s.fees/s.revenue*100).toFixed(1)+'%':'—';
  var netMarg=s.revenue>0?(s.net/s.revenue*100).toFixed(1)+'%':'—';
  document.getElementById('rpt-sum').innerHTML=
    rptCard('Orders',s.orders,'','')+
    rptCard('Gross Revenue','$'+s.revenue.toFixed(2),'','')+
    rptCard('Stripe Fees','-$'+s.fees.toFixed(2),'rpv-red',feeRate+' of gross')+
    rptCard('Net Revenue','$'+s.net.toFixed(2),'rpv-grn',netMarg+' margin')+
    rptCard('Avg Order','$'+s.avg_order.toFixed(2),'','');
  var useM=d.daily.length>60;
  drawRevTime(document.getElementById('chart-time'),useM?d.monthly:d.daily,!useM);
  drawActBars(document.getElementById('chart-act'),d.by_activity);
  drawDowBars(document.getElementById('chart-dow'),d.by_dow);
}
function rptCard(label,val,valCls,sub){
  return '<div class="rpt-stat"><div class="rpl">'+label+'</div><div class="rpv '+(valCls||'')+'">'+val+'</div>'+(sub?'<div class="rp-sub">'+sub+'</div>':'')+'</div>';
}
function drawRevTime(el,data,isDaily){
  if(!data||!data.length){el.innerHTML='<div class="rpt-empty">No data for this period.</div>';return;}
  var W=720,H=220,Pl=68,Pt=25,Pr=20,Pb=40;
  var iW=W-Pl-Pr,iH=H-Pt-Pb;
  var maxV=0;data.forEach(function(d){if(d.revenue>maxV)maxV=d.revenue;});
  if(!maxV){el.innerHTML='<div class="rpt-empty">No revenue in this period.</div>';return;}
  var n=data.length,bSlot=iW/Math.max(n,1),bW=Math.max(2,bSlot-Math.max(2,bSlot*.2));
  var grid='',bars='',xLab='';
  for(var i=0;i<=4;i++){
    var v=(maxV/4)*i,y=Pt+iH-(v/maxV)*iH;
    grid+='<line x1="'+Pl+'" y1="'+y.toFixed(1)+'" x2="'+(W-Pr)+'" y2="'+y.toFixed(1)+'" stroke="rgba(255,255,255,.05)" stroke-width="1"/>';
    var lbl=v>=1000?(v/1000).toFixed(v>=10000?0:1)+'k':v.toFixed(0);
    grid+='<text x="'+(Pl-6)+'" y="'+(y+4).toFixed(1)+'" font-size="10" fill="rgba(255,255,255,.38)" text-anchor="end">$'+lbl+'</text>';
  }
  data.forEach(function(d,i){
    var cx=Pl+i*bSlot+bSlot/2,x=cx-bW/2;
    var gH=(d.revenue/maxV)*iH,nH=d.net!=null?(d.net/maxV)*iH:0,base=Pt+iH;
    bars+='<rect x="'+x.toFixed(1)+'" y="'+(base-gH).toFixed(1)+'" width="'+bW.toFixed(1)+'" height="'+gH.toFixed(1)+'" fill="rgba(245,197,24,.4)" rx="2"/>';
    if(nH>0)bars+='<rect x="'+x.toFixed(1)+'" y="'+(base-nH).toFixed(1)+'" width="'+bW.toFixed(1)+'" height="'+nH.toFixed(1)+'" fill="#4ade80" rx="2" opacity=".75"/>';
  });
  var step=Math.max(1,Math.ceil(n/8));
  data.forEach(function(d,i){
    if(i%step===0){
      var lbl=isDaily?(d.date||'').slice(5):(d.month||'');
      xLab+='<text x="'+(Pl+i*bSlot+bSlot/2).toFixed(1)+'" y="'+(H-6)+'" font-size="9" fill="rgba(255,255,255,.35)" text-anchor="middle">'+lbl+'</text>';
    }
  });
  var leg='<rect x="'+Pl+'" y="7" width="10" height="7" fill="rgba(245,197,24,.4)" rx="1"/><text x="'+(Pl+13)+'" y="14" font-size="9" fill="rgba(255,255,255,.45)">Gross</text><rect x="'+(Pl+50)+'" y="7" width="10" height="7" fill="#4ade80" rx="1" opacity=".75"/><text x="'+(Pl+63)+'" y="14" font-size="9" fill="rgba(255,255,255,.45)">Net</text>';
  el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%">'+grid+bars+leg+xLab+'</svg>';
}
function drawActBars(el,data){
  if(!data||!data.length){el.innerHTML='<div class="rpt-empty">No data.</div>';return;}
  var maxV=0;data.forEach(function(d){if(d.revenue>maxV)maxV=d.revenue;});
  if(!maxV){el.innerHTML='<div class="rpt-empty">No revenue data.</div>';return;}
  el.innerHTML=data.slice(0,15).map(function(d){
    var gPct=(d.revenue/maxV*100).toFixed(1),nPct=(d.net/maxV*100).toFixed(1);
    return '<div class="rpt-bar-row">'
      +'<div class="rpt-bar-label">'+d.name+'</div>'
      +'<div style="flex:1;position:relative;height:18px;background:rgba(255,255,255,.04);border-radius:4px;overflow:hidden">'
      +'<div style="position:absolute;left:0;top:0;height:100%;width:'+gPct+'%;background:rgba(245,197,24,.4);border-radius:4px"></div>'
      +'<div style="position:absolute;left:0;top:3px;height:calc(100% - 6px);width:'+nPct+'%;background:#4ade80;border-radius:3px;opacity:.75"></div>'
      +'</div>'
      +'<div class="rpt-bar-val">$'+d.revenue.toFixed(0)+'<span class="rpt-bar-cnt">'+d.orders+' order'+(d.orders!==1?'s':'')+'</span></div>'
      +'</div>';
  }).join('');
}
function drawDowBars(el,data){
  if(!data||!data.length){el.innerHTML='<div class="rpt-empty">No data.</div>';return;}
  var maxV=0;data.forEach(function(d){if(d.revenue>maxV)maxV=d.revenue;});
  var W=520,H=160,Pl=52,Pt=18,Pr=20,Pb=30;
  var iW=W-Pl-Pr,iH=H-Pt-Pb,bSlot=iW/data.length,bW=Math.max(2,bSlot-8);
  var bars='',xLab='',grid='';
  for(var i=0;i<=3;i++){
    var v=maxV?(maxV/3)*i:0,y=Pt+iH-(maxV&&v?v/maxV:0)*iH;
    grid+='<line x1="'+Pl+'" y1="'+y.toFixed(1)+'" x2="'+(W-Pr)+'" y2="'+y.toFixed(1)+'" stroke="rgba(255,255,255,.05)" stroke-width="1"/>';
    var lbl=v>=1000?(v/1000).toFixed(0)+'k':v.toFixed(0);
    grid+='<text x="'+(Pl-4)+'" y="'+(y+4).toFixed(1)+'" font-size="9" fill="rgba(255,255,255,.35)" text-anchor="end">$'+lbl+'</text>';
  }
  data.forEach(function(d,i){
    var cx=Pl+i*bSlot+bSlot/2,x=cx-bW/2;
    var h=maxV?(d.revenue/maxV)*iH:0,y=Pt+iH-h;
    var col=(d.day==='Sat'||d.day==='Sun')?'#F5C518':'rgba(245,197,24,.55)';
    bars+='<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bW.toFixed(1)+'" height="'+Math.max(h,0).toFixed(1)+'" fill="'+col+'" rx="2"/>';
    if(d.orders>0)bars+='<text x="'+cx.toFixed(1)+'" y="'+(y-4).toFixed(1)+'" font-size="8" fill="rgba(255,255,255,.35)" text-anchor="middle">'+d.orders+'</text>';
    xLab+='<text x="'+cx.toFixed(1)+'" y="'+(H-4)+'" font-size="9" fill="rgba(255,255,255,.4)" text-anchor="middle">'+d.day+'</text>';
  });
  el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:520px">'+grid+bars+xLab+'</svg>';
}
if(window._INIT_TAB==='reports'){var _rb=document.querySelector('.tab-btn:last-child');if(_rb)_rb.click();}
</script>'''
    def td(v, cls=""): return f'<td class="{cls}">{v}</td>'
    trs = ""
    E = _html_escape
    for r in rows:
        # Names, emails, addresses and coupons come from customers (the online checkout
        # is public), so they're escaped and can't inject anything into this page
        first_file  = E((r['filenames'] or '').split(',')[0].strip() or '—')
        is_pos      = r.get("source") == "pos"
        photographer = _detect_photog(r['filenames'])   # commission follows the photo's JPEG code, not who sold it
        e_name, e_email = E(f"{r['first']} {r['last']}"), E(r['email'] or '')
        e_loc, e_what   = E(r['location'] or '—'), E(r['what'] or '')
        e_photog, e_files = E(photographer), E(r['filenames'] or '—')
        e_coupon, e_addr  = E(r['coupon_code'] or '—'), E(r['ship_addr'] or '—')
        charged     = r.get("charged", r["total"])
        tip_str     = f"${r['tip']:.2f}" if r.get("tip") else '—'
        total_str   = "Comp" if r.get("is_comp") else f"${charged:.2f}"
        if r.get("refund_full"):
            total_str = f'<s style="opacity:.5">${r["charged_original"]:.2f}</s> <span style="color:#f87171">Refunded</span>'
        elif r.get("refunded"):
            total_str = f'${charged:.2f} <span style="color:#f87171">(-${r["refunded"]:.2f} refunded)</span>'
        if is_pos:
            fee_str = "pending" if r.get("fee_pending") else (f"${r['stripe_fee']:.2f}" if r['stripe_fee'] else '—')
        else:
            fee_str = f"${r['stripe_fee']:.2f}"
        order_no    = E(str(r.get("order_no") or '—'))
        receipt_url = E(r.get("receipt_url") or '')
        order_cell  = (f'<a href="{receipt_url}" target="_blank" onclick="event.stopPropagation()" '
                       f'style="color:inherit">{order_no}</a>') if receipt_url else order_no
        detail_data = _json.dumps({
            "name":         e_name,
            "email":        e_email,
            "location":     e_loc,
            "photographer": e_photog,
            "what":         e_what,
            "files":        e_files,
            "total":        total_str,
            "tip":          tip_str,
            "discount":     f"-${r['discount']:.2f}" if r['discount'] else '—',
            "fee":          fee_str,
            "refund_key":   r["refund_key"],
            "refunded":     r.get("refunded", 0),
            "refund_full":  bool(r.get("refund_full")),
            "refund_date":  r.get("refund_date", ""),
            "refund_note":  r.get("refund_note", ""),
            "charged_num":  round(r["charged_original"], 2),
            "comp":         bool(r.get("is_comp")),
            "order_no":     ("Square " + order_no) if is_pos and r.get("order_no") else order_no,
            "receipt_url":  receipt_url,
            "net":          f"${r['net']:.2f}",
            "coupon":       e_coupon,
            "shipping":     f"${r['shipping']:.2f}" if r['shipping'] else '—',
            "address":      e_addr,
            "photo_paths":  r['photo_paths'] or '',    # raw: only used inside encoded URLs
            "date":         r['date'] or '',
            "last_name":    r['last'] or '',
            # the order list's own location filter needs the raw value too
            "location_raw": r['location'] or '',
        }).replace("&", "&amp;").replace('"', '&quot;').replace("<", "&lt;").replace(">", "&gt;")
        try:
            order_dt  = datetime.strptime(r['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
            is_recent = (now_utc - order_dt).days < DOWNLOAD_EXPIRE_DAYS
        except Exception:
            is_recent = False
        if is_pos:
            # In-person photo sales have a package download page; other POS sales have no link
            action_btn = (f'<a href="/package/{r["package_token"]}" target="_blank" class="copy-btn" '
                          f'onclick="event.stopPropagation()">Open Link</a>') if r.get("package_token") else '—'
        elif is_recent:
            action_btn = f'<button class="copy-btn" onclick="copyLink({r["order_id"]},event)">Copy Link</button>'
        else:
            action_btn = f'<a href="/admin/regen/{r["order_id"]}" class="regen-btn" onclick="event.stopPropagation()">↺ Regen Link</a>'
        trs += f"""<tr class="order-row" data-detail="{detail_data}" onclick="showDetail(this.dataset.detail)" style="cursor:pointer">
          {td(E(r['date'] or ''))}
          {td(order_cell, 'mono')}
          {td(e_name)}
          {td(e_loc)}
          {td(e_email)}
          {td(e_what)}
          {td(e_photog)}
          {td(first_file, 'mono')}
          {td(total_str)}
          {td(tip_str)}
          {td(f"-${r['discount']:.2f}" if r['discount'] else '—', 'fee')}
          {td(fee_str, 'fee')}
          {td(f"${r['net']:.2f}", 'net')}
          {td(e_coupon)}
          {td(f"${r['shipping']:.2f}" if r['shipping'] else '—')}
          {td(e_addr)}
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
#sidebar{{width:240px;flex-shrink:0;background:#0a1320;border-right:1px solid rgba(255,255,255,.07);overflow-y:auto}}
#sidebar-tree{{padding:.4rem 0}}
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
.nav-link{{display:block;padding:.52rem .9rem;border-radius:6px;margin:.05rem .45rem;font-size:16px;color:rgba(255,255,255,.55);text-decoration:none;transition:background .12s,color .12s}}
.nav-link:hover{{background:rgba(255,255,255,.06);color:rgba(255,255,255,.85)}}
.nav-link.active{{background:rgba(245,197,24,.09);color:#F5C430;box-shadow:inset 3px 0 0 #F5C430}}
.nav-sub{{padding-left:1.4rem!important;font-size:14px}}
.nav-group-hdr{{display:flex;align-items:center;justify-content:space-between;padding:.52rem .9rem;border-radius:6px;margin:.05rem .45rem;font-size:16px;color:rgba(255,255,255,.5);cursor:pointer;user-select:none;transition:background .12s,color .12s}}
.nav-group-hdr:hover{{background:rgba(255,255,255,.06);color:rgba(255,255,255,.85)}}
.nav-group-hdr.open{{color:#e8eaf0}}
.ng-arr{{font-size:10px;color:rgba(255,255,255,.3);transition:transform .15s;flex-shrink:0}}
.ng-arr.open{{transform:rotate(90deg)}}
.nav-children{{display:none}}
.nav-children.open{{display:block}}
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
.refund-box{{margin-top:1.1rem;padding:.8rem 1rem;border:1px solid rgba(248,113,113,.25);border-radius:8px;
  display:flex;align-items:center;justify-content:space-between;gap:1rem;font-size:.85rem}}
.refund-in{{background:#0f1117;border:1px solid rgba(255,255,255,.15);border-radius:6px;color:#fff;padding:.4rem .55rem;font-size:.82rem}}
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
.tab-bar{{display:flex;gap:.5rem;margin-bottom:1.25rem}}
.tab-btn{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);color:rgba(255,255,255,.5);border-radius:8px;padding:.42rem 1.1rem;font-size:.85rem;cursor:pointer;transition:all .15s}}
.tab-btn:hover{{background:rgba(255,255,255,.1);color:#fff}}
.tab-btn.active{{background:rgba(245,197,24,.12);border-color:rgba(245,197,24,.3);color:#F5C518}}
.rpt-section{{background:#1a1d27;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:1.1rem 1.25rem;margin-bottom:1.25rem}}
.rpt-title{{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.3);margin-bottom:.9rem}}
.rpt-bar-row{{display:flex;align-items:center;gap:.75rem;margin-bottom:.55rem;font-size:.82rem}}
.rpt-bar-label{{width:180px;flex-shrink:0;color:rgba(255,255,255,.7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.rpt-bar-track{{flex:1;background:rgba(255,255,255,.06);border-radius:4px;height:10px;overflow:hidden}}
.rpt-bar-fill{{height:100%;background:#F5C518;border-radius:4px}}
.rpt-bar-val{{width:160px;flex-shrink:0;color:#e2e8f0;text-align:right}}
.rpt-bar-cnt{{color:rgba(255,255,255,.35);font-size:.75rem;margin-left:.5rem}}
.rpt-chart-empty{{color:rgba(255,255,255,.3);font-size:.85rem;padding:1rem 0}}
.rpt-controls{{display:flex;gap:1rem;align-items:center;margin-bottom:1.25rem;flex-wrap:wrap}}
.rpt-qbtn{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);color:rgba(255,255,255,.5);border-radius:6px;padding:.38rem .85rem;font-size:.82rem;cursor:pointer;transition:all .15s}}
.rpt-qbtn:hover{{background:rgba(255,255,255,.1);color:#fff}}
.rpt-qbtn.active{{background:rgba(245,197,24,.12);border-color:rgba(245,197,24,.3);color:#F5C518}}
.rpt-custom{{display:flex;gap:.5rem;align-items:center}}
.rpt-custom input[type=date]{{background:#1a1d27;border:1px solid rgba(255,255,255,.15);color:#fff;padding:.36rem .65rem;border-radius:6px;font-size:.82rem}}
.rpt-custom button{{background:rgba(245,197,24,.12);border:1px solid rgba(245,197,24,.25);color:#F5C518;padding:.36rem .85rem;border-radius:6px;font-size:.82rem;cursor:pointer}}
.rpt-custom button:hover{{background:rgba(245,197,24,.22)}}
.rpt-sum{{display:flex;gap:.85rem;margin-bottom:1.25rem;flex-wrap:wrap}}
.rpt-stat{{background:#1a1d27;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:.85rem 1.1rem;flex:1;min-width:120px}}
.rpl{{font-size:10px;color:rgba(255,255,255,.38);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}}
.rpv{{font-size:1.22rem;font-weight:700;color:#F5C518}}
.rpv-red{{color:#f87171}}
.rpv-grn{{color:#4ade80}}
.rp-sub{{font-size:.75rem;color:rgba(255,255,255,.3);margin-top:2px}}
.rpt-loading{{padding:2.5rem;text-align:center;color:rgba(255,255,255,.35);font-size:.9rem;display:none}}
.rpt-empty{{padding:1.2rem 0;color:rgba(255,255,255,.3);font-size:.85rem}}
html.embedded #topbar,html.embedded #sidebar{{display:none}}
html.embedded #layout{{height:100vh}}
</style>
<link rel="stylesheet" href="/static/theme-admin.css">
</head><body>
<div id="topbar">
  <h1>Crystal Images — Admin</h1>
  <a href="/admin/logout">Sign out</a>
</div>
<div id="layout">
  <div id="sidebar">
    <a href="/admin/dashboard" class="nav-link">Dashboard</a>
    <a href="/admin/pricing" class="nav-link">Pricing</a>
    <a href="/admin/photographers" class="nav-link">Admin</a>
    <div style="border-top:1px solid rgba(255,255,255,.07);margin:.5rem 0"></div>
    <a href="/admin/orders" class="nav-link active">Orders</a>
    <a href="/admin/links" class="nav-link">Links</a>
    <a href="/admin/settings" class="nav-link">Settings</a>
    <a href="/admin/activities" class="nav-link">Activities</a>
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
window._INIT_TAB='{tab}';
function showDetail(encoded) {{
  try {{
    const d = JSON.parse(encoded);   // the row's data-detail attribute, already unescaped by the browser
    const fields = [
      ['Order #', d.receipt_url ? `<a href="${{d.receipt_url}}" target="_blank" style="color:inherit">${{d.order_no}}</a>` : d.order_no],
      ['Name', d.name], ['Email', d.email], ['Location', d.location], ['Photographer', d.photographer], ['Ordered', d.what],
      ['Total', d.total], ['Tip', d.tip], ['Discount', d.discount], ['Card Fee', d.fee],
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
    if (d.date && d.location_raw && d.last_name) {{
      const dlUrl = `/api/admin/download-folder?date=${{encodeURIComponent(d.date)}}&location=${{encodeURIComponent(d.location_raw)}}&family=${{encodeURIComponent(d.last_name)}}`;
      html += `<div style="margin-top:1.2rem;text-align:right"><a href="${{dlUrl}}" class="copy-btn" style="display:inline-block;text-decoration:none;padding:.45rem 1rem;font-size:.82rem">⬇ Download All Folder Photos</a></div>`;
    }}

    html += refundSectionHtml(d);
    document.getElementById('detail-body').innerHTML = html;
    document.getElementById('detail-modal').classList.add('open');
  }} catch(e) {{
    console.error('showDetail error:', e);
  }}
}}
function escH(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}
// Refunds are done in Square or WooCommerce; this records them so they come off
// revenue, net and photographer commission.
function refundSectionHtml(d) {{
  if (!d.refund_key || d.comp) return '';
  if (d.refunded) {{
    return `<div class="refund-box"><div><strong style="color:#f87171">${{d.refund_full ? 'Refunded' : 'Partly refunded'}}</strong>
      $${{Number(d.refunded).toFixed(2)}} on ${{escH(d.refund_date)}}${{d.refund_note ? ' &middot; ' + escH(d.refund_note) : ''}}</div>
      <button class="copy-btn" onclick="undoRefund('${{d.refund_key}}')">Undo</button></div>`;
  }}
  return `<div class="refund-box" style="display:block">
    <div style="font-size:.8rem;color:rgba(255,255,255,.55)">Refunded this sale in Square or WooCommerce? Record it here so it comes off revenue and commission.</div>
    <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-top:.6rem">
      <label style="font-size:.8rem">Amount $ <input id="refund-amt" class="refund-in" type="number" step="0.01" min="0.01" value="${{Number(d.charged_num).toFixed(2)}}" style="width:6rem"></label>
      <input id="refund-note" class="refund-in" placeholder="Note (optional)" style="flex:1;min-width:8rem">
      <button class="copy-btn" onclick="saveRefund('${{d.refund_key}}', ${{Number(d.charged_num)}})">Mark as Refunded</button>
    </div></div>`;
}}
async function saveRefund(key, full) {{
  const amt = parseFloat(document.getElementById('refund-amt').value);
  if (!(amt > 0)) {{ alert('Enter the amount refunded'); return; }}
  if (amt > full + 0.005 && !confirm("That is more than this sale's total. Record it anyway?")) return;
  const note = document.getElementById('refund-note').value.trim();
  const r = await fetch('/api/admin/orders/refund', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key, amount: amt, full: amt >= full - 0.005, note}})}});
  if (!r.ok) {{ const e = await r.json().catch(() => ({{}})); alert(e.error || 'Could not save the refund'); return; }}
  location.reload();
}}
async function undoRefund(key) {{
  if (!confirm('Remove this refund? The sale will count toward revenue and commission again.')) return;
  const r = await fetch('/api/admin/orders/refund?key=' + encodeURIComponent(key), {{method: 'DELETE'}});
  if (!r.ok) {{ alert('Could not undo the refund'); return; }}
  location.reload();
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
      h+='<div class="st-date" onclick="location.href=\\'/admin/dashboard?date=\\'+encodeURIComponent(date)"><span class="st-arr">&#9654;</span>'+date+'</div><div class="st-locs">';
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
    <div class="stat"><div class="label">Card Fees</div><div class="val" style="color:#f87171">${total_fees:.2f}</div></div>
    <div class="stat"><div class="label">Net</div><div class="val" style="color:#4ade80">${total_net:.2f}</div></div>
    <div class="stat"><div class="label">Tips</div><div class="val">${total_tips:.2f}</div></div>
    <div class="stat"><div class="label">Refunds</div><div class="val" style="color:#f87171">${total_refunds:.2f}</div></div>
  </div>
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('orders',this)">Orders</button>
  </div>
  <div id="view-orders">
  <div class="loc-breakdown">
    <div class="loc-breakdown-title">Revenue by Location</div>
    <div class="loc-bars">
      {"".join(
        f'<div class="loc-bar-row">'
        f'<span class="loc-bar-label">{_html_escape(loc)}</span>'
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
      <th>Date</th><th>Order #</th><th>Name</th><th>Location</th><th>Email</th><th>Ordered</th><th>Photographer</th>
      <th>Photo Files</th><th>Total</th><th>Tip</th><th>Discount</th><th>Card Fee</th><th>Net</th>
      <th>Coupon</th><th>Shipping</th><th>Address</th><th>Action</th>
    </tr></thead>
    <tbody>{trs if trs else '<tr><td colspan="17" class="empty">No completed orders in this period.</td></tr>'}</tbody>
  </table>
  </div>
  </div>
  <div id="view-reports" style="display:none">
    <div class="rpt-controls">
      <div style="display:flex;gap:.4rem;flex-wrap:wrap">
        <button class="rpt-qbtn active" onclick="setRptRange(7,this)">7D</button>
        <button class="rpt-qbtn" onclick="setRptRange(30,this)">30D</button>
        <button class="rpt-qbtn" onclick="setRptRange(90,this)">90D</button>
        <button class="rpt-qbtn" onclick="setRptRange(365,this)">12M</button>
        <button class="rpt-qbtn" onclick="setRptAllTime(this)">All Time</button>
      </div>
      <div class="rpt-custom">
        <input type="date" id="rpt-from" />
        <span style="color:rgba(255,255,255,.3);font-size:.82rem">to</span>
        <input type="date" id="rpt-to" />
        <button onclick="applyRptCustom()">Apply</button>
      </div>
    </div>
    <div id="rpt-loading" class="rpt-loading">Loading&hellip;</div>
    <div id="rpt-content" style="display:none">
      <div id="rpt-sum" class="rpt-sum"></div>
      <div class="rpt-section">
        <div class="rpt-title">Revenue Over Time &mdash; <span style="font-weight:400;color:rgba(255,255,255,.4)">gold&nbsp;=&nbsp;gross &nbsp; green&nbsp;=&nbsp;net</span></div>
        <div id="chart-time"></div>
      </div>
      <div class="rpt-section">
        <div class="rpt-title">Revenue by Activity &mdash; <span style="font-weight:400;color:rgba(255,255,255,.4)">gold&nbsp;=&nbsp;gross &nbsp; green&nbsp;=&nbsp;net</span></div>
        <div id="chart-act"></div>
      </div>
      <div class="rpt-section">
        <div class="rpt-title">Revenue by Day of Week &mdash; <span style="font-weight:400;color:rgba(255,255,255,.4)">weekend highlighted</span></div>
        <div id="chart-dow"></div>
      </div>
    </div>
  </div>
  </div>
</div>

<div id="rpt-pin-gate" style="position:fixed;inset:0;background:rgba(15,17,23,.97);z-index:10000;display:none;align-items:center;justify-content:center">
  <div style="background:#1a1d27;border:1px solid rgba(255,255,255,.1);border-radius:14px;padding:2.4rem 2.8rem;text-align:center;width:300px">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:rgba(255,255,255,.28);margin-bottom:.3rem">Crystal Images</div>
    <div style="font-size:1.25rem;font-weight:700;color:#e8eaf0;margin-bottom:.2rem">Admin Area</div>
    <div style="font-size:13px;color:rgba(255,255,255,.38);margin-bottom:1.6rem">Enter your PIN to view reports</div>
    <div id="rpt-pin-dots" style="display:flex;gap:.55rem;justify-content:center;margin-bottom:.5rem">
      <div class="rpt-pd"></div><div class="rpt-pd"></div><div class="rpt-pd"></div><div class="rpt-pd"></div>
    </div>
    <div id="rpt-pin-err" style="font-size:12px;color:#f87171;height:1.1rem;margin-bottom:.75rem"></div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem">
      <button class="rpb" onclick="rptPinKey('1')">1</button><button class="rpb" onclick="rptPinKey('2')">2</button><button class="rpb" onclick="rptPinKey('3')">3</button>
      <button class="rpb" onclick="rptPinKey('4')">4</button><button class="rpb" onclick="rptPinKey('5')">5</button><button class="rpb" onclick="rptPinKey('6')">6</button>
      <button class="rpb" onclick="rptPinKey('7')">7</button><button class="rpb" onclick="rptPinKey('8')">8</button><button class="rpb" onclick="rptPinKey('9')">9</button>
      <button class="rpb rpbg" onclick="rptPinClear()">Clear</button><button class="rpb" onclick="rptPinKey('0')">0</button><button class="rpb rpbg" onclick="rptPinBack()">&#9003;</button>
    </div>
    <button onclick="document.getElementById('rpt-pin-gate').style.display='none'" style="margin-top:1rem;background:none;border:none;color:rgba(255,255,255,.3);font-size:.8rem;cursor:pointer">Cancel</button>
  </div>
</div>
<style>
.rpt-pd{{width:13px;height:13px;border-radius:50%;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);transition:background .15s,border-color .15s;display:inline-block}}
.rpt-pd.filled{{background:#F5C430;border-color:#F5C430}}
.rpb{{background:#1e2330;border:1px solid rgba(255,255,255,.09);border-radius:9px;color:#e8eaf0;font-size:1.1rem;font-weight:500;padding:.72rem;cursor:pointer;transition:background .12s}}
.rpb:hover{{background:#252d3e}}
.rpb.rpbg{{background:transparent;border-color:rgba(255,255,255,.05);color:rgba(255,255,255,.4);font-size:.82rem}}
</style>

{chart_script}
<script>
var _rptPinVal='';
function rptPinKey(k){{
  if(_rptPinVal.length>=4)return;
  _rptPinVal+=k;
  _rptPinSync();
  if(_rptPinVal.length===4)_rptPinSubmit();
}}
function rptPinBack(){{_rptPinVal=_rptPinVal.slice(0,-1);_rptPinSync();document.getElementById('rpt-pin-err').textContent='';}}
function rptPinClear(){{_rptPinVal='';_rptPinSync();document.getElementById('rpt-pin-err').textContent='';}}
function _rptPinSync(){{
  var dots=document.querySelectorAll('#rpt-pin-dots .rpt-pd');
  dots.forEach(function(d,i){{d.classList.toggle('filled',i<_rptPinVal.length);}});
}}
async function _rptPinSubmit(){{
  try{{
    var r=await fetch('/api/photographer/auth',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{pin:_rptPinVal}})}});
    if(r.ok){{
      document.getElementById('rpt-pin-gate').style.display='none';
      window._rptPinDone=true;
      if(window._pendingTabBtn){{switchTab('reports',window._pendingTabBtn);window._pendingTabBtn=null;}}
    }}else{{document.getElementById('rpt-pin-err').textContent='Incorrect PIN';_rptPinVal='';_rptPinSync();}}
  }}catch(e){{document.getElementById('rpt-pin-err').textContent='Error — try again';_rptPinVal='';_rptPinSync();}}
}}
document.addEventListener('keydown',function(e){{
  if(document.getElementById('rpt-pin-gate').style.display==='none'||document.getElementById('rpt-pin-gate').style.display==='')return;
  if(e.key>='0'&&e.key<='9')rptPinKey(e.key);
  else if(e.key==='Backspace')rptPinBack();
}});
</script>
<script>if(window.parent!==window)document.documentElement.classList.add('embedded');</script>
</body></html>"""
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
    rows  += [_pos_order_row(o) for o in _fill_pending_square_fees(_fetch_pos_orders(
        after=(date_from if date_from and date_to else (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")),
        before=(date_to if date_from and date_to else None)))]
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    _apply_refunds(rows, _load_order_refunds())
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
    writer.writerow(["Date","Order #","First Name","Last Name","Email","Photographer","What Ordered",
                     "Photo Files","Location","Photo Date","Total","Tip","Discount",
                     "Card Fee","Net","Refunded","Coupon Code","Shipping Cost","Shipping Address"])
    for r in rows:
        photog = _exp_photog(r["filenames"])
        if r.get("source") == "pos":
            fee_csv = "pending" if r.get("fee_pending") else (f"${r['stripe_fee']:.2f}" if r["stripe_fee"] else "")
            order_csv = ("Square " + r["order_no"]) if r.get("order_no") else ""
        else:
            fee_csv, order_csv = f"${r['stripe_fee']:.2f}", r.get("order_no", "")
        writer.writerow([r["date"], order_csv, r["first"], r["last"], r["email"], photog, r["what"],
                         r["filenames"], r["location"], r["photo_date"],
                         "Comp" if r.get("is_comp") else f"${r['charged_original']:.2f}",
                         f"${r['tip']:.2f}" if r.get("tip") else "",
                         f"-${r['discount']:.2f}" if r["discount"] else "",
                         fee_csv, f"${r['net']:.2f}",
                         f"${r['refunded']:.2f}" if r.get("refunded") else "",
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
    for p in _load_photographers():
        if p.get("pin") == pin:
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


@app.get("/api/admin/devices")
def admin_devices(request: Request):
    """Approved devices, for Settings > Security."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    mine = _device_approved(request)
    devices = [{"describe": v.get("describe", ""), "approved": v.get("approved", ""),
                "last_signin": v.get("last_signin", v.get("approved", "")), "ip": v.get("ip", ""),
                "this_device": k == mine}
               for k, v in _load_admin_auth()["devices"].items() if v.get("expires", "") > _now_iso()]
    return {"devices": sorted(devices, key=lambda x: x["last_signin"], reverse=True),
            "codes_on": ADMIN_LOGIN_CODES_ON, "email": _masked_email(OWNER_EMAIL)}


@app.post("/api/admin/sign-out-everywhere")
def admin_sign_out_everywhere(request: Request):
    """End every studio session and forget every approved device (each needs a code again)."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    d = _load_admin_auth()
    with _admin_auth_lock:
        d["sessions"], d["devices"] = {}, {}
        _save_admin_auth(d)
    resp = JSONResponse({"ok": True})
    for c in (ADMIN_COOKIE, ADMIN_DEVICE_COOKIE, OWNER_COOKIE):
        resp.delete_cookie(c)
    return resp


@app.get("/api/admin/owner-status")
def owner_status(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"unlocked": _owner_unlocked(request)}


@app.post("/api/admin/owner-unlock")
def owner_unlock(request: Request, body: dict = Body(...)):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if _pin_locked_out(request):
        return JSONResponse(status_code=429, content={"error": "Too many wrong PINs — wait 10 minutes"})
    owner_pin = _owner_pin()
    if not owner_pin:
        return JSONResponse(status_code=503, content={
            "error": f"No photographer named {OWNER_NAME} — add one with your PIN in Photographers first"})
    if not hmac.compare_digest(str(body.get("pin") or "").strip(), owner_pin):
        _record_pin_failure(request)
        return JSONResponse(status_code=401, content={"error": "Incorrect PIN"})
    expires = int(_time.time()) + OWNER_UNLOCK_SECONDS
    resp = JSONResponse({"ok": True})
    resp.set_cookie(OWNER_COOKIE, _owner_token(expires), httponly=True, samesite="strict",
                    max_age=OWNER_UNLOCK_SECONDS)
    return resp


def _pos_pin_photographer(request: Request):
    """Photographer for the X-POS-PIN header, or None (also None while locked out)."""
    pin = request.headers.get("X-POS-PIN", "").strip()
    if not pin or _pin_locked_out(request):
        return None
    p = _auth_photographer(pin)
    if not p:
        _record_pin_failure(request)
    return p

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
def photographer_auth(request: Request, body: dict = Body(...)):
    if _pin_locked_out(request):
        return JSONResponse(status_code=429, content={"error": "Too many wrong PINs — try again in a few minutes"})
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        _record_pin_failure(request)
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    records = _load_clock_records()
    active = next((r for r in reversed(records)
                   if r["photographer_id"] == p["id"] and not r.get("clock_out")), None)
    resp = JSONResponse({"photographer": {k: v for k, v in p.items() if k != "pin"},
                         "clocked_in": active is not None, "active_record": active})
    # Lets this device load staff (no-watermark) thumbnails; renewed each time a PIN is entered
    expires = int(_time.time()) + POS_COOKIE_SECONDS
    resp.set_cookie(POS_COOKIE, _pos_token(expires), httponly=True, samesite="lax", max_age=POS_COOKIE_SECONDS)
    return resp

# ── Clock in / out ──
# Clock-ins run side by side (off the main loop), so the timecard file is updated
# under a lock: two photographers clocking in at once can't overwrite each other.
_clock_lock = threading.Lock()


@app.post("/api/clock/in")
def clock_in(body: dict = Body(...)):
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    with _clock_lock:
        return _clock_in(p, body)


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

@app.post("/api/clock/out")
def clock_out(body: dict = Body(...)):
    p = _auth_photographer(body.get("pin", ""))
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    with _clock_lock:
        return _clock_out(p)


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
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
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
async def delete_clock_record(record_id: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
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
    denied = _owner_required(request)
    if denied:
        return denied
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
    denied = _owner_required(request)
    if denied:
        return denied
    records = _load_clock_records()
    photographers = _load_photographers()
    return {"records": records, "photographers": photographers}

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

@app.get("/api/upload/locations")
def api_upload_locations():
    """Public list of locations available in the upload tool — portrait + activities."""
    _load_portrait_locations()
    pricing = _load_pricing()
    activity_names = sorted(pricing.get("activities", {}).keys(), key=str.lower)
    portrait_set = set(l.lower() for l in PORTRAIT_LOCATIONS)

    # Build category list for the layered upload UI
    raw_cats = pricing.get("activity_categories", [])
    categories = []
    for cat in raw_cats:
        name = cat.get("name", "")
        acts = [a for a in (cat.get("acts") or []) if a]
        active = cat.get("active", True)
        categories.append({"name": name, "acts": acts, "active": active})
        # Portrait-type categories also expand the portrait detection set
        if name.strip().lower() in _PORTRAIT_CATEGORY_NAMES:
            for act in acts:
                portrait_set.add(act.strip().lower())

    return {
        "portrait":   sorted(portrait_set, key=str.lower),
        "activities": activity_names,
        "categories": categories,
    }

# ── Pending (cull feed) ──
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

def _batch_delete_keys(keys: list):
    """Batch-delete R2 keys, up to 1000 per call. Ignores missing keys."""
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i+1000]
        try:
            s3.delete_objects(Bucket=R2_BUCKET,
                              Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
        except Exception as e:
            print(f"[batch_delete] error: {e}")


@app.post("/api/trash/empty")
async def trash_empty(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    trash = _load_trash_meta()
    if not trash:
        return {"emptied": 0}
    count = len(trash)
    _save_trash_meta([])   # clear meta immediately so UI updates fast
    keys = [t["key"] for t in trash]
    threading.Thread(target=_batch_delete_keys, args=(keys,), daemon=True).start()
    return {"emptied": count}


# ── Photographer management (admin) ──
@app.get("/api/photographers")
def list_photographers(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    return {"photographers": _load_photographers()}

@app.post("/api/photographers")
async def add_photographer(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
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
         "commission_type": body.get("commission_type", "hourly"),
         "commission_rate": float(body.get("commission_rate", 0) or 0),
         "created_at": datetime.now(timezone.utc).isoformat()}
    photographers.append(p)
    _save_photographers(photographers)
    return {"photographer": p}

@app.put("/api/photographers/{pid}")
async def update_photographer(pid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body = await request.json()
    photographers = _load_photographers()
    for p in photographers:
        if p["id"] == pid:
            for field in ("name", "pin", "default_location", "file_prefix", "commission_type"):
                if field in body:
                    p[field] = body[field]
            if "commission_rate" in body:
                p["commission_rate"] = float(body["commission_rate"] or 0)
            _save_photographers(photographers)
            return {"photographer": p}
    return JSONResponse(status_code=404, content={"error": "Not found"})

@app.delete("/api/photographers/{pid}")
async def delete_photographer(pid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    photographers = [p for p in _load_photographers() if p["id"] != pid]
    _save_photographers(photographers)
    return {"deleted": True}

@app.get("/api/photographers/commission")
def photographer_commission(request: Request,
                             date_from: str = Query(""),
                             date_to:   str = Query("")):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    if not date_from:
        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")
    photographers = _load_photographers()
    records       = _load_clock_records()
    pending_meta  = _load_pending_meta()
    # Revenue by photographer prefix
    prefix_map = {}  # prefix (3-char lower) -> photographer id
    for p in photographers:
        pfx = p.get("file_prefix", "").strip().lower()
        if pfx:
            prefix_map[pfx] = p["id"]
    rev_by_pid: dict[str, float] = {p["id"]: 0.0 for p in photographers}
    refunds = _load_order_refunds()
    try:
        wc_orders = _fetch_wc_orders_all(after=date_from, before=date_to)
        pos_orders = _fetch_pos_orders(after=date_from, before=date_to)
        for o in wc_orders:
            row = _order_row(o)
            fnames = (row.get("filenames") or "").split(",")
            for f in fnames:
                f = f.strip()
                if len(f) >= 3:
                    pid_match = prefix_map.get(f[:3].lower())
                    if pid_match and pid_match in rev_by_pid:
                        rev_by_pid[pid_match] += _commission_amount(row, refunds)
                        break
        for o in pos_orders:
            row = _pos_order_row(o)
            fnames = (row.get("filenames") or "").split(",")
            for f in fnames:
                f = f.strip()
                if len(f) >= 3:
                    pid_match = prefix_map.get(f[:3].lower())
                    if pid_match and pid_match in rev_by_pid:
                        rev_by_pid[pid_match] += _commission_amount(row, refunds)
                        break
    except Exception:
        pass
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
            "revenue": round(rev_by_pid.get(pid, 0.0), 2),
            "clock_records": shifts,
        })
    return {"commission": result, "date_from": date_from, "date_to": date_to}


# ─────────────────────────────────────────
# DISCOUNT CODE ADMIN API
# ─────────────────────────────────────────

@app.get("/api/admin/discount-codes")
def api_list_discount_codes(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    codes = _load_discount_codes()
    # Return codes without the full uses history (use /api/admin/discount-codes/{id} for that)
    summary = []
    for c in codes:
        s = {k: v for k, v in c.items() if k != "uses"}
        s["recent_uses"] = c.get("uses", [])[-5:]
        summary.append(s)
    return {"codes": summary}

@app.post("/api/admin/discount-codes")
async def api_create_discount_code(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()

    auto = body.get("auto_generate", False)
    prefix = body.get("prefix", "").strip().upper()
    if auto:
        code = _generate_code(prefix=prefix)
    else:
        code = body.get("code", "").strip().upper()

    if not code:
        return JSONResponse(status_code=400, content={"error": "Code is required"})

    codes = _load_discount_codes()
    if any(c.get("code","").upper() == code for c in codes):
        return JSONResponse(status_code=400, content={"error": f"Code '{code}' already exists"})

    ctype = body.get("type", "percent")
    if ctype not in ("percent", "fixed", "free_shipping"):
        return JSONResponse(status_code=400, content={"error": "type must be percent, fixed, or free_shipping"})

    new_code = {
        "id":                    str(uuid.uuid4())[:8],
        "code":                  code,
        "created":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "description":           body.get("description", "").strip(),
        "source":                body.get("source", "promo"),  # promo | receipt | review | referral
        "type":                  ctype,
        "amount":                float(body.get("amount", 0)),
        "free_shipping":         bool(body.get("free_shipping", False)),
        "applies_to":            body.get("applies_to", "all"),  # all | digital | prints | portraits | frames
        "min_order":             float(body.get("min_order", 0)),
        "min_qty":               int(body.get("min_qty", 0)),
        "usage_limit":           body.get("usage_limit") or None,
        "usage_limit_per_user":  body.get("usage_limit_per_user") or None,
        "expires":               body.get("expires") or None,
        "start_date":            body.get("start_date") or None,
        "active":                True,
        "usage_count":           0,
        "total_saved":           0.0,
        "uses":                  [],
    }

    # Sync to WooCommerce so native coupon_lines work in checkout
    wc_result = _wc_sync_coupon(new_code)
    if "wc_id" in wc_result:
        new_code["wc_id"] = wc_result["wc_id"]
    else:
        new_code["wc_sync_error"] = wc_result.get("error", "")

    codes.append(new_code)
    _save_discount_codes(codes)
    return {"code": {k: v for k, v in new_code.items() if k != "uses"}}

@app.get("/api/admin/discount-codes/{cid}")
def api_get_discount_code(cid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    codes = _load_discount_codes()
    c = next((x for x in codes if x.get("id") == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {"code": c}

@app.put("/api/admin/discount-codes/{cid}")
async def api_update_discount_code(cid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    codes = _load_discount_codes()
    WC_SYNC_FIELDS = {"description","source","type","amount","free_shipping",
                      "applies_to","min_order","min_qty","usage_limit",
                      "usage_limit_per_user","expires","start_date","active"}
    for c in codes:
        if c.get("id") == cid:
            changed_wc = any(f in body for f in WC_SYNC_FIELDS)
            for field in WC_SYNC_FIELDS:
                if field in body:
                    c[field] = body[field]
            if changed_wc:
                wc_result = _wc_sync_coupon(c)
                if "wc_id" in wc_result:
                    c["wc_id"] = wc_result["wc_id"]
                    c.pop("wc_sync_error", None)
                else:
                    c["wc_sync_error"] = wc_result.get("error", "")
            _save_discount_codes(codes)
            return {"code": {k: v for k, v in c.items() if k != "uses"}}
    return JSONResponse(status_code=404, content={"error": "Not found"})

@app.delete("/api/admin/discount-codes/{cid}")
async def api_delete_discount_code(cid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    all_codes = _load_discount_codes()
    to_delete = next((c for c in all_codes if c.get("id") == cid), None)
    if to_delete and to_delete.get("wc_id"):
        _wc_delete_coupon(to_delete["wc_id"])
    codes = [c for c in all_codes if c.get("id") != cid]
    _save_discount_codes(codes)
    return {"deleted": True}

@app.post("/api/admin/discount-codes/{cid}/wc-sync")
async def api_wc_sync_code(cid: str, request: Request):
    """Manually push a code to WooCommerce (or re-sync if it previously failed)."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    codes = _load_discount_codes()
    c = next((x for x in codes if x.get("id") == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    result = _wc_sync_coupon(c)
    if "wc_id" in result:
        c["wc_id"] = result["wc_id"]
        c.pop("wc_sync_error", None)
        _save_discount_codes(codes)
        return {"wc_id": result["wc_id"]}
    c["wc_sync_error"] = result.get("error", "")
    _save_discount_codes(codes)
    return JSONResponse(status_code=502, content={"error": result.get("error", "WC sync failed")})


@app.post("/api/admin/discount-codes/{cid}/generate")
async def api_generate_child_code(cid: str, request: Request):
    """Generate a new unique one-off code using an existing code as a template."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    codes = _load_discount_codes()
    template = next((c for c in codes if c.get("id") == cid), None)
    if not template:
        return JSONResponse(status_code=404, content={"error": "Template code not found"})

    prefix = body.get("prefix", template.get("code", "")).rstrip("-") + "-"
    new_code_str = _generate_code(prefix=prefix, length=6)
    while any(c.get("code","").upper() == new_code_str for c in codes):
        new_code_str = _generate_code(prefix=prefix, length=6)

    new_code = {k: v for k, v in template.items() if k not in ("id","code","usage_count","total_saved","uses")}
    new_code.update({
        "id":            str(uuid.uuid4())[:8],
        "code":          new_code_str,
        "created":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "template_id":   cid,
        "usage_count":   0,
        "total_saved":   0.0,
        "uses":          [],
        "expires":       body.get("expires") or template.get("expires"),
        "usage_limit":   body.get("usage_limit") or template.get("usage_limit"),
        "description":   body.get("description") or template.get("description",""),
    })
    wc_result = _wc_sync_coupon(new_code)
    if "wc_id" in wc_result:
        new_code["wc_id"] = wc_result["wc_id"]
    else:
        new_code["wc_sync_error"] = wc_result.get("error", "")

    codes.append(new_code)
    _save_discount_codes(codes)
    return {"code": {k: v for k, v in new_code.items() if k != "uses"}}


@app.get("/admin/discount-codes", response_class=HTMLResponse)
def admin_discount_codes_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/discount-codes")
    return HTMLResponse(open("templates/admin_discounts.html").read())


# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("templates/index.html").read())

@app.head("/", include_in_schema=False)
def index_head():
    """Uptime monitors check the site with HEAD requests (headers only, no page)."""
    return Response(status_code=200, media_type="text/html")

@app.get("/checkout", response_class=HTMLResponse)
def checkout_page():
    return HTMLResponse(open("templates/checkout.html").read())

@app.get("/clockin", response_class=HTMLResponse)
def clockin_page():
    return HTMLResponse(open("templates/clockin.html").read())

@app.post("/api/pos-unlock")
async def api_pos_unlock(request: Request):
    body = await request.json()
    code = body.get("passcode", "").strip()
    if not POS_PASSCODE:
        return {"ok": True}
    if code == POS_PASSCODE:
        return {"ok": True}
    return JSONResponse(status_code=401, content={"ok": False, "error": "Incorrect passcode"})

@app.get("/pos", response_class=HTMLResponse)
def pos_page():
    return HTMLResponse(open("templates/pos.html").read())

@app.get("/pos/manifest.json")
def pos_manifest():
    return JSONResponse({
        "name": "Crystal Images POS",
        "short_name": "CI POS",
        "start_url": "/pos",
        "display": "standalone",
        "background_color": "#082f44",
        "theme_color": "#082f44",
        "icons": [
            {"src": "/static/pos-icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/pos-icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    })

# ── Upload downloads ──
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

# ── ZIP PRICING TEST PAGES ────────────────────────────────────────────────────

ZIP_LOCS_SET    = {"adventure zip", "adventure zip line", "adventure zipline", "nature zip", "nature zip line", "nature zipline"}
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
        else:
            poses.append({"color": "", "label": "Unlabeled", "files": files})

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

@app.get("/api/admin/text-presets")
async def get_text_presets(request: Request):
    if not _admin_authed(request):
        raise HTTPException(403)
    return {"presets": _load_text_presets()}

@app.post("/api/admin/text-preset")
async def save_text_preset(request: Request):
    if not _admin_authed(request):
        raise HTTPException(403)
    body = await request.json()
    preset_id   = body.get("id") or ""
    name        = (body.get("name") or "").strip()
    text        = (body.get("text") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    presets = _load_text_presets()
    existing = next((p for p in presets if p.get("id") == preset_id), None)
    if existing:
        existing["name"] = name
        existing["text"] = text
    else:
        import uuid
        presets.append({"id": str(uuid.uuid4())[:8], "name": name, "text": text})
    _save_text_presets(presets)
    return {"ok": True, "presets": presets}

@app.delete("/api/admin/text-preset/{preset_id}")
async def delete_text_preset(preset_id: str, request: Request):
    if not _admin_authed(request):
        raise HTTPException(403)
    presets = [p for p in _load_text_presets() if p.get("id") != preset_id]
    _save_text_presets(presets)
    return {"ok": True}

@app.post("/api/admin/text-preset/bulk-apply")
async def bulk_apply_text_preset(request: Request):
    """Apply a preset's text as description to one or more folder keys."""
    if not _admin_authed(request):
        raise HTTPException(403)
    body = await request.json()
    text        = (body.get("text") or "").strip()
    folder_keys = body.get("folder_keys") or []
    if not folder_keys:
        raise HTTPException(400, "folder_keys required")
    fm = _load_folder_meta()
    for fk in folder_keys:
        if fk not in fm:
            fm[fk] = {}
        fm[fk]["description"] = text
    _save_folder_meta(fm)
    return {"ok": True, "updated": len(folder_keys)}

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
    denied = _owner_required(request)
    if denied:
        return denied
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

@app.post("/api/admin/folder/set-item-list")
async def api_set_folder_item_list(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk = body.get("folder_key")
    item_list = body.get("item_list", "")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    folder_meta = _load_folder_meta()
    if fk not in folder_meta:
        folder_meta[fk] = {}
    folder_meta[fk]["item_list"] = item_list
    _save_folder_meta(folder_meta)
    return {"status": "ok"}

@app.get("/admin/zip-compare")
def zip_compare_page():
    return RedirectResponse("/admin/pricing#zip")

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
    files    = body.get("files", [])
    if not date or not location or not files:
        return JSONResponse(status_code=400, content={"error": "Missing date, location, or files"})
    is_portrait = _is_portrait_location(location)
    folder      = body.get("folder", "").strip() if is_portrait else ""
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
    keys     = body.get("keys", [])
    poses    = body.get("poses", None)  # from UI pose assignment; None means use XMP detection
    times    = body.get("times", {})   # {filename: minutes_since_midnight} from client EXIF read
    if not date or not location or not keys:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    is_portrait = _is_portrait_location(location)
    folder   = body.get("folder", "").strip() if is_portrait else ""
    existing    = {item["path"] for item in data}
    added       = 0
    location = clean_location(location)
    for key in keys:
        if key not in existing:
            entry = {
                "path":      key,
                "date":      date,
                "location":  location,
                "last_name": folder if is_portrait else "",
                "group":     "" if is_portrait else folder,
            }
            if not is_portrait:
                entry["draft"] = True
            data.append(entry)
            existing.add(key)
            added += 1
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(), ContentType="application/json")
    if is_portrait and folder and keys:
        if poses is not None:
            _write_folder_poses_from_ui(date, location, folder, poses)
        else:
            _update_folder_poses(date, location, folder, keys)
    if times:
        # Only persist times for locations that have time_search enabled
        _pricing = _load_pricing()
        _loc_cfg = _pricing.get("activities", {}).get(location, {})
        if _loc_cfg.get("flags", {}).get("time_search"):
            cache_key = (date, location.strip().lower())
            merged = dict(_folder_times_cache.get(cache_key) or {})
            merged.update(times)
            _folder_times_cache[cache_key] = merged
            try:
                s3.put_object(Bucket=R2_BUCKET, Key=_times_r2_key(date, location),
                              Body=json.dumps(merged).encode(), ContentType="application/json")
            except Exception:
                pass
    # Thumbnails for every uploaded photo (drafts too — the studio shows them)
    thumbs_job = _queue_new_photo_thumbs(keys)
    return {"indexed": added, "date": date, "location": location, "folder": folder,
            "thumbs_job_id": thumbs_job}

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
    location    = clean_location(location)
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    prefix = f"images/{date}/{loc_slug}/{folder_slug}/" if folder_slug else f"images/{date}/{loc_slug}/"
    is_portrait = location.lower() in PORTRAIT_LOCATIONS
    existing = {item["path"] for item in data}
    added = 0
    added_keys = []
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
                    "draft":     True,
                })
                existing.add(key)
                added_keys.append(key)
                added += 1
    if added:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
    if is_portrait and folder:
        all_keys = [
            obj["Key"]
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix)
            for obj in page.get("Contents", [])
            if obj["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
        if all_keys:
            _update_folder_poses(date, location, folder, all_keys)
    thumbs_job = _queue_new_photo_thumbs(added_keys)
    return {"indexed": added, "prefix": prefix, "thumbs_job_id": thumbs_job}

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

    pushed   = 0
    to_embed = []
    for item in data:
        if (item.get("draft")
                and item["date"] == date
                and item["location"].strip().lower() == location.lower()
                and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()):
            del item["draft"]
            to_embed.append(item)
            pushed += 1

    if pushed:
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")

    job_id = None
    if to_embed:
        job_id = str(uuid.uuid4())[:8]
        _embed_jobs[job_id] = {"total": len(to_embed), "done": 0, "finished": False}
        def _embed_bg(jid=job_id):
            count = embed_items_fast(to_embed, job_id=jid)
            s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                          Body=json.dumps(data).encode(), ContentType="application/json")
            if jid in _embed_jobs:
                _embed_jobs[jid]["finished"] = True
            print(f"Push-live classify done: {count}/{len(to_embed)}")
        threading.Thread(target=_embed_bg, daemon=True).start()

    return {"pushed": pushed, "job_id": job_id, "classify_total": len(to_embed)}


_pos_thumbs_job = {"job_id": None}


def _r2_keys_under(prefix: str) -> set:
    keys = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix):
        keys.update(o["Key"] for o in page.get("Contents", []))
    return keys


@app.post("/api/admin/thumbs/pos")
def admin_pos_thumbs(request: Request, body: dict = Body(...)):
    """Make missing POS thumbnails for photos in a date range (e.g. the winter photos),
    newest dates first. Safe to run again: photos that already have one are skipped."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    date_from = (body.get("date_from") or "").strip()
    date_to   = (body.get("date_to") or "").strip()
    if not date_to:
        return JSONResponse(status_code=400, content={"error": "Choose the last date to include"})
    running = _embed_jobs.get(_pos_thumbs_job["job_id"] or "")
    if running and not running.get("finished"):
        return {"job_id": _pos_thumbs_job["job_id"], "already_running": True}
    have = _r2_keys_under(_nowm_thumb_r2_key("images/"))
    photos = [it for it in data
              if not it.get("draft")
              and (not date_from or (it.get("date") or "") >= date_from)
              and (it.get("date") or "") <= date_to]
    work = []
    for item in sorted(photos, key=lambda it: it.get("date") or "", reverse=True):
        key = to_r2_key(item.get("path") or "")
        if key.startswith("images/") and _nowm_thumb_r2_key(key) not in have:
            work.append((key, ["pos"]))
    job_id = _start_thumb_job(work)
    _pos_thumbs_job["job_id"] = job_id
    return {"job_id": job_id, "queued": len(work), "in_range": len(photos)}


@app.get("/api/admin/embed-status")
def admin_embed_status(request: Request, job_id: str = Query(...)):
    """Return progress for a background labeling / thumbnail job (studio only)."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    job = _embed_jobs.get(job_id)
    if not job:
        return {"done": 0, "total": 0, "finished": True}
    return {"done": job["done"], "total": job["total"], "finished": job.get("finished", False),
            "failed": job.get("failed", 0)}


@app.post("/api/admin/classify-backfill")
async def admin_classify_backfill(request: Request):
    """Labeling backfill: label sport and jacket/pants colors (labeling model) for photos
    that have no jacket_colors label yet.
    Optionally filter by location (e.g. 'Whitefish Mountain') to process only
    specific resort folders instead of the entire library."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not _load_onnx_model():
        return JSONResponse(status_code=503, content={"error": "The labeling model isn't available"})
    body             = await request.json()
    location_filter  = (body.get("location") or "").strip().lower()
    batch_limit      = int(body.get("limit") or 0)  # 0 = no limit

    _portrait_locs_lower = {loc.strip().lower() for loc in PORTRAIT_LOCATIONS}
    candidates = [
        it for it in data
        if not it.get("draft")
        and it.get("jacket_colors") is None
        and it.get("location", "").strip().lower() not in _portrait_locs_lower
        and (not location_filter or location_filter in it.get("location", "").lower())
    ]
    to_classify = candidates[:batch_limit] if batch_limit > 0 else candidates
    remaining   = max(0, len(candidates) - len(to_classify))

    if not to_classify:
        return {"queued": 0, "message": "All matching photos already classified", "remaining": 0}

    job_id = str(uuid.uuid4())[:8]
    _embed_jobs[job_id] = {"total": len(to_classify), "done": 0, "finished": False}

    def _bg(jid=job_id):
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_and_classify(item):
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=item["path"])
                raw  = resp["Body"].read()
                colors = classify_photo_colors(raw)
                if colors:
                    item["sport"]         = colors.get("sport", "unclear")
                    item["jacket_colors"] = colors.get("jacket_colors", [])
                    item["pants_colors"]  = colors.get("pants_colors", [])
                    item["classifier"]    = MODEL_LABEL
                return True
            except Exception as e:
                print(f"classify-backfill failed {item['path']}: {e}")
                return False

        done = 0
        batch_size = 16
        for i in range(0, len(to_classify), batch_size):
            batch = to_classify[i:i + batch_size]
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(_fetch_and_classify, batch))
            done += sum(results)
            if jid in _embed_jobs:
                _embed_jobs[jid]["done"] = done
            # Save progress every batch so partial results survive a restart
            s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                          Body=json.dumps(data).encode(), ContentType="application/json")

        if jid in _embed_jobs:
            _embed_jobs[jid]["finished"] = True
        print(f"Classify backfill done: {done}/{len(to_classify)}")

    threading.Thread(target=_bg, daemon=True).start()
    return {"queued": len(to_classify), "remaining": remaining, "job_id": job_id}


@app.post("/api/admin/classify-folder")
async def admin_classify_folder(request: Request):
    """Run the labeling model on a specific folder (date + location + folder name).
    Processes only photos in that folder that don't yet have color labels."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not _load_onnx_model():
        return JSONResponse(status_code=503, content={"error": "The labeling model isn't available"})
    body     = await request.json()
    date     = (body.get("date") or "").strip()
    location = (body.get("location") or "").strip().lower()
    folder   = (body.get("folder") or "").strip().lower()

    if not date or not location:
        return JSONResponse(status_code=400, content={"error": "date and location required"})

    to_classify = [
        it for it in data
        if not it.get("draft")
        and it.get("jacket_colors") is None
        and it["date"] == date
        and it.get("location", "").strip().lower() == location
        and (not folder or (it.get("last_name","") or it.get("group","")).strip().lower() == folder)
    ]
    if not to_classify:
        return {"queued": 0, "message": "All photos in this folder already classified"}

    job_id = str(uuid.uuid4())[:8]
    _embed_jobs[job_id] = {"total": len(to_classify), "done": 0, "finished": False}

    def _bg(jid=job_id):
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_and_classify(item):
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=item["path"])
                raw  = resp["Body"].read()
                colors = classify_photo_colors(raw)
                if colors:
                    item["sport"]         = colors.get("sport", "unclear")
                    item["jacket_colors"] = colors.get("jacket_colors", [])
                    item["pants_colors"]  = colors.get("pants_colors", [])
                    item["classifier"]    = MODEL_LABEL
                return True
            except Exception as e:
                print(f"classify-folder failed {item['path']}: {e}")
                return False

        done = 0
        for i in range(0, len(to_classify), 16):
            batch = to_classify[i:i + 16]
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(_fetch_and_classify, batch))
            done += sum(results)
            if jid in _embed_jobs:
                _embed_jobs[jid]["done"] = done
        s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
        if jid in _embed_jobs:
            _embed_jobs[jid]["finished"] = True
        print(f"Classify folder done: {done}/{len(to_classify)}")

    threading.Thread(target=_bg, daemon=True).start()
    return {"queued": len(to_classify), "job_id": job_id}


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


def _bulk_trash(to_trash: list) -> tuple[list, int]:
    """Copy items to trash sequentially, then batch-delete originals in one call."""
    if not to_trash:
        return [], 0

    now       = datetime.now(timezone.utc)
    now_iso   = now.isoformat()
    purge_iso = (now + timedelta(days=7)).isoformat()
    new_meta  = []
    orig_keys = []

    for item in to_trash:
        key       = to_r2_key(item["path"])
        uid       = str(uuid.uuid4())[:8]
        trash_key = f"studio_trash/{uid}_{os.path.basename(item['path'])}"
        try:
            s3.copy_object(Bucket=R2_BUCKET,
                           CopySource={"Bucket": R2_BUCKET, "Key": key},
                           Key=trash_key)
        except Exception as e:
            print(f"[trash] copy failed {key}: {e}")
            continue
        orig_keys.append(key)
        new_meta.append({
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
        })

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

    to_trash = [item for item in data if _matches(item)]
    if not to_trash:
        return {"deleted": 0, "removed_index": 0}

    # Remove from index immediately so the response is instant
    count = len(to_trash)
    data = [item for item in data if not _matches(item)]
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(), ContentType="application/json")
    try:
        fm = _load_folder_meta()
        fk = _folder_key(date, location, folder)
        if fk in fm:
            del fm[fk]
            _save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    # Copy to trash in background so we don't block the HTTP response
    def _bg():
        new_meta, trashed = _bulk_trash(to_trash)
        if trashed:
            tm = _load_trash_meta(); tm.extend(new_meta); _save_trash_meta(tm)
        print(f"[delete-folder] bg trash done: {trashed}/{count}")
    threading.Thread(target=_bg, daemon=True).start()

    return {"deleted": count, "removed_index": count}


@app.post("/api/admin/delete-location")
async def admin_delete_location(request: Request):
    """Move all photos at a date+location to trash (7-day recovery)."""
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()

    def _matches(item):
        return (
            item["date"] == date
            and item["location"].strip().lower() == location.lower()
        )

    to_trash = [item for item in data if _matches(item)]
    if not to_trash:
        return {"deleted": 0}

    count = len(to_trash)
    data = [item for item in data if not _matches(item)]
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(), ContentType="application/json")
    try:
        fm = _load_folder_meta()
        keys_to_del = [k for k in fm if k.startswith(f"{date}|{location}|") or k == f"{date}|{location}|"]
        for k in keys_to_del:
            del fm[k]
        if keys_to_del:
            _save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    def _bg():
        new_meta, trashed = _bulk_trash(to_trash)
        if trashed:
            tm = _load_trash_meta(); tm.extend(new_meta); _save_trash_meta(tm)
        print(f"[delete-location] bg trash done: {trashed}/{count}")
    threading.Thread(target=_bg, daemon=True).start()

    return {"deleted": count}


@app.post("/api/admin/delete-date")
async def admin_delete_date(request: Request):
    """Move all photos on a date to trash (7-day recovery)."""
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    date = body.get("date", "")

    to_trash = [item for item in data if item.get("date", "") == date]
    if not to_trash:
        return {"deleted": 0}

    count = len(to_trash)
    data = [item for item in data if item.get("date", "") != date]
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(), ContentType="application/json")
    try:
        fm = _load_folder_meta()
        keys_to_del = [k for k in fm if k.startswith(f"{date}|")]
        for k in keys_to_del:
            del fm[k]
        if keys_to_del:
            _save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    def _bg():
        new_meta, trashed = _bulk_trash(to_trash)
        if trashed:
            tm = _load_trash_meta(); tm.extend(new_meta); _save_trash_meta(tm)
        print(f"[delete-date] bg trash done: {trashed}/{count}")
    threading.Thread(target=_bg, daemon=True).start()

    return {"deleted": count}


import copy as _copy

_SETTINGS_DEFAULTS: dict = {
    "time_filter_locations": [],
    "site":     {"name": "Crystal Images", "tagline": "", "logo_url": ""},
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


@app.get("/api/admin/settings")
async def get_admin_settings(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return _load_settings()


@app.post("/api/admin/settings")
async def save_admin_settings(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    _save_settings(body)
    return {"ok": True}


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/settings")
    return HTMLResponse(open("templates/admin_settings.html").read())


@app.get("/admin/indexing", response_class=HTMLResponse)
async def admin_indexing_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/indexing")
    return HTMLResponse(open("templates/admin_indexing.html").read())

@app.get("/admin/activities", response_class=HTMLResponse)
async def admin_activities_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/activities")
    return HTMLResponse(open("templates/admin_activities.html").read())

@app.get("/admin/customers", response_class=HTMLResponse)
async def admin_customers_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/customers")
    return HTMLResponse(open("templates/admin_customers.html").read())


@app.post("/api/admin/rename-activity")
async def rename_activity(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    old_name = body.get("old_name", "").strip()
    new_name = body.get("new_name", "").strip()
    if not old_name or not new_name:
        return JSONResponse(status_code=400, content={"error": "Both old_name and new_name are required"})
    if old_name == new_name:
        return {"renamed": True, "photos_updated": 0}

    # Update location on every matching photo in memory
    updated = 0
    for item in data:
        if item.get("location", "").strip() == old_name:
            item["location"] = new_name
            updated += 1

    # Normalize all locations and deduplicate by path to prevent merge duplicates
    for item in data:
        raw = item.get("location", "")
        if raw:
            item["location"] = clean_location(raw)
    seen_paths: set = set()
    deduped = []
    for item in data:
        path = item.get("path", "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        deduped.append(item)
    data = deduped

    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(),
                  ContentType="application/json")

    # Rename / merge the pricing config key and update category acts list
    pricing = _load_pricing()
    acts    = pricing.get("activities", {})
    if old_name in acts:
        old_cfg = acts.pop(old_name)
        if new_name not in acts:
            acts[new_name] = old_cfg
    elif new_name not in acts:
        acts[new_name] = {}
    pricing["activities"] = acts
    for cat in pricing.get("activity_categories", []):
        cat_acts = cat.get("acts", [])
        if old_name in cat_acts:
            idx = cat_acts.index(old_name)
            if new_name not in cat_acts:
                cat_acts[idx] = new_name
            else:
                cat_acts.pop(idx)
    _save_pricing(pricing)

    return {"renamed": True, "photos_updated": updated}


@app.post("/api/admin/normalize-locations")
async def normalize_locations(request: Request):
    global data
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    # Step 1: normalize all location fields to their clean display form
    for item in data:
        raw = item.get("location", "")
        if raw:
            item["location"] = clean_location(raw)

    # Step 2: dedup by exact R2 path (same file indexed twice)
    seen_paths: set = set()
    after_path: list = []
    for item in data:
        path = item.get("path", "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        after_path.append(item)

    # Step 3: dedup by (date, location, family/group, filename)
    # Catches the same photo stored under two different directory names in R2
    seen_keys: set = set()
    deduped: list = []
    for item in after_path:
        filename = os.path.basename(item.get("path", ""))
        key = (
            item.get("date", ""),
            item.get("location", ""),
            item.get("last_name", "") or item.get("group", ""),
            filename,
        )
        if filename and key in seen_keys:
            continue
        if filename:
            seen_keys.add(key)
        deduped.append(item)

    removed = len(data) - len(deduped)
    data = deduped
    s3.put_object(Bucket=R2_BUCKET, Key="images.json",
                  Body=json.dumps(data).encode(),
                  ContentType="application/json")

    return {"normalized": True, "total": len(data), "duplicates_removed": removed}


@app.get("/api/admin/debug-data")
async def admin_debug_data(request: Request):
    """Temporary diagnostic: shows what's in memory vs R2."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    dates_in_memory = sorted({item.get("date", "NO_DATE") for item in data}, reverse=True)
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
        r2_data = json.loads(obj["Body"].read())
        dates_in_r2 = sorted({item.get("date", "NO_DATE") for item in r2_data}, reverse=True)
        r2_count = len(r2_data)
    except Exception as e:
        dates_in_r2 = [f"error: {e}"]
        r2_count = -1
    return {
        "memory_count": len(data),
        "memory_dates": dates_in_memory,
        "r2_count": r2_count,
        "r2_dates": dates_in_r2,
        "sample_paths": [item.get("path", "") for item in data[:5]],
    }


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
            is_portrait = loc_lower in PORTRAIT_LOCATIONS
            folders[fk] = {
                "folder_key":       fk,
                "date":             date,
                "location":         location,
                "name":             name,
                "photo_count":      0,
                "draft_count":      0,
                "classified_count": 0,
                "is_zip":           is_zip,
                "is_portrait":      is_portrait,
                "group_size":       folder_meta.get(fk, {}).get("group_size") if is_zip else None,
                "_preview":         None,
            }
        folders[fk]["photo_count"] += 1
        if item.get("draft"):
            folders[fk]["draft_count"] += 1
        if item.get("jacket_colors") is not None:
            folders[fk]["classified_count"] += 1
        if folders[fk]["_preview"] is None:
            folders[fk]["_preview"] = item.get("path")
    from urllib.parse import quote as _quote
    result = []
    for f in sorted(folders.values(), key=lambda x: (x["date"], x["location"], x["name"]), reverse=True):
        preview_path = f.pop("_preview", None)
        f["preview_url"] = f"/api/admin/photo?path={_quote(preview_path)}&size=thumb" if preview_path else ""
        f["reviewed"] = bool(folder_meta.get(f["folder_key"], {}).get("reviewed"))
        result.append(f)
    return {"folders": result}

@app.post("/api/admin/toggle-folder-reviewed")
async def api_toggle_folder_reviewed(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk = body.get("folder_key")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "folder_key required"})
    fm = _load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["reviewed"] = not bool(fm[fk].get("reviewed"))
    _save_folder_meta(fm)
    return {"ok": True, "reviewed": fm[fk]["reviewed"]}

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

@app.get("/admin/dashboard/{section}", response_class=HTMLResponse)
def admin_dashboard_section(request: Request, section: str):
    if not _admin_authed(request):
        return RedirectResponse(f"/admin?next=/admin/dashboard/{section}")
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
        "description": fm.get(fk, {}).get("description", ""),
    }

@app.post("/api/admin/folder/description")
async def admin_folder_set_description(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk   = body.get("folder_key", "").strip()
    desc = body.get("description", "").strip()
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    fm = _load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["description"] = desc
    _save_folder_meta(fm)
    return {"status": "ok"}

@app.get("/api/folder/info")
def api_folder_info(date: str = Query(None), location: str = Query(None), family: str = Query(None)):
    if not date:
        return {"description": ""}
    # date-only key ("date||"):   used for date-level admin note
    # location-level key ("date|loc|"):  shown on sub-folder list view
    # folder-level key ("date|loc|fam"): shown inside a specific folder
    fk = _folder_key(date, (location or "").strip(), (family or "").strip())
    fm = _load_folder_meta()
    desc = fm.get(fk, {}).get("description", "")
    # Fall back to category default_note when no folder-specific description
    if not desc and location:
        pricing = _load_pricing()
        loc_name = (location or "").strip().lower()
        for cat in (pricing.get("activity_categories") or []):
            acts = [a.lower() for a in (cat.get("acts") or [])]
            if loc_name in acts and cat.get("default_note"):
                desc = cat["default_note"]
                break
    return {"description": desc}

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
        for key_fn in (_thumb_r2_key, _grid_thumb_r2_key):
            try:
                s3.delete_object(Bucket=R2_BUCKET, Key=key_fn(key))
            except Exception:
                pass
        _forget_thumb_links(key)
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
    _queue_new_photo_thumbs(list(path_remap.values()))
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
    _queue_new_photo_thumbs(list(path_remap.values()))
    return {"renamed": len(path_remap), "new_folder": new_folder}


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

@app.get("/api/print-pricing")
def api_print_pricing():
    return _load_print_pricing()

@app.post("/api/admin/print-pricing")
async def api_save_print_pricing(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body = await request.json()
    _save_print_pricing(body)
    return {"status": "ok"}

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

@app.get("/api/portrait-pricing")
def api_portrait_pricing():
    return _load_portrait_pricing()

@app.get("/api/admin/portrait-locations")
def api_get_portrait_locations(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    _load_portrait_locations()
    return {"locations": sorted(PORTRAIT_LOCATIONS)}

@app.post("/api/admin/portrait-locations")
async def api_save_portrait_locations(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body  = await request.json()
    locs  = [l.lower().strip() for l in body.get("locations", []) if l.strip()]
    _save_portrait_locations(locs)
    _load_portrait_locations()
    return {"ok": True, "locations": sorted(PORTRAIT_LOCATIONS)}

@app.post("/api/admin/portrait-pricing")
async def api_save_portrait_pricing(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body = await request.json()
    _save_portrait_pricing(body)
    return {"status": "ok"}

# ── SEASONS ─────────────────────────────────────────────────────────────────────
@app.get("/api/seasons")
def api_get_seasons():
    return {"seasons": _load_seasons()}

@app.post("/api/admin/seasons")
async def api_save_seasons(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    seasons = body.get("seasons", [])
    _save_seasons(seasons)
    return {"ok": True, "seasons": seasons}

# ── FOLDER POSES ────────────────────────────────────────────────────────────────
@app.get("/api/folder/poses")
def api_folder_poses(request: Request, date: str = Query(None),
                     location: str = Query(None), family: str = Query(None)):
    if not date or not location or not family:
        return {"poses": [], "poses_live": False, "has_poses": False}
    fk    = _folder_key(date, location.strip(), family.strip())
    fm    = _load_folder_meta()
    entry = fm.get(fk, {})
    poses = entry.get("poses", [])
    if not poses:
        return {"poses": [], "poses_live": False, "has_poses": False}
    poses_live = entry.get("poses_live", False)
    is_admin   = _admin_authed(request)
    if not poses_live and not is_admin:
        return {"poses": [], "poses_live": False, "has_poses": False}
    return {
        "poses":        poses,
        "poses_live":   poses_live,
        "has_poses":    True,
        "preview_only": not poses_live and is_admin,
    }

@app.post("/api/admin/folder/poses-live")
async def api_toggle_poses_live(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    family   = body.get("family", "").strip()
    live     = bool(body.get("live", False))
    if not date or not location:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    fk = _folder_key(date, location, family)
    fm = _load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["poses_live"] = live
    _save_folder_meta(fm)
    return {"ok": True, "poses_live": live}

@app.post("/api/admin/folder/rebuild-poses")
async def api_rebuild_poses(request: Request):
    """Re-read XMP labels for all photos in a portrait folder and rebuild pose groupings."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    if not date or not location or not folder:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-")
    prefix      = f"images/{date}/{loc_slug}/{folder_slug}/"
    all_keys = [
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix)
        for obj in page.get("Contents", [])
        if obj["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]
    if not all_keys:
        return {"ok": False, "error": "No images found"}
    _update_folder_poses(date, location, folder, all_keys)
    fk    = _folder_key(date, location, folder)
    fm    = _load_folder_meta()
    poses = fm.get(fk, {}).get("poses", [])
    return {"ok": True, "poses_found": len(poses), "photos_scanned": len(all_keys)}

@app.get("/api/admin/activity-groups")
def api_activity_groups_get(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    pricing = _load_pricing()
    _derived = {
        cat["name"]: cat.get("acts", [])
        for cat in pricing.get("activity_categories", [])
        if cat.get("name")
    }
    merged = {**_derived, **pricing.get("activity_groups", {})}
    return {"activity_groups": merged}

@app.post("/api/admin/activity-groups")
async def api_activity_groups_save(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    groups = body.get("activity_groups", {})
    pricing = _load_pricing()
    pricing["activity_groups"] = groups
    _save_pricing(pricing)
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

@app.get("/api/private-session-pricing")
def api_get_private_session_pricing():
    return _load_private_session_pricing()

@app.post("/api/admin/private-session-pricing")
async def api_save_private_session_pricing(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body = await request.json()
    _save_private_session_pricing(body)
    return {"ok": True}

# ── ITEMS BUNDLES ─────────────────────────────────────────────────────────────
ITEMS_BUNDLES_KEY = "meta/items_bundles.json"

_items_bundles_doc = JsonDoc(s3, R2_BUCKET, ITEMS_BUNDLES_KEY, ttl=30, indent=2,   # read on every checkout quote
                             default=lambda: {"bundles": []})

def _load_items_bundles():  return _items_bundles_doc.load()
def _save_items_bundles(d): _items_bundles_doc.save(d)

@app.get("/api/items-bundles")
def api_get_items_bundles():
    return _load_items_bundles()

@app.post("/api/admin/items-bundles")
async def api_save_items_bundles(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = _owner_required(request)
    if denied:
        return denied
    body = await request.json()
    _save_items_bundles(body)
    return {"ok": True}


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
    for key, val in plain_vars.items():
        if key in html_blocks:
            continue
        body = body.replace(f"{{{{{key}}}}}", str(val) if val is not None else "")
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
        inner = para.replace("\n", "<br>")
        parts_html.append(f'<p style="margin:0 0 .9rem;font-size:.9rem;color:#333;line-height:1.65">{inner}</p>')
        parts_text.append(para)
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

def _wrap_email_html(body_html: str) -> str:
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f9f9f9;font-family:Arial,sans-serif">'
        '<div style="max-width:560px;margin:0 auto;padding:2rem 1rem">'
        '<div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:2rem">'
        '<p style="margin:0 0 1.25rem;font-size:.75rem;color:#999;text-transform:uppercase;letter-spacing:.5px">Crystal Images</p>'
        + body_html +
        '<hr style="border:none;border-top:1px solid #eee;margin:1.5rem 0 1rem">'
        '<p style="margin:0;font-size:.72rem;color:#aaa">Questions? <a href="mailto:info@bigskyphotos.com" style="color:#1a73e8">info@bigskyphotos.com</a></p>'
        '</div></div></body></html>'
    )

@app.get("/api/admin/email-templates")
def api_list_email_templates(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"templates": list(_load_email_templates().values())}

@app.put("/api/admin/email-templates/{tid}")
def api_update_email_template(tid: str, request: Request, body: dict = Body(...)):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    templates = _load_email_templates()
    if tid not in templates:
        return JSONResponse(status_code=404, content={"error": "Template not found"})
    templates[tid]["subject"] = body.get("subject", templates[tid]["subject"])
    templates[tid]["body"]    = body.get("body",    templates[tid]["body"])
    if "review_url" in body:
        review_url = (body.get("review_url") or "").strip()
        if review_url and not review_url.startswith("https://"):
            return JSONResponse(status_code=400, content={"error": "Review link must start with https://"})
        templates[tid]["review_url"] = review_url
    if not templates[tid].get("system") and "name" in body:
        templates[tid]["name"] = body["name"]
    _save_email_templates(templates)
    return {"ok": True, "template": templates[tid]}

@app.post("/api/admin/email-templates")
def api_create_email_template(request: Request, body: dict = Body(...)):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    import uuid as _uuid
    tid = "custom_" + _uuid.uuid4().hex[:8]
    templates = _load_email_templates()
    templates[tid] = {
        "id": tid, "name": name, "system": False,
        "subject": body.get("subject", ""),
        "body":    body.get("body",    ""),
        "tokens":  ["first_name"],
    }
    _save_email_templates(templates)
    return {"ok": True, "template": templates[tid]}

@app.delete("/api/admin/email-templates/{tid}")
def api_delete_email_template(tid: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    templates = _load_email_templates()
    if tid not in templates:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if templates[tid].get("system"):
        return JSONResponse(status_code=400, content={"error": "Cannot delete system templates"})
    del templates[tid]
    _save_email_templates(templates)
    return {"ok": True}


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
                            square: dict | None = None, comp: bool = False):
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
            f'<tr style="border-top:2px solid #eee">'
            f'<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700">Complimentary</td>'
            f'<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700;text-align:right">No charge</td></tr>'
            if comp else
            f'<tr><td style="padding:.25rem .5rem;font-size:.83rem;color:#888">4% Resort Tax</td>'
            f'<td style="padding:.25rem .5rem;font-size:.83rem;text-align:right">${tax:.2f}</td></tr>'
            + (f'<tr><td style="padding:.25rem .5rem;font-size:.83rem;color:#888">Tip</td>'
               f'<td style="padding:.25rem .5rem;font-size:.83rem;text-align:right">${tip:.2f}</td></tr>' if tip else '')
            + f'<tr style="border-top:2px solid #eee">'
            f'<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700">Total Charged</td>'
            f'<td style="padding:.45rem .5rem;font-size:.95rem;font-weight:700;text-align:right">${grand_total:.2f}</td></tr>'
        )
        + f'</table>'
    )
    dl_btn_html = ""
    if download_url:
        dl_btn_html = (
            f'<p style="margin:0 0 .5rem;font-size:.9rem;font-weight:600;color:#111">Your Photos</p>'
            f'<p style="margin:0 0 1rem"><a href="{download_url}" '
            f'style="background:#1a73e8;color:#fff;padding:.55rem 1.25rem;border-radius:6px;'
            f'font-size:.88rem;font-weight:600;text-decoration:none;display:inline-block">'
            f'Download Your Photos</a></p>'
            + (f'<p style="margin:0 0 1rem;font-size:.8rem;color:#888">Your download link is available until {expire_str}.</p>'
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


@app.post("/api/admin/packages/upload")
async def api_create_package_upload(request: Request):
    """Create a download package by uploading photos directly (not from the site gallery)."""
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from fastapi import UploadFile, File, Form
    form = await request.form()
    title      = (form.get("title") or "").strip()
    emails_raw = (form.get("emails") or "").strip()
    expire_days = int(form.get("expire_days") or 30)
    files = form.getlist("photos")

    emails = [e.strip() for e in emails_raw.replace(",", "\n").split("\n") if e.strip()]
    if not title or not emails or not files:
        return JSONResponse(status_code=400, content={"error": "title, emails, and at least one photo required"})

    token = str(uuid.uuid4()).replace("-", "")
    now     = datetime.now(timezone.utc)
    expires = (now + timedelta(days=expire_days)).isoformat()
    expire_str = (now + timedelta(days=expire_days)).strftime("%B %d, %Y")

    paths = []
    for f in files:
        if not hasattr(f, "filename") or not f.filename:
            continue
        safe_name = f.filename.replace("/", "_").replace("\\", "_")
        r2_key = f"packages/uploads/{token}/{safe_name}"
        data = await f.read()
        s3.put_object(Bucket=R2_BUCKET, Key=r2_key, Body=data,
                      ContentType=f.content_type or "image/jpeg")
        paths.append(r2_key)

    if not paths:
        return JSONResponse(status_code=400, content={"error": "No valid photos received"})

    pkg = {
        "token": token, "title": title, "emails": emails,
        "paths": paths, "created": now.isoformat(),
        "expires": expires, "expire_days": expire_days,
    }
    _store_pkg(token, pkg)

    download_url = f"{SITE_URL}/package/{token}"
    _send_package_email(emails, title, download_url, len(paths), expire_str)
    return {"token": token, "url": download_url}


@app.post("/api/admin/packages")
async def api_create_package(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    title = body.get("title", "").strip()
    emails = [e.strip() for e in body.get("emails", []) if e.strip()]
    paths = body.get("paths", [])
    expire_days = int(body.get("expire_days", 30))

    if not title or not emails or not paths:
        return JSONResponse(status_code=400, content={"error": "title, emails, and paths required"})

    token = str(uuid.uuid4()).replace("-", "")
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=expire_days)).isoformat()
    expire_str = (now + timedelta(days=expire_days)).strftime("%B %d, %Y")

    pkg = {
        "token": token,
        "title": title,
        "emails": emails,
        "paths": paths,
        "created": now.isoformat(),
        "expires": expires,
        "expire_days": expire_days,
    }
    _store_pkg(token, pkg)

    download_url = f"{SITE_URL}/package/{token}"
    _send_package_email(emails, title, download_url, len(paths), expire_str)

    return {"token": token, "url": download_url}


@app.get("/api/admin/packages")
def api_list_packages(request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    pkgs = _list_pkgs()
    now = datetime.now(timezone.utc)
    result = []
    for p in pkgs:
        try:
            exp = datetime.fromisoformat(p["expires"])
            expired = now > exp
        except Exception:
            expired = False
        result.append({
            "token":   p["token"],
            "title":   p["title"],
            "emails":  p.get("emails", []),
            "count":   len(p.get("paths", [])),
            "created": p.get("created", "")[:10],
            "expires": p.get("expires", "")[:10],
            "expired": expired,
        })
    return {"packages": result}


@app.delete("/api/admin/packages/{token}")
def api_delete_package(token: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    _delete_pkg_r2(token)
    return {"status": "ok"}


@app.post("/api/admin/packages/{token}/extend")
async def api_extend_package(token: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    days = int(body.get("days", 30))
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    pkg["expires"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    pkg["expire_days"] = days
    _store_pkg(token, pkg)
    return {"expires": pkg["expires"]}


@app.post("/api/admin/packages/{token}/resend-email")
async def api_resend_pkg_email(token: str, request: Request):
    if not _admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    download_url = f"{SITE_URL}/package/{token}"
    try:
        exp_str = datetime.fromisoformat(pkg["expires"]).strftime("%B %d, %Y")
    except Exception:
        exp_str = "soon"
    _send_package_email(pkg.get("emails", []), pkg["title"], download_url, len(pkg.get("paths", [])), exp_str)
    return {"status": "ok"}


@app.get("/package/{token}")
def package_page(token: str):
    pkg = _load_pkg(token)
    if not pkg:
        return HTMLResponse(_pkg_error("Invalid or expired download link.", 404), status_code=404)
    try:
        expires_dt = datetime.fromisoformat(pkg["expires"])
    except Exception:
        return HTMLResponse(_pkg_error("Invalid link.", 404), status_code=404)
    if datetime.now(timezone.utc) > expires_dt:
        return HTMLResponse(_pkg_error(
            "This download link has expired.",
            410,
            sub="Please contact us at <a href='mailto:info@bigskyphotos.com' style='color:#F2C94C'>info@bigskyphotos.com</a> to receive a new link."
        ), status_code=410)

    paths = pkg.get("paths", [])
    expire_str = expires_dt.strftime("%B %d, %Y")
    title_esc = pkg["title"].replace("<", "&lt;").replace(">", "&gt;")

    photo_items = "\n".join(
        f'<div class="pi"><img src="/package/{token}/thumb/{i}" alt=""><a href="/package/{token}/file/{i}" download class="dib">⬇ Download</a></div>'
        for i, _ in enumerate(paths)
    )

    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_esc} — Crystal Images</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh}}
header{{background:rgba(0,0,0,.35);border-bottom:1px solid rgba(255,255,255,.08);padding:.9rem 1.5rem;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.75rem;
  position:sticky;top:0;z-index:10;backdrop-filter:blur(8px)}}
header h1{{color:#F2C94C;font-size:1.1rem;font-weight:700}}
header .meta{{font-size:.75rem;color:rgba(255,255,255,.38);margin-top:.2rem}}
.dla{{background:#F2C94C;color:#0d1f2d;border:none;border-radius:7px;padding:.55rem 1.2rem;
  font-weight:700;font-size:.88rem;cursor:pointer;text-decoration:none;display:inline-block;white-space:nowrap}}
.dla:disabled{{opacity:.6;cursor:default}}
.dla.alt{{background:rgba(255,255,255,.1);color:#fff}}
.acts{{display:flex;gap:.5rem;flex-wrap:wrap}}
.wrap{{max-width:1400px;margin:0 auto;padding:1.25rem 1rem}}
.lic{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px;
  padding:.7rem 1rem;margin-bottom:1.1rem;font-size:.76rem;color:rgba(255,255,255,.42);line-height:1.8}}
.lic strong{{color:rgba(255,255,255,.68)}}
.grid{{position:relative;min-height:100px}}
.pi{{position:absolute;overflow:hidden;border-radius:6px;background:#111;cursor:pointer;transition:opacity .15s}}
.pi:hover{{opacity:.88}}
.pi img{{width:100%;height:auto;display:block}}
.pi:hover img{{opacity:.78}}
.dib{{position:absolute;bottom:7px;right:7px;background:#F2C94C;color:#0d1f2d;border-radius:5px;
  padding:.3rem .7rem;font-size:.75rem;font-weight:700;text-decoration:none;
  opacity:0;transition:opacity .15s;display:flex;align-items:center;gap:.3rem}}
.pi:hover .dib{{opacity:1}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:999;display:none;
  flex-direction:column;align-items:center;justify-content:center}}
#lb.open{{display:flex}}
#lb-img{{max-width:92vw;max-height:84vh;object-fit:contain;border-radius:4px;cursor:default}}
#lb-bar{{position:absolute;top:0;left:0;right:0;display:flex;align-items:center;justify-content:flex-end;
  padding:.75rem 1rem;gap:.65rem;background:linear-gradient(rgba(0,0,0,.5),transparent)}}
#lb-dl{{background:#F2C94C;color:#0d1f2d;border:none;border-radius:6px;padding:.38rem .9rem;
  font-weight:700;font-size:.82rem;cursor:pointer;text-decoration:none}}
#lb-close{{background:rgba(255,255,255,.12);border:none;color:#fff;font-size:1.1rem;cursor:pointer;
  border-radius:6px;padding:.35rem .7rem;opacity:.8}}
#lb-close:hover{{opacity:1}}
</style></head>
<body>
<header>
  <div>
    <h1>{title_esc}</h1>
    <div class="meta">{len(paths)} photo{'s' if len(paths)!=1 else ''} &nbsp;·&nbsp; Link expires {expire_str}</div>
  </div>
  <div class="acts">
    <button id="save-photos" class="dla" style="display:none" onclick="savePhotosClick()">Save to Photos</button>
    <a href="/package/{token}/all" download class="dla" id="dl-all">⬇ Download All ({len(paths)} photos)</a>
  </div>
  <div id="save-hint" class="meta" style="display:none;width:100%"></div>
</header>
<div class="wrap">
  <div class="lic">
    All photos are for <strong>personal use</strong> — print, frame, and share freely.
    For commercial use, tag <strong>@crystalimagesbigsky</strong> on Instagram.
    Please do not resell or redistribute the original files.
    Questions? <a href="mailto:info@bigskyphotos.com" style="color:#F2C94C">info@bigskyphotos.com</a>
  </div>
  <div class="grid" id="masonry-grid">{photo_items}</div>
</div>
<div id="lb">
  <div id="lb-bar">
    <button id="lb-save" class="dla" style="display:none;padding:.38rem .9rem;font-size:.82rem" onclick="lbSaveClick()">Save to Photos</button>
    <a id="lb-dl" href="#" download>⬇ Download</a>
    <button id="lb-close" onclick="closeLb()">✕</button>
  </div>
  <img id="lb-img" src="" alt="">
</div>
<script>
// Masonry: place each item into the shortest column so portrait/landscape alternate naturally
function runMasonry() {{
  const grid = document.getElementById('masonry-grid');
  const items = [...grid.children];
  if (!items.length) return;
  const gap = 8;
  const W = grid.offsetWidth;
  const cols = W < 500 ? 2 : W < 780 ? 3 : W < 1100 ? 4 : 5;
  const colW = (W - gap * (cols - 1)) / cols;
  const heights = new Array(cols).fill(0);
  grid.style.position = 'relative';
  items.forEach(item => {{
    item.style.width = colW + 'px';
    const c = heights.indexOf(Math.min(...heights));
    item.style.left = (c * (colW + gap)) + 'px';
    item.style.top = heights[c] + 'px';
    heights[c] += item.offsetHeight + gap;
  }});
  grid.style.height = Math.max(...heights) + 'px';
}}
let _masonryTimer;
function scheduleMasonry() {{ clearTimeout(_masonryTimer); _masonryTimer = setTimeout(runMasonry, 60); }}
// Run once each image loads so layout fills in progressively, then again on resize
document.querySelectorAll('#masonry-grid img').forEach(img => {{
  if (img.complete) scheduleMasonry();
  else img.addEventListener('load', scheduleMasonry);
}});
window.addEventListener('resize', scheduleMasonry);

let lbIdx = 0;
document.querySelectorAll('.pi img').forEach((img,i)=>{{
  img.parentElement.addEventListener('click',e=>{{
    if(e.target.tagName==='A')return;
    lbIdx = i;
    document.getElementById('lb-img').src='/package/{token}/medium/'+i;
    document.getElementById('lb-dl').href='/package/{token}/file/'+i;
    document.getElementById('lb').classList.add('open');
    prepareLbSave(i);
  }});
}});
function closeLb(){{document.getElementById('lb').classList.remove('open')}}

// ── Save to Photos ──
// Phones put normal downloads in the Files app, where many people never find them.
// The share sheet's "Save Images" puts them straight into Photos. Sharing has to
// start from a tap, so photos are fetched first, then a second tap opens the sheet.
const IS_MOBILE = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const SAVE_OK = IS_MOBILE && (() => {{
  try {{
    const f = new File([new Blob(['x'], {{type: 'image/jpeg'}})], 't.jpg', {{type: 'image/jpeg'}});
    return !!(navigator.canShare && navigator.canShare({{files: [f]}}));
  }} catch(e) {{ return false; }}
}})();
const BATCH = 10;          // photos per share sheet: keeps memory use reasonable on phones
let _links = null, _next = 0, _ready = null, _busy = false;
const $id = id => document.getElementById(id);

async function getLinks() {{
  if (!_links) {{
    const r = await fetch('/package/{token}/links');
    if (!r.ok) throw new Error('This link has expired');
    _links = (await r.json()).files;
  }}
  return _links;
}}

async function fetchFile(link) {{
  const r = await fetch(link.url);
  if (!r.ok) throw new Error('Could not load ' + link.name);
  return new File([await r.blob()], link.name, {{type: 'image/jpeg'}});
}}

async function fetchAll(links, onEach) {{
  const out = new Array(links.length);
  let i = 0, done = 0;
  async function worker() {{
    while (i < links.length) {{
      const k = i++;
      out[k] = await fetchFile(links[k]);
      onEach(++done);
    }}
  }}
  await Promise.all([worker(), worker(), worker(), worker()]);
  return out;
}}

function setSave(label, disabled, hint) {{
  const b = $id('save-photos');
  b.textContent = label; b.disabled = !!disabled;
  $id('save-hint').style.display = hint ? '' : 'none';
  $id('save-hint').textContent = hint || '';
}}

async function savePhotosClick() {{
  if (_busy) return;
  if (_ready) {{                                    // second tap: open the share sheet
    const files = _ready;
    try {{
      await navigator.share({{files}});
      _ready = null; _next += files.length;
      const left = _links.length - _next;
      if (left > 0) setSave(`Save next ${{Math.min(BATCH, left)}} photo${{left === 1 ? '' : 's'}}`, false,
                            `${{_next}} of ${{_links.length}} saved`);
      else setSave('All photos saved', true, 'They are in your Photos app.');
    }} catch(e) {{
      if (e.name !== 'AbortError') setSave('Save to Photos', false, "Saving didn't work here. Use Download All instead.");
    }}
    return;
  }}
  _busy = true;                                     // first tap: fetch the next batch
  try {{
    const links = (await getLinks()).slice(_next, _next + BATCH);
    setSave(`Preparing 0 of ${{links.length}}...`, true);
    _ready = await fetchAll(links, n => setSave(`Preparing ${{n}} of ${{links.length}}...`, true));
    setSave(`Tap to save ${{_ready.length}} photo${{_ready.length === 1 ? '' : 's'}}`, false,
            'Then choose "Save Images" (or "Save Image").');
  }} catch(e) {{
    setSave('Save to Photos', false, e.message + '. Please try again.');
  }} finally {{ _busy = false; }}
}}

// Single photo from the enlarged view
let _lbFile = null, _lbFor = -1;
async function prepareLbSave(i) {{
  if (!SAVE_OK) return;
  const b = $id('lb-save');
  b.style.display = ''; b.disabled = true; b.textContent = 'Preparing...';
  _lbFile = null; _lbFor = i;
  try {{
    const f = await fetchFile((await getLinks())[i]);
    if (_lbFor !== i) return;                       // moved on to another photo
    _lbFile = f; b.disabled = false; b.textContent = 'Save to Photos';
  }} catch(e) {{ b.textContent = 'Unavailable'; }}
}}
async function lbSaveClick() {{
  if (!_lbFile) return;
  try {{ await navigator.share({{files: [_lbFile]}}); }} catch(e) {{}}
}}

if (SAVE_OK) {{
  $id('save-photos').style.display = '';
  $id('dl-all').classList.add('alt');
  $id('dl-all').textContent = 'Download as zip';
}}
document.getElementById('lb').addEventListener('click',e=>{{if(e.target.id==='lb'||e.target.id==='lb-img')closeLb()}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeLb()}});
</script>
</body></html>""")


def _pkg_error(msg: str, status: int, sub: str = "") -> str:
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:2rem">
<div><h2 style="color:#{'c0392b' if status>=400 else 'F2C94C'};margin-bottom:.75rem">{msg}</h2>
{"<p style='color:rgba(255,255,255,.5);font-size:.9rem'>"+sub+"</p>" if sub else ""}
</div></body></html>"""


@app.get("/package/{token}/thumb/{idx}")
def package_thumb(token: str, idx: int):
    from fastapi.responses import StreamingResponse as SR
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    if idx < 0 or idx >= len(paths):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(paths[idx])
    try:
        # Package photos may be uploaded files rather than gallery photos: studio size only
        data = _cached_thumb(key, "admin") or _make_thumbs(key, ["admin"])["admin"]
        return SR(BytesIO(data), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/package/{token}/medium/{idx}")
def package_medium(token: str, idx: int):
    from fastapi.responses import StreamingResponse as SR
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    if idx < 0 or idx >= len(paths):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(paths[idx])
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        img = Image.open(obj["Body"]).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((1800, 1800), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        buf.seek(0)
        return SR(buf, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/package/{token}/file/{idx}")
def package_file(token: str, idx: int):
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    if idx < 0 or idx >= len(paths):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(paths[idx])
    presigned = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key,
                "ResponseContentDisposition": f"attachment; filename={os.path.basename(key)}"},
        ExpiresIn=3600,
    )
    return RedirectResponse(presigned)


@app.get("/package/{token}/links")
def package_links(token: str):
    """Short-lived direct links to the originals, so the page can fetch them for
    "Save to Photos" straight from storage (fast, and not through this server)."""
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    files = []
    for path in pkg.get("paths", []):
        key = to_r2_key(path)
        files.append({"name": os.path.basename(key),
                      "url": s3.generate_presigned_url("get_object", Params={"Bucket": R2_BUCKET, "Key": key},
                                                       ExpiresIn=3600)})
    return {"files": files}


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


@app.get("/package/{token}/all")
def package_all(token: str):
    pkg = _load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    safe_title = re.sub(r'[^\w\-_. ]', '', pkg.get("title", "photos"))[:60].strip().replace(" ", "_")
    filename = f"{safe_title}.zip"

    def _fetch(path):
        try:
            return path, s3.get_object(Bucket=R2_BUCKET, Key=to_r2_key(path))["Body"].read()
        except Exception as e:
            print(f"[zip] skipped {path}: {e}")
            return path, None

    def iter_zip():
        # Fetch a few photos ahead in parallel while earlier ones are being sent,
        # and send each one as soon as it's added (nothing piles up in memory)
        out = _ZipStream()
        zf = zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ahead = [pool.submit(_fetch, p) for p in paths[:4]]
            nxt = len(ahead)
            while ahead:
                path, data = ahead.pop(0).result()
                if nxt < len(paths):
                    ahead.append(pool.submit(_fetch, paths[nxt])); nxt += 1
                if data is not None:
                    zf.writestr(os.path.basename(path), data)
                    yield out.take()
        zf.close()
        rem = out.take()
        if rem: yield rem

    from fastapi.responses import StreamingResponse
    return StreamingResponse(iter_zip(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin/links", response_class=HTMLResponse)
def admin_links_page(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin?next=/admin/links")
    new_token = request.query_params.get("new", "")
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", new_token):
        new_token = ""            # only a real package id is ever written into the page
    new_url   = f"{SITE_URL}/package/{new_token}" if new_token else ""
    html = (open("templates/admin_links.html").read()
            .replace("__NEW_TOKEN__", new_token)
            .replace("__NEW_URL__", new_url))
    return HTMLResponse(html)


# ── Background startup work that needs every function above to exist ─────────
threading.Thread(target=_backfill_square_id_markers, daemon=True).start()
