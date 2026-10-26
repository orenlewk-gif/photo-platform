"""READ-ONLY: each labeled photo's real capture time and camera, from the first 128 KB of the file
(folder dates are placeholders). Writes capture_times.json: {path: [ISO time or None, camera]}."""
import io, json, os, time
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
                  config=Config(max_pool_connections=40, retries={"max_attempts": 5}))
BUCKET = os.environ["R2_BUCKET_NAME"]


def read(path):
    try:
        head = s3.get_object(Bucket=BUCKET, Key=path, Range="bytes=0-131071")["Body"].read()
        ex = Image.open(io.BytesIO(head)).getexif()
        sub = ex.get_ifd(0x8769)                                  # Exif sub-block: capture time, subseconds
        when = sub.get(36867) or ex.get(306)                      # DateTimeOriginal, else DateTime
        frac = str(sub.get(37521) or "").strip()                  # SubSecTimeOriginal
        serial = sub.get(42033) or ""                             # BodySerialNumber
        cam = f"{ex.get(272, '')} {serial}".strip()               # Model + serial
        if when:
            when = when.strip().replace(":", "-", 2).replace(" ", "T") + (f".{frac}" if frac else "")
        return path, [when, cam]
    except Exception as e:
        return path, [None, f"error {str(e)[:60]}"]


data = json.loads(s3.get_object(Bucket=BUCKET, Key="images.json")["Body"].read())
paths = [d["path"] for d in data if d.get("jacket_colors") and d.get("date", "") <= "2026-06-12"]
t0 = time.time()
with ThreadPoolExecutor(32) as ex:
    out = dict(ex.map(read, paths))
json.dump(out, open(os.path.join(HERE, "capture_times.json"), "w"))
print(f"{len(out)} photos in {(time.time()-t0)/60:.1f} min; with a capture time: {sum(1 for v in out.values() if v[0])}")
