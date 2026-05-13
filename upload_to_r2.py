import os
import boto3
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
)

BUCKET    = os.getenv("R2_BUCKET_NAME")
BASE_DIR  = "images"

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

new_paths = [p for p in image_paths if p not in existing]
print(f"Already uploaded: {len(existing)} | New to upload: {len(new_paths)}")

if not new_paths:
    print("Nothing new to upload.")
    exit(0)

for local_path in tqdm(new_paths):
    ext = local_path.lower().rsplit(".", 1)[-1]
    content_type = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    s3.upload_file(
        local_path,
        BUCKET,
        local_path,  # uses same path as R2 key
        ExtraArgs={"ContentType": content_type}
    )

print(f"Done. Uploaded {len(new_paths)} photos to R2.")
