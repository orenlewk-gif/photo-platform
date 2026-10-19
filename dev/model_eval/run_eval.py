"""READ-ONLY v3 report card (approved by Oren 2026-10-18). Streams ~2,000 labeled photos v3 never trained on
from production storage into memory (nothing saved to disk, nothing written to storage), runs the live model
three ways (as live / faster decode / mirror image), and records the raw scores so cutoffs can be tuned offline."""
import csv, io, json, os, random, sys, time
from collections import defaultdict
from multiprocessing import Pool
HERE = os.path.dirname(os.path.abspath(__file__))
M = "/Users/orenlewkowski/Desktop/photo-platform/"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000

def pick():
    data = json.load(open(os.path.join(HERE, "images.json")))
    seen = {r["r2_key"] for r in csv.DictReader(open(M + "training_data/labels.csv"))}
    exam = os.path.join(HERE, "exam_photos.json")
    if os.path.exists(exam):                      # grade every version on the same photos
        want = set(json.load(open(exam)))
        pool = [d for d in data if d["path"] in want and d.get("jacket_colors") is not None]
        return [{"path": d["path"], "jacket": d["jacket_colors"], "pants": d["pants_colors"], "sport": d.get("sport"),
                 "corrected": bool(d.get("manually_corrected")), "folder": f'{d["date"]} {d["location"]}'} for d in pool]
    pool = [d for d in data if d.get("jacket_colors") is not None and not d.get("draft") and d["path"] not in seen]
    by = defaultdict(list)
    for d in pool:
        by[(d["date"], d["location"])].append(d)
    rnd = random.Random(42); out = []
    for k, items in sorted(by.items()):
        out += rnd.sample(items, round(N * len(items) / len(pool)))
    return [{"path": d["path"], "jacket": d["jacket_colors"], "pants": d["pants_colors"], "sport": d.get("sport"),
             "corrected": bool(d.get("manually_corrected")), "folder": f'{d["date"]} {d["location"]}'} for d in out]

_s = {}
def init():
    import boto3, numpy as np, onnxruntime as ort
    from dotenv import dotenv_values
    env = dotenv_values(os.path.expanduser("~/Desktop/photo-platform/.env"))
    _s["s3"] = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"], aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                            aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name="auto")
    _s["bucket"] = env.get("R2_BUCKET_NAME", "crystal-images")
    o = ort.SessionOptions(); o.intra_op_num_threads = 1; o.inter_op_num_threads = 1
    _s["sess"] = ort.InferenceSession(M + "model_v3.onnx", sess_options=o, providers=["CPUExecutionProvider"])
    meta = json.load(open(M + "model_v3_meta.json"))
    _s["mean"] = np.array(meta["clip_mean"], np.float32).reshape(3, 1, 1)
    _s["std"] = np.array(meta["clip_std"], np.float32).reshape(3, 1, 1)

def work(item):
    import numpy as np
    from PIL import Image
    try:
        b = _s["s3"].get_object(Bucket=_s["bucket"], Key=item["path"])["Body"].read()
        def prep(pil):
            a = np.asarray(pil.resize((224, 224), Image.LANCZOS), np.float32).transpose(2, 0, 1) / 255.
            return ((a - _s["mean"]) / _s["std"])[None]
        live = prep(Image.open(io.BytesIO(b)).convert("RGB"))            # exactly as the site does today
        im = Image.open(io.BytesIO(b)); im.draft("RGB", (900, 900)); fast = prep(im.convert("RGB"))
        flip = live[:, :, :, ::-1].copy()
        res = {}
        for name, x in (("live", live), ("fast", fast), ("flip", flip)):
            j, p, s = _s["sess"].run(None, {"pixel_values": x})
            res[name] = {"j": j[0].tolist(), "p": p[0].tolist(), "s": s[0].tolist()}
        return {**item, **res}
    except Exception as e:
        return {**item, "error": str(e)}

if __name__ == "__main__":
    items = pick()
    json.dump(items, open(os.path.join(HERE, "sample.json"), "w"))
    out = open(os.path.join(HERE, "results.jsonl"), "w"); t0 = time.time(); done = 0
    print(f"{len(items)} photos picked", flush=True)
    with Pool(6, initializer=init) as pool:
        for r in pool.imap_unordered(work, items, chunksize=2):
            out.write(json.dumps(r) + "\n"); done += 1
            if done % 100 == 0 or done == len(items):
                out.flush(); print(f"{done}/{len(items)}  {time.time()-t0:.0f}s", flush=True)
    print("DONE", flush=True)
