"""READ-ONLY: how sure the labeling model was on checked real uploads, to set the forgiving search's line.

For every photo in labels.csv that a model labeled at upload (labeled_by) and Oren then checked, the
model is run again on the photo (read from storage into memory, nothing written there) and its score
for every color is saved next to the checked labels. Then the report: if a search also shows a photo
under a color the model scored at least T (and the color isn't already its label), how many of the
photos people would otherwise miss come back, and how many extra photos does a search show.

  python dev/model_eval/sureness.py <labels.csv> <model folder> <model name> <out.jsonl>
  python dev/model_eval/sureness.py --report <out.jsonl>
Storage keys come from the environment (R2_*), as for the export."""
import csv, io, json, os, sys, threading, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(3, 1, 1)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(3, 1, 1)
SAME = {"navy": "blue", "teal": "blue", "maroon": "red", "violet": "purple", "camo": "green", "tan": "brown"}
CHIPS = ["red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "black", "gray", "brown"]


def shade(c):
    c = (c or "").strip().lower()
    return SAME.get(c, c)


def colors(s):
    return sorted({shade(c) for c in str(s or "").split("|") if c.strip()})


def run(labels_csv, model_dir, name, out_path):
    import boto3
    import onnxruntime as ort
    from PIL import Image
    rows = [r for r in csv.DictReader(open(labels_csv)) if r.get("labeled_by")]
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(l)["path"] for l in open(out_path)}
    todo = [r for r in rows if r["r2_key"] not in done]
    print(f"{len(rows)} checked real uploads, {len(todo)} to score with {name}")
    meta = json.load(open(os.path.join(model_dir, f"{name}_meta.json")))
    sess = ort.InferenceSession(os.path.join(model_dir, f"{name}.onnx"), providers=["CPUExecutionProvider"])
    all_colors, sports = meta["all_colors"], meta["sports"]
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    bucket = os.environ["R2_BUCKET_NAME"]
    lock, out, t0, n = threading.Lock(), open(out_path, "a"), time.time(), [0]

    def prep(raw):
        img = Image.open(io.BytesIO(raw))
        if img.format in ("JPEG", "MPO"):
            img.draft("RGB", (900, 900))
        a = np.asarray(img.convert("RGB").resize((224, 224), Image.LANCZOS), np.float32).transpose(2, 0, 1) / 255.0
        return ((a - MEAN) / STD)[None].astype(np.float32)

    def one(r):
        try:
            x = prep(s3.get_object(Bucket=bucket, Key=r["r2_key"])["Body"].read())
        except Exception as e:
            print("skip", r["r2_key"], str(e)[:60]); return
        with lock:
            j, p, s = sess.run(["jacket_logits", "pants_logits", "sport_logits"], {"pixel_values": x})
        sig = lambda v: 1 / (1 + np.exp(-v))
        def merged(prob):
            best = {}
            for c, pr in zip(all_colors, prob):
                c = shade(c)
                best[c] = max(best.get(c, 0.0), float(pr))
            return {c: round(v, 4) for c, v in best.items()}
        sp = np.exp(s[0] - s[0].max()); sp /= sp.sum()
        rec = {"path": r["r2_key"], "jacket": colors(r["jacket_colors"]), "pants": colors(r["pants_colors"]),
               "sport": r["sport"], "said_jacket": colors(r["model_jacket"]), "said_pants": colors(r["model_pants"]),
               "said_sport": r["model_sport"], "j": merged(sig(j[0])), "p": merged(sig(p[0])),
               "s": {k: round(float(v), 4) for k, v in zip(sports, sp)}}
        with lock:
            out.write(json.dumps(rec) + "\n"); n[0] += 1
            if n[0] % 250 == 0:
                out.flush(); print(f"  {n[0]}/{len(todo)}  {(time.time() - t0) / 60:.1f} min", flush=True)
    with ThreadPoolExecutor(12) as ex:
        list(ex.map(one, todo))
    out.close()
    print("done", n[0])


def report(out_path):
    recs = [json.loads(l) for l in open(out_path)]
    n = len(recs)
    print(f"{n} checked real uploads\n")
    for part, key, said_key in (("jacket", "j", "said_jacket"), ("pants", "p", "said_pants")):
        # every (photo, real color) the model didn't label: would a search for that color find it?
        missed = [(r, c) for r in recs for c in r[part] if c in CHIPS and c not in r[said_key]]
        labeled = Counter(c for r in recs for c in r[said_key] if c in CHIPS)
        print(f"== {part}: {len(missed)} times a real color wasn't the model's label "
              f"(a search for that color misses the photo today)")
        print(f"{'line':>6} | {'found again':>12} | {'extra photos per search, vs today':>34}")
        for t in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
            back = sum(1 for r, c in missed if r[key].get(c, 0) >= t)
            extra = Counter(c for r in recs for c, v in r[key].items() if c in CHIPS and v >= t and c not in r[said_key])
            right = Counter(c for r in recs for c, v in r[key].items()
                            if c in CHIPS and v >= t and c not in r[said_key] and c in r[part])
            ext = sum(extra.values()); tot = sum(labeled.values())
            print(f"{t:>6.2f} | {back:>5} ({back / max(1, len(missed)) * 100:4.1f}%) | "
                  f"+{ext / max(1, tot) * 100:5.1f}% photos ({sum(right.values())} of {ext} really that color)")
        print()
    # per color at a middle line, to see where it helps
    t = 0.20
    print(f"== per jacket color at line {t}: missed today -> found again | extra photos shown")
    for c in CHIPS:
        miss = [(r) for r in recs if c in r["jacket"] and c not in r["said_jacket"]]
        back = sum(1 for r in miss if r["j"].get(c, 0) >= t)
        lab = sum(1 for r in recs if c in r["said_jacket"])
        ext = sum(1 for r in recs if c not in r["said_jacket"] and r["j"].get(c, 0) >= t)
        print(f"  {c:7} missed {len(miss):4} -> found {back:4} | labeled {lab:5}, +{ext} shown")
    for t in (0.10, 0.20, 0.30):
        miss = [r for r in recs if r["sport"] in ("ski", "snowboard") and r["said_sport"] != r["sport"]]
        back = sum(1 for r in miss if r["s"].get(r["sport"], 0) >= t)
        print(f"sport, line {t}: {len(miss)} wrong today -> {back} found again")


if __name__ == "__main__":
    if sys.argv[1] == "--report":
        report(sys.argv[2])
    else:
        run(*sys.argv[1:5])
