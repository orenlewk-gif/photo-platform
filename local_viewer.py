"""
local_viewer.py
---------------
Standalone local photo viewer for the storefront.
No watermarks. No cart. No internet required.

Run:
    python3 local_viewer.py

Then open http://localhost:8080 in a browser.
Photos are served directly from the local `images/` folder.
"""

import os
import re
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
import uvicorn

app = FastAPI()

IMAGES_DIR = Path("images")
THUMB_SIZE  = 600   # px — larger than the online version since no bandwidth concern
TEMPLATES   = Jinja2Templates(directory="templates")

# ── Helpers ──────────────────────────────────────────────────────────────────

def clean_name(raw: str) -> str:
    """adventure-zip-line → Adventure Zip Line"""
    return re.sub(r"[-_]+", " ", raw).strip().title()

def fix_orientation(img):
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img

def list_dates():
    """Return sorted list of date strings (newest first)."""
    if not IMAGES_DIR.exists():
        return []
    return sorted(
        [d.name for d in IMAGES_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")],
        reverse=True,
    )

def list_locations(date: str):
    """Return locations for a given date."""
    day_dir = IMAGES_DIR / date
    if not day_dir.exists():
        return []
    return sorted(
        [clean_name(d.name) for d in day_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
    )

def find_location_dir(date: str, location: str):
    """Find the actual folder matching a cleaned location name."""
    day_dir = IMAGES_DIR / date
    if not day_dir.exists():
        return None
    for d in day_dir.iterdir():
        if d.is_dir() and clean_name(d.name).lower() == location.lower():
            return d
    return None

def list_photos(date: str, location: str):
    """Return sorted list of photo file paths for a date+location."""
    loc_dir = find_location_dir(date, location)
    if not loc_dir:
        return []
    exts = {".jpg", ".jpeg", ".png"}
    return sorted(
        [str(p) for p in loc_dir.iterdir() if p.suffix.lower() in exts],
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/viewer.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/local/dates")
async def api_dates():
    dates = list_dates()
    result = []
    for d in dates:
        result.append({
            "date": d,
            "locations": list_locations(d),
        })
    return result


@app.get("/api/local/browse")
async def api_browse(date: str = Query(...), location: str = Query(...)):
    photos = list_photos(date, location)
    return {
        "date": date,
        "location": location,
        "photos": [{"path": p, "filename": Path(p).name} for p in photos],
    }


@app.get("/api/local/photo")
async def api_photo(path: str = Query(...), size: str = Query("thumb")):
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)
    # Security: must be inside images dir
    try:
        file_path.resolve().relative_to(IMAGES_DIR.resolve())
    except ValueError:
        return Response(status_code=403)

    try:
        img = Image.open(file_path).convert("RGB")
        img = fix_orientation(img)
        if size == "thumb":
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            quality = 82
        else:
            # Full size — still cap at 2400px to keep it snappy on screen
            img.thumbnail((2400, 2400), Image.LANCZOS)
            quality = 92
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        buf.seek(0)
        cache = "private, max-age=3600"
        return Response(buf.read(), media_type="image/jpeg",
                        headers={"Cache-Control": cache})
    except Exception as e:
        return Response(status_code=500)


if __name__ == "__main__":
    print("\n  📷  Crystal Images — Local Viewer")
    print("  ──────────────────────────────────")
    print("  Open http://localhost:8080 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
