import os
from io import BytesIO

import boto3
from dotenv import load_dotenv
from tqdm import tqdm
from PIL import Image, ImageOps

load_dotenv()

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
)

BUCKET        = os.getenv("R2_BUCKET_NAME")
BASE_DIR      = "images"
THUMB_MAX_PX  = 450
THUMB_QUALITY = 72


def fix_orientation(img):
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def thumb_key(r2_key):
    """images/foo/bar.jpg  →  thumbs/foo/bar.jpg"""
    if r2_key.startswith("images/"):
        return "thumbs/" + r2_key[len("images/"):]
    return "thumbs/" + r2_key


def generate_and_upload_thumb(local_path, r2_key):
    """Generate a watermarked thumbnail and upload to R2."""
    try:
        img = Image.open(local_path).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)

        # Apply watermark if available
        if os.path.exists("watermark.png"):
            from generate_thumbs import load_watermark, apply_watermark
            wm = load_watermark()
            img = apply_watermark(img, wm)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        buf.seek(0)

        s3.put_object(
            Bucket=BUCKET,
            Key=thumb_key(r2_key),
            Body=buf,
            ContentType="image/jpeg",
            CacheControl="public, max-age=31536000",
        )
    except Exception as e:
        print(f"  Warning: could not generate thumb for {r2_key}: {e}")


# Collect all images
image_paths = []
for root, dirs, files in os.walk(BASE_DIR):
    for file in files:
        if file.lower().endswith((".jpg", ".jpeg", ".png")):
            image_paths.append(os.path.join(root, file))

print(f"Found {len(image_paths)} photos to upload...")

# Check what's already in R2
existing = set()
paginator = s3.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=BUCKET, Prefix="images/"):
    for obj in page.get("Contents", []):
        existing.add(obj["Key"])

# Check which thumbs already exist
existing_thumbs = set()
for page in paginator.paginate(Bucket=BUCKET, Prefix="thumbs/"):
    for obj in page.get("Contents", []):
        existing_thumbs.add(obj["Key"])

new_paths = [p for p in image_paths if p not in existing]
print(f"Already uploaded: {len(existing)} | New to upload: {len(new_paths)}")

if not new_paths:
    print("Nothing new to upload.")
    exit(0)

for local_path in tqdm(new_paths, desc="Uploading"):
    ext = local_path.lower().rsplit(".", 1)[-1]
    content_type = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"

    # Upload original
    s3.upload_file(
        local_path,
        BUCKET,
        local_path,
        ExtraArgs={"ContentType": content_type},
    )

    # Generate + upload thumb if not already present
    tkey = thumb_key(local_path)
    if tkey not in existing_thumbs:
        generate_and_upload_thumb(local_path, local_path)

print(f"Done. Uploaded {len(new_paths)} photos (+ thumbnails) to R2.")
