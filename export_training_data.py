"""
Export labels.csv from R2 images.json for model fine-tuning.

Produces:
  training_data/
    labels.csv   — r2_key, jacket_colors, pants_colors, sport, classifier, manually_corrected

Images are downloaded directly in Colab from R2 — no local download needed.

Run from the photo-platform directory:
  python3 export_training_data.py
"""

import json
import os
import csv
import boto3
from dotenv import load_dotenv
from pathlib import Path
from collections import Counter

from core.text import folder_key

load_dotenv()

R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT_URL      = os.getenv("R2_ENDPOINT_URL")
R2_BUCKET            = os.getenv("R2_BUCKET_NAME")

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
)

# Exam day: these photos are kept OUT of training so every model version is graded on the
# same never-seen photos (dev/model_eval/exam_photos.json). The whole day is skipped, not just
# the exam photos, because burst sequences would otherwise leak near-copies into training.
EXAM_DAYS = {"2026-06-07"}

OUT_DIR    = Path("training_data")
LABELS_CSV = OUT_DIR / "labels.csv"
OUT_DIR.mkdir(exist_ok=True)


def load_images_json():
    print("Downloading images.json from R2...")
    obj = s3.get_object(Bucket=R2_BUCKET, Key="images.json")
    return json.loads(obj["Body"].read().decode())


def load_reviewed_folders():
    """Folder keys marked done in Label Photos: only labels Oren has checked go into training
    (photos the model labeled at upload stay out until their folder is marked done)."""
    try:
        meta = json.loads(s3.get_object(Bucket=R2_BUCKET, Key="meta/folder_meta.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        return set()
    return {k for k, v in meta.items() if isinstance(v, dict) and v.get("reviewed")}


def folder_of(item):
    # The same folder key Label Photos marks done (routes/admin_photos.py api_admin_folders)
    return folder_key(item.get("date", ""), item.get("location", "").strip(),
                      (item.get("last_name") or item.get("group") or "").strip())


REVIEWED = set()


def is_exportable(item):
    if item.get("date") in EXAM_DAYS:
        return False
    if folder_of(item) not in REVIEWED:
        return False
    if item.get("jacket_colors") is None:
        return False
    if item.get("jacket_colors") == []:
        return False
    location = item.get("location", "").strip().lower()
    if location in {"lone peak portraits", "portrait"}:
        return False
    return True


def main():
    data = load_images_json()
    REVIEWED.update(load_reviewed_folders())
    exportable = [it for it in data if is_exportable(it)]
    print(f"Total photos: {len(data)} | Exportable: {len(exportable)}")
    print(f"Skipped exam day(s) {sorted(EXAM_DAYS)}: {sum(1 for it in data if it.get('date') in EXAM_DAYS)} photos")
    unreviewed = [it for it in data if it.get("date") not in EXAM_DAYS and it.get("jacket_colors")
                  and folder_of(it) not in REVIEWED]
    print(f"Left out (labeled, but folder not marked done): {len(unreviewed)} photos in "
          f"{len({folder_of(it) for it in unreviewed})} folders")

    rows = []
    for item in exportable:
        rows.append({
            "r2_key":             item["path"],
            "jacket_colors":      "|".join(item.get("jacket_colors") or []),
            "pants_colors":       "|".join(item.get("pants_colors") or []),
            "sport":              item.get("sport") or "",
            "classifier":         item.get("classifier") or "haiku",
            "manually_corrected": "1" if item.get("manually_corrected") else "0",
        })

    with open(LABELS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "r2_key", "jacket_colors", "pants_colors",
            "sport", "classifier", "manually_corrected",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done. {len(rows)} rows written to {LABELS_CSV}")
    print(f"Manually corrected: {sum(1 for r in rows if r['manually_corrected'] == '1')}")
    print("Classifier breakdown:")
    for k, v in Counter(r["classifier"] for r in rows).items():
        print(f"  {k}: {v}")
    print(f"Sports: {dict(Counter(r['sport'] for r in rows))}")


if __name__ == "__main__":
    main()
