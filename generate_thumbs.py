"""
generate_thumbs.py
------------------
Pre-generates watermarked thumbnails for all photos in images.json and
uploads them to R2 at the "thumbs/" prefix.

Run once to back-fill existing photos:
    python3 generate_thumbs.py

Skip photos that already have a thumb in R2 (safe to re-run).
"""

import os
import json
from io import BytesIO

import boto3
from dotenv import load_dotenv
from PIL import Image, ImageOps
from tqdm import tqdm

load_dotenv()

# ── R2 client ──────────────────────────────────────────────────────────────
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
)
BUCKET = os.getenv("R2_BUCKET_NAME", "crystal-images")

THUMB_MAX_PX  = 450
THUMB_QUALITY = 72
WATERMARK_OPACITY = 0.45

# ── Load watermark ──────────────────────────────────────────────────────────
def load_watermark():
    if not os.path.exists("watermark.png"):
        print("No watermark.png found — thumbs will be generated without watermark.")
        return None
    wm = Image.open("watermark.png").convert("RGBA")
    wm.thumbnail((500, 500), Image.LANCZOS)
    w, h = wm.size
    px = wm.load()
    THRESH = 230

    # Flood fill outer white background → transparent
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
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in bg:
                r, g, b, a = px[nx, ny]
                if r > THRESH and g > THRESH and b > THRESH:
                    bg.add((nx, ny)); queue.append((nx, ny))

    # Remove large enclosed white regions (donut hole)
    seen = set()
    hole_threshold = (w * h) * 0.005
    for sy in range(h):
        for sx in range(w):
            if (sx, sy) in seen:
                continue
            r, g, b, a = px[sx, sy]
            if not (r > THRESH and g > THRESH and b > THRESH):
                continue
            region = [(sx, sy)]
            visited = {(sx, sy)}
            stack = [(sx, sy)]
            while stack:
                cx, cy = stack.pop()
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in visited:
                        r2, g2, b2, a2 = px[nx, ny]
                        if r2 > THRESH and g2 > THRESH and b2 > THRESH:
                            visited.add((nx, ny))
                            stack.append((nx, ny))
                            region.append((nx, ny))
            seen.update(visited)
            if len(region) > hole_threshold:
                for rx, ry in region:
                    px[rx, ry] = (px[rx, ry][0], px[rx, ry][1], px[rx, ry][2], 0)

    data = list(wm.getdata())
    wm.putdata([(r, g, b, int(a * WATERMARK_OPACITY)) for r, g, b, a in data])
    print(f"Watermark loaded ({w}x{h})")
    return wm


def apply_watermark(img, wm):
    if wm is None:
        return img
    img_rgba = img.convert("RGBA")
    wm_size = int(min(img.width, img.height) * 0.60)
    wm_scaled = wm.resize((wm_size, wm_size), Image.LANCZOS)
    x = (img.width  - wm_size) // 2
    y = (img.height - wm_size) // 2
    img_rgba.paste(wm_scaled, (x, y), wm_scaled)
    return img_rgba.convert("RGB")


def fix_orientation(img):
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def to_r2_key(path):
    idx = path.find("images/")
    return path[idx:] if idx >= 0 else path


def thumb_key(r2_key):
    """maps images/foo/bar.jpg → thumbs/foo/bar.jpg"""
    if r2_key.startswith("images/"):
        return "thumbs/" + r2_key[len("images/"):]
    return "thumbs/" + r2_key


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    with open("images.json") as f:
        data = json.load(f)

    paths = list({item["path"] for item in data})
    print(f"Found {len(paths)} unique photos in images.json")

    # Check which thumbs already exist in R2
    print("Checking existing thumbs in R2...")
    existing_thumbs = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix="thumbs/"):
        for obj in page.get("Contents", []):
            existing_thumbs.add(obj["Key"])
    print(f"  {len(existing_thumbs)} thumbs already in R2")

    to_process = [p for p in paths if thumb_key(to_r2_key(p)) not in existing_thumbs]
    print(f"  {len(to_process)} thumbs to generate\n")

    if not to_process:
        print("All thumbs already generated. Nothing to do.")
        return

    wm = load_watermark()
    errors = []

    for path in tqdm(to_process, desc="Generating thumbs"):
        try:
            r2_key = to_r2_key(path)
            tkey   = thumb_key(r2_key)

            # Fetch original from R2
            obj = s3.get_object(Bucket=BUCKET, Key=r2_key)
            img = Image.open(obj["Body"]).convert("RGB")
            img = fix_orientation(img)
            img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
            img = apply_watermark(img, wm)

            buf = BytesIO()
            img.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
            buf.seek(0)

            s3.put_object(
                Bucket=BUCKET,
                Key=tkey,
                Body=buf,
                ContentType="image/jpeg",
                CacheControl="public, max-age=31536000",
            )
        except Exception as e:
            errors.append((path, str(e)))

    print(f"\nDone. Generated {len(to_process) - len(errors)} thumbs.")
    if errors:
        print(f"{len(errors)} errors:")
        for p, e in errors:
            print(f"  {p}: {e}")


if __name__ == "__main__":
    main()
