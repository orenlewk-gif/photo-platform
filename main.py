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

app = FastAPI()

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

print("Loading CLIP model...")
model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
print("Model loaded.")

with open("images.json", "r") as f:
    data = json.load(f)
print(f"Loaded {len(data)} photos.")


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

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
    for item in items:
        display = clean_location(item["location"])
        loc_map[display] = item["location"]
    return {"locations": sorted(loc_map.keys())}


@app.get("/api/browse")
def browse(date: str, location: str):
    pool = [item for item in data
            if item["date"] == date
            and clean_location(item["location"]) == location]
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
        inputs = processor(text=[query], return_tensors="pt", padding=True)
        with torch.no_grad():
            text_embedding = model.get_text_features(**inputs)[0]

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

    results.sort(reverse=True, key=lambda x: x[0])

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


@app.get("/api/photo")
def get_photo(path: str):
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    try:
        img = Image.open(path).convert("RGB")
        img = fix_orientation(img)
        b64 = image_to_base64(img)
        return {"b64": b64}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("templates/index.html").read())
