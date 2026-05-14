import os
import torch
import json
from PIL import Image
from tqdm import tqdm
from transformers import CLIPProcessor, CLIPModel

BASE_DIR   = "images"
INDEX_FILE = "images.json"

# Portrait folders are found by last name — no CLIP needed
SKIP_CLIP_IF = "portrait"

# Lazy-load model only if needed
model     = None
processor = None

def get_model():
    global model, processor
    if model is None:
        print("Loading CLIP model...")
        model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        print("Model loaded.")
    return model, processor

# -----------------------
# LOAD EXISTING INDEX
# -----------------------
if os.path.exists(INDEX_FILE):
    with open(INDEX_FILE, "r") as f:
        results = json.load(f)
    already_indexed = {item["path"] for item in results}
    print(f"Already indexed: {len(results)} photos")
else:
    results = []
    already_indexed = set()

# -----------------------
# SCAN FOLDERS
# -----------------------
image_paths = []
for root, dirs, files in os.walk(BASE_DIR):
    for file in files:
        if file.lower().endswith((".jpg", ".jpeg", ".png")):
            full_path = os.path.abspath(os.path.join(root, file))
            if full_path not in already_indexed:
                image_paths.append(full_path)

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
        # Folder structure: {date}/{activity}/image.jpg
        #                or {date}/{activity}/{last_name}/image.jpg
        rel_path  = os.path.relpath(img_path, BASE_DIR)
        parts     = rel_path.split(os.sep)
        date      = parts[0] if len(parts) > 0 else "unknown"
        activity  = parts[1] if len(parts) > 1 else "unknown"
        last_name = parts[2] if len(parts) > 3 else ""

        # Skip CLIP for portrait/family folders — they're searched by last name
        if SKIP_CLIP_IF in activity.lower():
            embedding = None
            skipped_clip += 1
        else:
            m, p = get_model()
            image = Image.open(img_path).convert("RGB")
            inputs = p(images=image, return_tensors="pt")
            with torch.no_grad():
                embedding = m.get_image_features(**inputs)[0].tolist()

        results.append({
            "path":      img_path,
            "date":      date,
            "location":  activity,
            "last_name": last_name,
            "embedding": embedding
        })

    except Exception as e:
        print(f"Error processing {img_path}: {e}")

# -----------------------
# SAVE JSON
# -----------------------
with open(INDEX_FILE, "w") as f:
    json.dump(results, f)

print(f"Done. Added {len(image_paths)} new photos.")
print(f"Total in index: {len(results)}")
print(f"Saved to {INDEX_FILE}")