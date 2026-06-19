import os
import torch
import json
from PIL import Image
from tqdm import tqdm
from transformers import CLIPProcessor, CLIPModel
import boto3
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

BASE_DIR   = "images"
INDEX_FILE = "images.json"

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
)
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "crystal-images")

# Portrait locations use last-name sub-folders and don't need CLIP
PORTRAIT_LOCATIONS = ["lone peak portraits", "explorer gondola", "ramcharger portraits"]

# Lazy-load model only if needed
model     = None
processor = None
device    = "mps" if torch.backends.mps.is_available() else "cpu"

def get_model():
    global model, processor
    if model is None:
        print(f"Loading CLIP model on {device}...")
        model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(device)
        processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        print("Model loaded.")
    return model, processor

# -----------------------
# LOAD EXISTING INDEX — R2 is authoritative
# -----------------------
results = []
try:
    obj = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
    results = json.loads(obj["Body"].read().decode("utf-8"))
    with open(INDEX_FILE, "w") as f:
        json.dump(results, f)
    print(f"Loaded {len(results)} photos from R2 (authoritative)")
except Exception:
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r") as f:
            results = json.load(f)
        print(f"Loaded {len(results)} photos from local fallback")
    else:
        print("No existing index found, starting fresh")
already_indexed = {item["path"] for item in results}

# -----------------------
# SYNC FROM R2 → LOCAL (photos published via web upload)
# -----------------------
def to_r2_key(abs_path):
    rel = os.path.relpath(abs_path, start=os.getcwd())
    idx = rel.find("images" + os.sep)
    key = rel[idx:] if idx >= 0 else rel
    return key.replace(os.sep, "/")


# -----------------------
# SCAN FOLDERS + UPLOAD NEW PHOTOS TO R2
# -----------------------
image_paths = []
for root, dirs, files in os.walk(BASE_DIR):
    for file in files:
        if file.lower().endswith((".jpg", ".jpeg", ".png")):
            full_path = os.path.abspath(os.path.join(root, file))
            if full_path not in already_indexed:
                image_paths.append(full_path)

# Upload any new photos to R2 before indexing (parallel for speed)
if image_paths and os.getenv("R2_ENDPOINT_URL"):
    print(f"Uploading {len(image_paths)} photo(s) to R2...")
    def upload_one(img_path):
        try:
            s3.upload_file(img_path, R2_BUCKET, to_r2_key(img_path))
            return None
        except Exception as e:
            return f"Upload failed {img_path}: {e}"
    with tqdm(total=len(image_paths), desc="Uploading") as pbar:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(upload_one, p): p for p in image_paths}
            for f in as_completed(futures):
                err = f.result()
                if err:
                    print(err)
                pbar.update(1)

print(f"New photos to index: {len(image_paths)}")

if not image_paths:
    print("Nothing new to index.")
    exit(0)

# -----------------------
# PROCESS IMAGES
# -----------------------
skipped_clip = 0
for img_path in tqdm(image_paths):
    try:
        # Folder structure:
        #   {date}/{location}/image.jpg                        (activity, no sub-folder)
        #   {date}/{location}/{last_name}/image.jpg            (portrait — Lone Peak, Explorer Gondola)
        #   {date}/{location}/{group}/image.jpg                (activity with groups — Mountain Biking, Zip Lines)
        rel_path  = os.path.relpath(img_path, BASE_DIR)
        parts     = rel_path.split(os.sep)
        date      = parts[0] if len(parts) > 0 else "unknown"
        activity  = parts[1] if len(parts) > 1 else "unknown"

        activity_normalized = activity.lower().replace("-", " ").replace("_", " ")
        is_portrait = any(p in activity_normalized for p in PORTRAIT_LOCATIONS)

        if len(parts) > 3:          # sub-folder present
            if is_portrait:
                last_name = parts[2]
                group     = ""
            else:
                group     = parts[2]
                last_name = ""
        else:
            last_name = ""
            group     = ""

        # Skip CLIP for portrait locations — searched by last name only
        if is_portrait:
            embedding = None
            skipped_clip += 1
        else:
            m, p = get_model()
            image = Image.open(img_path).convert("RGB")
            inputs = {k: v.to(device) for k, v in p(images=image, return_tensors="pt").items()}
            with torch.no_grad():
                embedding = m.get_image_features(**inputs)[0].tolist()

        results.append({
            "path":      img_path,
            "date":      date,
            "location":  activity,
            "last_name": last_name,
            "group":     group,
            "embedding": embedding
        })

    except Exception as e:
        print(f"Error processing {img_path}: {e}")

# -----------------------
# HEAL: add any R2 images/ entries missing from index (web-uploaded go-live photos)
# -----------------------
PORTRAIT_LOCATIONS_SET = set(PORTRAIT_LOCATIONS)
existing_paths = {item["path"] for item in results}
healed = 0
try:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="images/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            parts = key.split("/")
            if len(parts) < 4:
                continue
            r2_date, r2_loc = parts[1], parts[2]
            r2_loc_norm = r2_loc.lower()
            r2_is_portrait = any(p in r2_loc_norm for p in PORTRAIT_LOCATIONS_SET)
            r2_last_name = parts[3] if len(parts) > 4 and r2_is_portrait else ""
            r2_group     = parts[3] if len(parts) > 4 and not r2_is_portrait else ""
            canonical_path = os.path.join(os.getcwd(), key.replace("/", os.sep))
            if canonical_path in existing_paths:
                continue
            if any(item.get("path", "").endswith(key.replace("images/", "", 1)) for item in results):
                continue
            results.append({
                "path":      canonical_path,
                "date":      r2_date,
                "location":  r2_loc,
                "last_name": r2_last_name,
                "group":     r2_group,
                "embedding": None,
            })
            existing_paths.add(canonical_path)
            healed += 1
    if healed:
        print(f"Healed {healed} missing R2 photos back into index.")
except Exception as e:
    print(f"Heal step failed (non-fatal): {e}")

# -----------------------
# SAVE JSON + PUSH TO R2
# -----------------------
with open(INDEX_FILE, "w") as f:
    json.dump(results, f)

print(f"Done. Added {len(image_paths)} new photos.")
print(f"Total in index: {len(results)}")
print(f"Saved to {INDEX_FILE}")

if os.getenv("R2_ENDPOINT_URL"):
    print("Uploading images.json to R2...")
    s3.upload_file(INDEX_FILE, R2_BUCKET, "images.json",
                   ExtraArgs={"ContentType": "application/json"})
    print("images.json pushed to R2.")
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for url in [os.getenv("SITE_URL", "https://photos.bigskyphotos.com"), "http://localhost:8080"]:
        try:
            req = urllib.request.Request(f"{url}/api/reload", method="POST")
            urllib.request.urlopen(req, timeout=5, context=ctx if url.startswith("https") else None)
            print(f"Reloaded: {url}")
        except Exception as e:
            print(f"Reload failed for {url}: {e}")