#!/usr/bin/env python3
"""
watch_photos.py — run this on your local machine while adding or organizing photos.

It watches the images/ folder and, within a few seconds of any change:
  • Uploads new/moved photos to R2
  • Re-indexes them (CLIP embedding or last-name only for portraits)
  • Pushes the updated images.json to R2
  • Calls /api/reload so the live site picks up the changes instantly

Usage:
    python3 watch_photos.py

Stop with Ctrl+C.
"""

import os, json, time, re, threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image
import torch
import boto3
import requests as http_requests
from dotenv import load_dotenv

load_dotenv()

BASE_DIR     = "images"
INDEX_FILE   = "images.json"
SKIP_CLIP_IF = "portrait"
EXTENSIONS   = {".jpg", ".jpeg", ".png"}
DEBOUNCE     = 3.0   # seconds to wait after last event before flushing

SITE_URL     = os.getenv("SITE_URL", "https://photos.bigskyphotos.com")
RELOAD_TOKEN = os.getenv("RELOAD_TOKEN", "")

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
)
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "crystal-images")

# ── CLIP model (lazy) ─────────────────────────────────────────────────────────

_model = None
_proc  = None

def get_model():
    global _model, _proc
    if _model is None:
        from transformers import CLIPModel, CLIPProcessor
        print("Loading CLIP model...")
        _model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        _proc  = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        print("Model loaded.")
    return _model, _proc

# ── index helpers ─────────────────────────────────────────────────────────────

_lock = threading.Lock()

def load_index():
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE) as f:
            return json.load(f)
    return []

def save_index(results):
    with open(INDEX_FILE, "w") as f:
        json.dump(results, f)

def push_index():
    s3.upload_file(INDEX_FILE, R2_BUCKET, "images.json",
                   ExtraArgs={"ContentType": "application/json"})

def reload_site():
    url = f"{SITE_URL}/api/reload"
    params = {"token": RELOAD_TOKEN} if RELOAD_TOKEN else {}
    try:
        r = http_requests.post(url, params=params, timeout=10)
        print(f"  site reload → HTTP {r.status_code}")
    except Exception as e:
        print(f"  site reload failed: {e}")

# ── R2 helpers ────────────────────────────────────────────────────────────────

def to_r2_key(abs_path):
    rel = os.path.relpath(abs_path, start=os.getcwd())
    idx = rel.find("images" + os.sep)
    key = rel[idx:] if idx >= 0 else rel
    return key.replace(os.sep, "/")

def upload_photo(abs_path):
    key = to_r2_key(abs_path)
    s3.upload_file(abs_path, R2_BUCKET, key)
    print(f"  uploaded → {key}")
    return key

def delete_from_r2(abs_path):
    key = to_r2_key(abs_path)
    try:
        s3.delete_object(Bucket=R2_BUCKET, Key=key)
        print(f"  deleted  → {key}")
    except Exception as e:
        print(f"  R2 delete failed ({key}): {e}")

# ── metadata ──────────────────────────────────────────────────────────────────

def extract_meta(abs_path):
    rel   = os.path.relpath(abs_path, start=os.path.abspath(BASE_DIR))
    parts = rel.split(os.sep)
    date      = parts[0] if len(parts) > 0 else "unknown"
    activity  = parts[1] if len(parts) > 1 else "unknown"
    last_name = parts[2] if len(parts) > 3 else ""
    return date, activity, last_name

def compute_embedding(abs_path, activity):
    if SKIP_CLIP_IF in activity.lower():
        return None
    m, p = get_model()
    img  = Image.open(abs_path).convert("RGB")
    with torch.no_grad():
        return m.get_image_features(**p(images=img, return_tensors="pt"))[0].tolist()

def make_entry(abs_path):
    date, activity, last_name = extract_meta(abs_path)
    return {
        "path":      abs_path,
        "date":      date,
        "location":  activity,
        "last_name": last_name,
        "embedding": compute_embedding(abs_path, activity),
    }

# ── debounced flush ───────────────────────────────────────────────────────────

_adds    = set()   # abs paths of new/modified images
_deletes = set()   # abs paths removed
_moves   = {}      # old_abs -> new_abs
_dir_moves = []    # (old_dir_abs, new_dir_abs)
_timer   = None

def _schedule():
    global _timer
    if _timer:
        _timer.cancel()
    _timer = threading.Timer(DEBOUNCE, _flush)
    _timer.daemon = True
    _timer.start()

def _flush():
    with _lock:
        adds      = set(_adds);      _adds.clear()
        deletes   = set(_deletes);   _deletes.clear()
        moves     = dict(_moves);    _moves.clear()
        dir_moves = list(_dir_moves); _dir_moves.clear()

    if not adds and not deletes and not moves and not dir_moves:
        return

    print(f"\n── syncing: +{len(adds)} add(s)  -{len(deletes)} delete(s)"
          f"  ↔{len(moves)} move(s)  📁{len(dir_moves)} folder move(s) ──")

    results  = load_index()
    by_path  = {item["path"]: item for item in results}

    # ── folder moves ──────────────────────────────────────────────────────────
    for old_dir, new_dir in dir_moves:
        affected = [p for p in list(by_path) if p.startswith(old_dir)]
        print(f"  folder move: {len(affected)} photos")
        for old_abs in affected:
            new_abs = new_dir + old_abs[len(old_dir):]
            delete_from_r2(old_abs)
            if os.path.exists(new_abs):
                upload_photo(new_abs)
                by_path.pop(old_abs)
                by_path[new_abs] = make_entry(new_abs)

    # ── file moves ────────────────────────────────────────────────────────────
    for old_abs, new_abs in moves.items():
        delete_from_r2(old_abs)
        by_path.pop(old_abs, None)
        if os.path.exists(new_abs):
            upload_photo(new_abs)
            by_path[new_abs] = make_entry(new_abs)

    # ── new / modified files ──────────────────────────────────────────────────
    for abs_path in adds:
        if not os.path.exists(abs_path):
            continue
        upload_photo(abs_path)
        by_path[abs_path] = make_entry(abs_path)

    # ── deleted files ─────────────────────────────────────────────────────────
    for abs_path in deletes:
        by_path.pop(abs_path, None)
        # photos are NOT auto-deleted from R2 to avoid accidental data loss

    updated = list(by_path.values())
    save_index(updated)
    push_index()
    reload_site()
    print(f"── done. index: {len(updated)} photos ──\n")

# ── watchdog handler ──────────────────────────────────────────────────────────

def _is_image(path):
    return Path(path).suffix.lower() in EXTENSIONS

class PhotoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or not _is_image(event.src_path):
            return
        _adds.add(os.path.abspath(event.src_path))
        _schedule()

    def on_modified(self, event):
        if event.is_directory or not _is_image(event.src_path):
            return
        _adds.add(os.path.abspath(event.src_path))
        _schedule()

    def on_moved(self, event):
        old = os.path.abspath(event.src_path)
        new = os.path.abspath(event.dest_path)
        if event.is_directory:
            _dir_moves.append((old + os.sep, new + os.sep))
        elif _is_image(event.dest_path):
            _moves[old] = new
        _schedule()

    def on_deleted(self, event):
        if event.is_directory or not _is_image(event.src_path):
            return
        abs_path = os.path.abspath(event.src_path)
        _deletes.add(abs_path)
        _adds.discard(abs_path)
        _schedule()


if __name__ == "__main__":
    watch_path = os.path.abspath(BASE_DIR)
    print(f"Watching {watch_path}")
    print("Drop photos in, move folders around — changes sync to the site within a few seconds.")
    print("Press Ctrl+C to stop.\n")

    handler  = PhotoHandler()
    observer = Observer()
    observer.schedule(handler, watch_path, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nStopped.")
    observer.join()
