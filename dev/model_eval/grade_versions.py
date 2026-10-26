"""READ-ONLY grading of two model versions on the same photos (Oren's OK, 2026-10-25).

Each labeled photo from the exam day (6/7, never trained on) and the progress-check day
(6/8) is streamed from storage into memory once (nothing saved to disk or written to storage)
and run through v3 exactly as the site prepares photos today, and through v4 exactly as it
would on the site (upright, padded square). Raw scores go to results_versions.jsonl; the report
(report_versions.py) tunes v4's rules on 6/8 and grades both versions on 6/7.

Run with the storage keys in the environment:
  R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET_NAME=... python grade_versions.py
"""
import io, json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import boto3
import numpy as np
import onnxruntime as ort
from botocore.config import Config
from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
V3 = os.path.expanduser("~/Desktop/photo-platform/model_v3.onnx")
V4 = os.path.expanduser("~/Desktop/v4-training/model_v4.onnx")
V4_META = json.load(open(os.path.expanduser("~/Desktop/v4-training/model_v4_meta.json")))
DAYS = {"2026-06-07": "exam", "2026-06-08": "tune"}
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(3, 1, 1)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(3, 1, 1)
PAD = tuple(V4_META["preprocess"]["pad_color"])

s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
                  config=Config(max_pool_connections=16, retries={"max_attempts": 5}))
BUCKET = os.environ["R2_BUCKET_NAME"]


def session(path):
    o = ort.SessionOptions(); o.intra_op_num_threads = 2; o.inter_op_num_threads = 1
    return ort.InferenceSession(path, sess_options=o, providers=["CPUExecutionProvider"])


def tensor(pil):
    a = np.asarray(pil, np.float32).transpose(2, 0, 1) / 255.0
    return ((a - MEAN) / STD)[None]


def v3_input(raw):
    """The site today (core/classify.py): reduced decode, no rotation, squashed to 224."""
    img = Image.open(io.BytesIO(raw))
    if img.format in ("JPEG", "MPO"):
        img.draft("RGB", (900, 900))
    return tensor(img.convert("RGB").resize((224, 224), Image.LANCZOS))


def v4_input(raw):
    """v4 as trained (the notebook's prepare(), then its 256 -> 224 steps)."""
    img = Image.open(io.BytesIO(raw))
    if img.format in ("JPEG", "MPO"):
        img.draft("RGB", (900, 900))
    img = ImageOps.exif_transpose(img.convert("RGB"))
    side = max(img.size)
    sq = Image.new("RGB", (side, side), PAD)
    sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return tensor(sq.resize((256, 256), Image.LANCZOS).resize((224, 224), Image.BILINEAR))


def main():
    data = json.loads(s3.get_object(Bucket=BUCKET, Key="images.json")["Body"].read())
    items = [d for d in data if d.get("date") in DAYS and d.get("jacket_colors")]
    out_path = os.path.join(HERE, "results_versions.jsonl")
    done_paths = set()
    if os.path.exists(out_path):                   # picks up where a stopped run left off
        done_paths = {json.loads(l)["path"] for l in open(out_path) if l.strip()}
    todo = [d for d in items if d["path"] not in done_paths]
    print(f"{len(items)} labeled photos on {sorted(DAYS)}; {len(todo)} to grade", flush=True)
    sess3, sess4 = session(V3), session(V4)
    lock, out, t0, n = threading.Lock(), open(out_path, "a"), time.time(), [0]

    def work(d):
        rec = {"path": d["path"], "day": d["date"], "set": DAYS[d["date"]], "jacket": d.get("jacket_colors") or [],
               "pants": d.get("pants_colors") or [], "sport": d.get("sport") or "", "folder": d["location"]}
        try:
            raw = s3.get_object(Bucket=BUCKET, Key=d["path"])["Body"].read()
            for name, sess, x in (("v3", sess3, v3_input(raw)), ("v4", sess4, v4_input(raw))):
                j, p, s = sess.run(None, {"pixel_values": x})
                rec[name] = {"j": j[0].tolist(), "p": p[0].tolist(), "s": s[0].tolist()}
        except Exception as e:
            rec["error"] = str(e)[:200]
        with lock:
            out.write(json.dumps(rec) + "\n"); n[0] += 1
            if n[0] % 100 == 0 or n[0] == len(todo):
                out.flush(); print(f"{n[0]}/{len(todo)}  {(time.time()-t0)/60:.1f} min", flush=True)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, todo))
    out.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
