from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import json
import torch
import re
from transformers import CLIPProcessor, CLIPModel
from rapidfuzz import fuzz
from datetime import datetime
import os
import base64
from io import BytesIO
from PIL import Image, ImageOps
from functools import lru_cache
import boto3
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

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

def image_to_base64(img, max_size=900):
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=83)
    return base64.b64encode(buf.getvalue()).decode()

# ─────────────────────────────────────────
# LOAD MODEL & DATA (once on startup)
# ─────────────────────────────────────────

model     = None
processor = None

def get_model():
    global model, processor
    if model is None:
        print("Loading CLIP model on first search request...")
        model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        print("Model loaded.")
    return model, processor

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
    previews = {}
    for item in items:
        display = clean_location(item["location"])
        loc_map[display] = item["location"]
        if display not in previews:
            previews[display] = []
        if len(previews[display]) < 4:
            previews[display].append(item["path"])

    locations = []
    for display in sorted(loc_map.keys()):
        locations.append({
            "name":     display,
            "previews": previews[display]
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
        if len(families[ln]) < 4:
            families[ln].append(item["path"])

    result = []
    for name in sorted(families.keys()):
        result.append({"name": name, "previews": families[name]})
    return {"families": result, "has_families": len(result) > 0}


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
    if not query and not last_name:
        return JSONResponse(status_code=400, content={"error": "Provide query or last_name"})

    # Text embedding
    text_embedding = None
    if query:
        m, p = get_model()
        inputs = p(text=[query], return_tensors="pt", padding=True)
        with torch.no_grad():
            text_embedding = m.get_text_features(**inputs)[0]

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

        # Score
        if text_embedding is not None:
            img_emb    = torch.tensor(item["embedding"])
            similarity = torch.cosine_similarity(text_embedding, img_emb, dim=0).item()
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
def get_photo(path: str):
    from fastapi.responses import StreamingResponse as SR
    try:
        key = to_r2_key(path)
        if os.getenv("R2_ENDPOINT_URL"):
            obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
            return SR(obj["Body"], media_type="image/jpeg",
                      headers={"Cache-Control": "private, max-age=3600"})
        elif os.path.exists(path):
            img = Image.open(path).convert("RGB")
            img = fix_orientation(img)
            buf = BytesIO()
            img.thumbnail((900, 900), Image.LANCZOS)
            img.save(buf, format="JPEG", quality=83)
            buf.seek(0)
            return SR(buf, media_type="image/jpeg")
        else:
            return JSONResponse(status_code=404, content={"error": "File not found"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("templates/index.html").read())
