"""READ-ONLY grading of new labeling models against the live one, on checked photos they never trained on.

The photos: every labeled photo in a folder marked done (plus the exam folder, 2026-06-07) whose path is
not in the new models' training file (labels.csv). Today that is the exam days; as new folders are
checked after a training run, they count too, so new models are graded on recent real uploads.

Models are listed in MODELS: (name, folder with <name>.onnx + _meta.json, crop to the skier like v5b).
Each photo is read from storage into memory once; nothing is written to storage. Scores go to
results_new.jsonl (resumable). Run with the storage keys in the environment, in a Python that has
onnxruntime (and torch + torchvision when a model crops):
  python dev/model_eval/grade_new.py ~/Desktop/v5-training/labels.csv
then the report prints at the end (or: python dev/model_eval/grade_new.py --report)."""
import csv, io, json, os, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
V5 = os.path.expanduser("~/Desktop/v5-training/")
MODELS = [("model_v4a", os.path.expanduser("~/Desktop/photo-platform-dev/"), False),    # live before v5
          ("model_v5a", V5, False),
          ("model_v5b", V5, True)]
COMBOS = {"v5b jacket+sport, v5a pants": {"jacket": "model_v5b", "pants": "model_v5a", "sport": "model_v5b"}}
OUT = os.path.join(HERE, "results_new.jsonl")
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(3, 1, 1)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(3, 1, 1)
SAME = {"navy": "blue", "teal": "blue", "maroon": "red", "violet": "purple", "camo": "green"}
RULE = (0.50, 0.15)                                   # the site's second-jacket-color rule
MARGIN_X, MARGIN_Y = 0.12, 0.08                       # the v5b crop (as in the training notebook)


# ── Preparing a photo the way each model was trained ─────────────────────────
def to_tensor(img):
    from PIL import Image
    a = np.asarray(img.resize((224, 224), Image.LANCZOS), np.float32).transpose(2, 0, 1) / 255.0
    return ((a - MEAN) / STD)[None].astype(np.float32)


def decoded(raw):
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    o = (img.getexif() or {}).get(0x0112, 1)
    if img.format in ("JPEG", "MPO"):
        img.draft("RGB", (900, 900))
    return img.convert("RGB"), o


def to_stored(box, o, W, H):
    x0, y0, x1, y1 = box
    if o == 3: return (W - x1, H - y1, W - x0, H - y0)
    if o == 6: return (y0, H - x1, y1, H - x0)
    if o == 8: return (W - y1, x0, W - y0, x1)
    return box


def square_around(box, W, H):
    x0, y0, x1, y1 = box
    side = min(max((x1 - x0) * (1 + 2 * MARGIN_X), (y1 - y0) * (1 + 2 * MARGIN_Y)), W, H)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(cx - side / 2, 0), W - side); top = min(max(cy - side / 2, 0), H - side)
    return (int(left), int(top), int(left + side), int(top + side))


_finder, _finder_lock = None, threading.Lock()


def crop_to_skier(img, o):
    global _finder
    import torch
    from torchvision.models.detection import fasterrcnn_mobilenet_v3_large_fpn, FasterRCNN_MobileNet_V3_Large_FPN_Weights
    with _finder_lock:
        if _finder is None:
            w = FasterRCNN_MobileNet_V3_Large_FPN_Weights.DEFAULT
            _finder = (fasterrcnn_mobilenet_v3_large_fpn(weights=w, box_score_thresh=0.5).eval(), w.transforms())
        model, tf = _finder
        up = img.rotate({3: 180, 6: 270, 8: 90}[o], expand=True) if o in (3, 6, 8) else img
        with torch.no_grad():
            det = model([tf(up)])[0]
    people = [b.tolist() for b, lab in zip(det["boxes"], det["labels"]) if int(lab) == 1]
    if not people:
        return img
    big = max(people, key=lambda p: (p[2] - p[0]) * (p[3] - p[1]))
    return img.crop(square_around(to_stored(big, o, img.width, img.height), img.width, img.height))


# ── Grading ──────────────────────────────────────────────────────────────────
def grade(train_csv):
    import boto3, onnxruntime as ort
    from botocore.config import Config
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
                      config=Config(max_pool_connections=16, retries={"max_attempts": 5}))
    B = os.environ["R2_BUCKET_NAME"]
    trained = {r["r2_key"] for r in csv.DictReader(open(train_csv))}
    data = json.loads(s3.get_object(Bucket=B, Key="images.json")["Body"].read())
    meta = json.loads(s3.get_object(Bucket=B, Key="meta/folder_meta.json")["Body"].read())
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from core.text import folder_key
    reviewed = {k for k, v in meta.items() if isinstance(v, dict) and v.get("reviewed")}
    fk = lambda d: folder_key(d.get("date", ""), d.get("location", "").strip(), (d.get("last_name") or d.get("group") or "").strip())
    items = [d for d in data if d.get("jacket_colors") and not d.get("draft") and d["path"] not in trained
             and (d.get("date") == "2026-06-07" or fk(d) in reviewed)]
    done = {json.loads(l)["path"] for l in open(OUT)} if os.path.exists(OUT) else set()
    todo = [d for d in items if d["path"] not in done]
    print(f"{len(items)} checked photos none of the new models trained on; {len(todo)} to grade", flush=True)
    sessions = {}
    for name, folder, _ in MODELS:
        o = ort.SessionOptions(); o.intra_op_num_threads = 2; o.inter_op_num_threads = 1
        sessions[name] = (ort.InferenceSession(folder + name + ".onnx", sess_options=o, providers=["CPUExecutionProvider"]),
                          json.load(open(folder + name + "_meta.json"))["all_colors"])
    lock, out, t0, n = threading.Lock(), open(OUT, "a"), time.time(), [0]

    def work(d):
        rec = {"path": d["path"], "date": d.get("date"), "jacket": d.get("jacket_colors") or [],
               "pants": d.get("pants_colors") or [], "sport": d.get("sport") or ""}
        try:
            img, o = decoded(s3.get_object(Bucket=B, Key=d["path"])["Body"].read())
            whole = to_tensor(img)
            cropped = to_tensor(crop_to_skier(img, o)) if any(c for _, _, c in MODELS) else None
            for name, _, crop in MODELS:
                sess, colors = sessions[name]
                j, p, s = sess.run(None, {"pixel_values": cropped if crop else whole})
                rec[name] = {"colors": colors, "j": j[0].tolist(), "p": p[0].tolist(), "s": s[0].tolist()}
        except Exception as e:
            rec["error"] = str(e)[:200]
        with lock:
            out.write(json.dumps(rec) + "\n"); n[0] += 1
            if n[0] % 100 == 0 or n[0] == len(todo):
                out.flush(); print(f"{n[0]}/{len(todo)}  {(time.time()-t0)/60:.1f} min", flush=True)

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(work, todo))
    out.close()


# ── The report: "does the photo need a fix?", with the site's rules ─────────
def norm(cs):
    return frozenset(SAME.get(c, c) for c in (cs or []) if c) or frozenset(["none"])


def answers(r, name):
    m = r[name]
    colors = m["colors"]
    jp = 1 / (1 + np.exp(-np.asarray(m["j"])))
    o = np.argsort(jp)[::-1]
    jacket = [colors[o[0]]]
    if jp[o[1]] >= RULE[0] and jp[o[0]] - jp[o[1]] < RULE[1]:
        jacket.append(colors[o[1]])
    return {"jacket": norm(jacket), "pants": norm([colors[int(np.argmax(m["p"]))]]),
            "sport": ["ski", "snowboard", "unclear"][int(np.argmax(m["s"]))]}


EXAM_REAL_DAYS = {"2026-02-01", "2026-02-05", "2026-03-08", "2026-03-10"}   # never in any training file


def report():
    rows = [json.loads(l) for l in open(OUT) if l.strip()]
    rows = [r for r in rows if "error" not in r and all(n in r for n, _, _ in MODELS)]
    real = {p: (t or "")[:10] for p, (t, _) in json.load(open(os.path.join(HERE, "capture_times.json"))).items()}
    table(rows, "All checked photos the new models never trained on (some share a shooting day with training photos)")
    table([r for r in rows if real.get(r["path"]) in EXAM_REAL_DAYS],
          "The fair test: the four held-out shooting days (Feb 1, Feb 5, Mar 8, Mar 10)", colors=True)


def table(rows, title, colors=False):
    if not rows:
        print(f"\n{title}: no photos"); return
    truth = lambda r: {"jacket": norm(r["jacket"]), "pants": norm(r["pants"]), "sport": r["sport"] or "unclear"}
    pick = {n: [answers(r, n) for r in rows] for n, _, _ in MODELS}
    for cname, parts in COMBOS.items():
        pick[cname] = [{part: pick[src][i][part] for part, src in parts.items()} for i in range(len(rows))]
    print(f"\n{title}: {len(rows)} photos\n")
    print(f"{'model':32s} {'no fix':>7s} {'jacket':>7s} {'pants':>7s} {'sport':>7s}")
    best, best_score = None, -1
    for name, ans in pick.items():
        t = [truth(r) for r in rows]
        ok = {k: sum(a[k] == x[k] for a, x in zip(ans, t)) / len(rows) * 100 for k in ("jacket", "pants", "sport")}
        nf = sum(all(a[k] == x[k] for k in ("jacket", "pants", "sport")) for a, x in zip(ans, t)) / len(rows) * 100
        print(f"{name:32s} {nf:6.1f}% {ok['jacket']:6.1f}% {ok['pants']:6.1f}% {ok['sport']:6.1f}%")
        if nf > best_score:
            best, best_score = name, nf
    if not colors:
        return
    print(f"\nColors {best} misses most (share of photos with that color it got):")
    for part in ("jacket", "pants"):
        seen, got = Counter(), Counter()
        for a, r in zip(pick[best], rows):
            for c in truth(r)[part]:
                seen[c] += 1; got[c] += c in a[part]
        worst = sorted((got[c] / seen[c], c) for c in seen if seen[c] >= 10)[:6]
        print(f"  {part}: " + ", ".join(f"{c} {p*100:.0f}% of {seen[c]}" for p, c in worst))


if __name__ == "__main__":
    if sys.argv[1:] != ["--report"]:
        grade(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/Desktop/v5-training/labels.csv"))
    report()
