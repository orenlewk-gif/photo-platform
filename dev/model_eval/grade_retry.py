"""READ-ONLY exam for the v4 retry (Oren's OK, 2026-10-26): v3, v4a (v3's preparation) and v4b (upright),
each on every labeled photo in the exam folder (6/7), streamed from storage into memory once (nothing saved
to disk or written to storage). Raw scores go to results_retry.jsonl; report with report_retry.py.

Run with the storage keys in the environment:
  R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET_NAME=... python grade_retry.py
"""
import io, json, os, threading, time
from concurrent.futures import ThreadPoolExecutor
import boto3
import numpy as np
import onnxruntime as ort
from botocore.config import Config
from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.expanduser("~/Desktop/v4-training/retry/")
MODELS = {"v3": (os.path.expanduser("~/Desktop/photo-platform/model_v3.onnx"), False),   # (file, turned upright)
          "v4a": (FILES + "model_v4a.onnx", False),
          "v4b": (FILES + "model_v4b.onnx", True)}
EXAM_FOLDER = "2026-06-07"
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(3, 1, 1)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(3, 1, 1)

s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
                  config=Config(max_pool_connections=16, retries={"max_attempts": 5}))
BUCKET = os.environ["R2_BUCKET_NAME"]


def prepared(raw, upright):
    """Exactly how the site prepares a photo (core/classify.py), optionally turned upright first."""
    img = Image.open(io.BytesIO(raw))
    if img.format in ("JPEG", "MPO"):
        img.draft("RGB", (900, 900))
    img = img.convert("RGB")
    if upright:
        img = ImageOps.exif_transpose(img)
    a = np.asarray(img.resize((224, 224), Image.LANCZOS), np.float32).transpose(2, 0, 1) / 255.0
    return ((a - MEAN) / STD)[None]


def main():
    data = json.loads(s3.get_object(Bucket=BUCKET, Key="images.json")["Body"].read())
    items = [d for d in data if d.get("date") == EXAM_FOLDER and d.get("jacket_colors")]
    out_path = os.path.join(HERE, "results_retry.jsonl")
    done = {json.loads(l)["path"] for l in open(out_path)} if os.path.exists(out_path) else set()
    todo = [d for d in items if d["path"] not in done]
    print(f"{len(items)} labeled exam-folder photos; {len(todo)} to grade", flush=True)
    sessions = {}
    for name, (path, _) in MODELS.items():
        o = ort.SessionOptions(); o.intra_op_num_threads = 2; o.inter_op_num_threads = 1
        sessions[name] = ort.InferenceSession(path, sess_options=o, providers=["CPUExecutionProvider"])
    lock, out, t0, n = threading.Lock(), open(out_path, "a"), time.time(), [0]

    def work(d):
        rec = {"path": d["path"], "jacket": d.get("jacket_colors") or [], "pants": d.get("pants_colors") or [],
               "sport": d.get("sport") or "", "folder": d["location"]}
        try:
            raw = s3.get_object(Bucket=BUCKET, Key=d["path"])["Body"].read()
            inputs = {u: prepared(raw, u) for u in (False, True)}
            for name, (_, upright) in MODELS.items():
                j, p, s = sessions[name].run(None, {"pixel_values": inputs[upright]})
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
