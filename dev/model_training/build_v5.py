"""Builds v5_train.ipynb (run: python3 dev/model_training/build_v5.py).

v4a (v3's recipe + more photos) tied v3 on the exam days; it labels new uploads now. v5 keeps that
recipe and changes one thing per run:
  Run A (v5a): all labeled photos (~25k), camo folded into green (15 colors, Oren 2026-10-27)
  Run B (v5b): the same, with each photo cropped to the skier first (the biggest person found by a
               free person finder, torchvision's FasterRCNN-MobileNetV3, BSD license)
Both run back to back in one session and are checked on the same real days as v4 (Jan 23, Mar 7)."""
import json
import os

CELLS = []


def md(text):
    CELLS.append(("markdown", text.strip("\n")))


def code(text):
    CELLS.append(("code", text.strip("\n")))


md(r'''
# Crystal Images — color model v5 (two runs)

Same recipe as v4a (the model labeling uploads now), one change per run:

| Run | What's different from v4a |
|---|---|
| **v5a** | all the newly labeled photos, and **camo folded into green** (15 colors instead of 16) |
| **v5b** | the same, and every photo is **cropped to the skier** first (the biggest person in the frame) |

Both start from v3's weights (camo's slot removed), use v3's learning speed, rounds and small photo variations.
Progress is checked on the same real days as v4 (**Jan 23, Mar 7**), never trained on. The exam days (Feb 1, Feb 5,
Mar 8, Mar 10) aren't in labels.csv at all; both runs are graded on them afterwards, and only a run that beats
the live model goes live.

**Kaggle:** the same dataset with the **new labels.csv** (New Version > replace labels.csv; keep model_v3.pt), the same
four Secrets, **GPU T4 x2, Internet on** (the person finder downloads its weights), then **Save Version > Save & Run All**.
About an hour and a half; you can close the browser.
''')

code(r'''
# 1. Setup
import os, io, sys, subprocess, glob, json, time, threading, zipfile
from pathlib import Path

ON_KAGGLE = os.path.exists('/kaggle')
try:
    import google.colab  # noqa: F401
    ON_COLAB = True
except ImportError:
    ON_COLAB = False
PLATFORM = 'kaggle' if ON_KAGGLE else 'colab' if ON_COLAB else 'computer'
print('Running on', PLATFORM)

def _need(*pkgs):
    missing = []
    for p in pkgs:
        try:
            __import__(p.replace('-', '_'))
        except ImportError:
            missing.append(p)
    if missing:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', *missing], check=True)
_need('boto3', 'onnx', 'onnxruntime', 'onnxscript', 'transformers')

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from PIL import Image, ImageOps, ImageDraw
from torchvision import transforms
from transformers import CLIPModel
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEVICE = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
print('PyTorch', torch.__version__, '| device:', DEVICE, '|', torch.cuda.get_device_name(0) if DEVICE == 'cuda' else '')
''')

code(r'''
# 2. Settings
RUNS = [('v5a', 'as_v3'),      # the photo as the site prepares it today (fills the frame, as stored)
        ('v5b', 'crop')]       # the same, cropped to the skier first
PROGRESS_DAYS = {'2026-01-23', '2026-03-07'}   # real capture days held out for the progress check (as in v4)
# v3's recipe (also v4a's): last 4 vision blocks + projection at 5e-6, heads at 1e-4,
# AdamW (weight decay 1e-4), cosine over 10 rounds, batch 32, flip + light color jitter, plain losses
ROUNDS, BODY_LR, HEAD_LR, BATCH = 10, 5e-6, 1e-4, 32
SECOND_JACKET_MIN, SECOND_JACKET_GAP = 0.50, 0.15   # the site's rules for a second jacket color
SMOKE = os.environ.get('SMOKE_TEST') == '1'         # a quick end-to-end check on a few photos (not for real runs)
if SMOKE:
    ROUNDS = 1

if ON_KAGGLE:
    found = glob.glob('/kaggle/input/**/labels.csv', recursive=True)
    assert found, 'Add your labels.csv dataset as an Input'
    LABELS, SAVE = Path(found[0]), Path('/kaggle/working')
elif ON_COLAB:
    from google.colab import drive
    drive.mount('/content/drive')
    SAVE = Path('/content/drive/MyDrive/crystal_images'); LABELS = SAVE / 'labels.csv'
else:
    SAVE, LABELS = Path('training_run'), Path('training_data/labels.csv')
SAVE.mkdir(parents=True, exist_ok=True)
WORK = Path('/kaggle/temp' if ON_KAGGLE else '/content/work' if ON_COLAB else 'training_run/work')

def find_input(name):
    for p in [SAVE / name, Path('.') / name, *map(Path, glob.glob(f'/kaggle/input/**/{name}', recursive=True))]:
        if p.exists():
            return p
    return None

def secret(name):
    if ON_KAGGLE:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret(name)
    if ON_COLAB:
        from google.colab import userdata
        return userdata.get(name)
    return os.environ[name]

V3_WEIGHTS = find_input('model_v3.pt')
assert V3_WEIGHTS or SMOKE, 'Add model_v3.pt to the dataset — both runs start from v3'
print('labels:', LABELS, '| v3 weights:', V3_WEIGHTS, '| saving to:', SAVE)
''')

code(r'''
# 3. Labels (camo is part of green now); progress-check photos are the held-out real days
ALL_COLORS = ['red','orange','yellow','green','blue','purple','pink',
              'white','black','gray','brown','tan','teal','none','unclear']   # v3's order without camo (the site reads it from the meta file)
V3_COLORS = ['red','orange','yellow','green','blue','purple','pink',
             'white','black','gray','brown','tan','teal','camo','none','unclear']   # v3's heads, for loading its weights
SPORTS = ['ski', 'snowboard', 'unclear']
C2I = {c: i for i, c in enumerate(ALL_COLORS)}
S2I = {s: i for i, s in enumerate(SPORTS)}
SAME = {'navy': 'blue', 'teal': 'blue', 'maroon': 'red', 'violet': 'purple', 'camo': 'green'}   # how the site groups shades

def colors(s):
    out = []
    for c in str(s if isinstance(s, str) else '').split('|'):
        c = SAME.get(c.strip().lower(), c.strip().lower())
        if c in C2I and c not in out:
            out.append(c)
    return out

df = pd.read_csv(LABELS)
assert 'taken' in df.columns, 'This labels.csv is the old one — export again (it includes each photo\'s real date)'
df['jacket'] = df['jacket_colors'].apply(colors)
df['pants'] = df['pants_colors'].apply(colors)
df['sport_i'] = df['sport'].apply(lambda s: S2I.get(str(s).strip().lower(), S2I['unclear']))
df['real_day'] = df['taken'].fillna('').astype(str).str[:10]
df = df[df['jacket'].str.len() > 0].reset_index(drop=True)
if SMOKE:
    df = pd.concat([df[df['real_day'].isin(PROGRESS_DAYS)].head(16), df[~df['real_day'].isin(PROGRESS_DAYS)].head(48)]).reset_index(drop=True)
is_check = df['real_day'].isin(PROGRESS_DAYS)
train_df, check_df = df[~is_check].reset_index(drop=True), df[is_check].reset_index(drop=True)
print(f'{len(df)} labeled photos from {df["real_day"].nunique()} real days | training {len(train_df)} | '
      f'progress check {sorted(PROGRESS_DAYS)}: {len(check_df)}')
print('jacket colors:', dict(Counter(c for cs in df['jacket'] for c in cs).most_common()))
''')

code(r'''
# 4. Download each photo once. Keep it two ways: as the site prepares it today, and a 900px copy
#    (as stored) for the person finder in the next step. The prepared photos are zipped for v6.
def decoded(raw):
    img = Image.open(io.BytesIO(raw))
    orient = (img.getexif() or {}).get(0x0112, 1)
    if img.format in ('JPEG', 'MPO'):
        img.draft('RGB', (900, 900))         # reduced decode, as the site does
    return img.convert('RGB'), orient

def local_name(key, prep):
    return WORK / prep / (key.replace('/', '__').rsplit('.', 1)[0] + ('.jpg' if prep == 'big' else '.png'))

ORIENT_FILE = WORK / 'orientation.json'
for prep in ('as_v3', 'big', 'crop'):
    (WORK / prep).mkdir(parents=True, exist_ok=True)
for prep in ('as_v3', 'crop'):
    old_zip = find_input(f'prepared_{prep}.zip')
    if old_zip and not any((WORK / prep).iterdir()):
        print('Unpacking photos prepared earlier:', old_zip)
        with zipfile.ZipFile(old_zip) as z:
            z.extractall(WORK / prep)
orient = json.load(open(ORIENT_FILE)) if ORIENT_FILE.exists() else {}

need = [k for k in df['r2_key'] if not local_name(k, 'as_v3').exists()
        or (not local_name(k, 'crop').exists() and not local_name(k, 'big').exists())]
print(f'{len(df) - len(need)} photos already here, {len(need)} to download')
if need:
    import boto3
    from botocore.config import Config
    s3 = boto3.client('s3', endpoint_url=secret('R2_ENDPOINT_URL'), aws_access_key_id=secret('R2_ACCESS_KEY_ID'),
                      aws_secret_access_key=secret('R2_SECRET_ACCESS_KEY'),
                      config=Config(max_pool_connections=64, retries={'max_attempts': 5}))
    BUCKET = secret('R2_BUCKET_NAME')
    done, failed, lock, t0 = [0], [], threading.Lock(), time.time()
    def fetch(key):
        try:
            img, o = decoded(s3.get_object(Bucket=BUCKET, Key=key)['Body'].read())
            img.resize((224, 224), Image.LANCZOS).save(local_name(key, 'as_v3'))   # fills the frame, like the site
            img.save(local_name(key, 'big'), quality=92)
            with lock:
                orient[key] = o
        except Exception as e:
            failed.append((key, str(e)[:80]))
        with lock:
            done[0] += 1
            if done[0] % 500 == 0 or done[0] == len(need):
                print(f'  {done[0]}/{len(need)}  ({(time.time()-t0)/60:.1f} min)', flush=True)
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(fetch, need))
    json.dump(orient, open(ORIENT_FILE, 'w'))
    print(f'Failed: {len(failed)}', failed[:5])
''')

code(r'''
# 5. Crop to the skier (v5b): the person finder looks at an upright copy, the biggest person is
#    the skier, and a square around them (a little margin, never stretched) is cut from the photo
#    as stored, so the only difference from v5a is the crop. No person found: the whole photo.
from torchvision.models.detection import fasterrcnn_mobilenet_v3_large_fpn, FasterRCNN_MobileNet_V3_Large_FPN_Weights
MARGIN_X, MARGIN_Y = 0.12, 0.08     # around the person's box (as in the crop test: right skier in 48/48 photos)

def to_stored(box, o, W, H):
    """A box on the upright copy -> the same pixels on the photo as stored (EXIF orientation o)."""
    x0, y0, x1, y1 = box
    if o == 3: return (W - x1, H - y1, W - x0, H - y0)
    if o == 6: return (y0, H - x1, y1, H - x0)
    if o == 8: return (W - y1, x0, W - y0, x1)
    return box

def square_around(box, W, H):
    x0, y0, x1, y1 = box
    w, h = (x1 - x0) * (1 + 2 * MARGIN_X), (y1 - y0) * (1 + 2 * MARGIN_Y)
    side = min(max(w, h), W, H)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(cx - side / 2, 0), W - side); top = min(max(cy - side / 2, 0), H - side)
    return (int(left), int(top), int(left + side), int(top + side))

todo = [k for k in df['r2_key'] if not local_name(k, 'crop').exists() and local_name(k, 'big').exists()]
crop_found = {}
if todo:
    weights = FasterRCNN_MobileNet_V3_Large_FPN_Weights.DEFAULT
    FIND_ON = 'cuda' if DEVICE == 'cuda' else 'cpu'          # (Apple's GPU mode is unreliable for this model)
    finder = fasterrcnn_mobilenet_v3_large_fpn(weights=weights, box_score_thresh=0.5).eval().to(FIND_ON)
    to_t = weights.transforms()
    t0 = time.time()
    def load(key):
        img = Image.open(local_name(key, 'big')).convert('RGB')
        o = int(orient.get(key, 1))
        up = img.rotate({3: 180, 6: 270, 8: 90}[o], expand=True) if o in (3, 6, 8) else img   # = exif_transpose
        return key, img, up, o
    with ThreadPoolExecutor(8) as ex:
        for b in range(0, len(todo), 16):
            batch = list(ex.map(load, todo[b:b + 16]))
            with torch.no_grad():
                dets = finder([to_t(up).to(FIND_ON) for _, _, up, _ in batch])
            for (key, img, up, o), det in zip(batch, dets):
                people = [bx.tolist() for bx, lab in zip(det['boxes'].cpu(), det['labels'].cpu()) if int(lab) == 1]
                if people:
                    biggest = max(people, key=lambda p: (p[2] - p[0]) * (p[3] - p[1]))
                    cut = img.crop(square_around(to_stored(biggest, o, img.width, img.height), img.width, img.height))
                else:
                    cut = img
                crop_found[key] = bool(people)
                cut.resize((224, 224), Image.LANCZOS).save(local_name(key, 'crop'))
            if (b // 16) % 50 == 0:
                print(f'  {min(b + 16, len(todo))}/{len(todo)}  ({(time.time()-t0)/60:.1f} min)', flush=True)
    found_n = sum(crop_found.values())
    print(f'Person found in {found_n}/{len(crop_found)} photos ({found_n / max(1, len(crop_found)) * 100:.1f}%); the rest use the whole photo')
    # A contact sheet to look at: whole photo (left) and what v5b sees (right)
    sample = [k for k in todo[:: max(1, len(todo) // 24)]][:24]
    sheet = Image.new('RGB', (3 * 470, 8 * 230), (0, 0, 0))
    for i, k in enumerate(sample):
        tile = Image.new('RGB', (470, 230), (20, 20, 20))
        tile.paste(Image.open(local_name(k, 'as_v3')), (3, 3)); tile.paste(Image.open(local_name(k, 'crop')), (240, 3))
        sheet.paste(tile, ((i % 3) * 470, (i // 3) * 230))
    sheet.save(SAVE / 'crop_examples.jpg', quality=85)
    print('Saved crop_examples.jpg (Output) to check the crops by eye')
for prep in ('as_v3', 'crop'):
    with zipfile.ZipFile(SAVE / f'prepared_{prep}.zip', 'w', zipfile.ZIP_STORED) as z:
        for p in (WORK / prep).iterdir():
            z.write(p, p.name)
ready = lambda frame: frame[frame['r2_key'].map(lambda k: local_name(k, 'as_v3').exists() and local_name(k, 'crop').exists())].reset_index(drop=True)
train_df, check_df = ready(train_df), ready(check_df)
print(f'ready: training {len(train_df)}, progress check {len(check_df)}')
''')

code(r'''
# 6. Photos, model and scoring — v3's exact setup (15 colors)
MEAN = [0.48145466, 0.4578275, 0.40821073]
STD = [0.26862954, 0.26130258, 0.27577711]
train_tf = transforms.Compose([transforms.Resize((224, 224)), transforms.RandomHorizontalFlip(),
                               transforms.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.1),
                               transforms.ToTensor(), transforms.Normalize(MEAN, STD)])
check_tf = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(), transforms.Normalize(MEAN, STD)])

def multihot(cs):
    v = np.zeros(len(ALL_COLORS), dtype=np.float32)
    for c in cs:
        v[C2I[c]] = 1
    return v

class Photos(Dataset):
    def __init__(self, frame, prep, tf):
        self.keys, self.prep, self.tf = frame['r2_key'].tolist(), prep, tf
        self.j = np.stack(frame['jacket'].apply(multihot)); self.p = np.stack(frame['pants'].apply(multihot))
        self.s = frame['sport_i'].to_numpy()
    def __len__(self): return len(self.keys)
    def __getitem__(self, i):
        return self.tf(Image.open(local_name(self.keys[i], self.prep)).convert('RGB')), self.j[i], self.p[i], self.s[i]

class ColorClassifier(nn.Module):
    def __init__(self, clip_model, n_colors=len(ALL_COLORS), n_sports=3, dropout=0.3):
        super().__init__()
        self.vision, self.proj = clip_model.vision_model, clip_model.visual_projection
        head = lambda out: nn.Sequential(nn.Linear(512, 256), nn.ReLU(), nn.Dropout(dropout), nn.Linear(256, out))
        self.jacket_head, self.pants_head, self.sport_head = head(n_colors), head(n_colors), head(n_sports)
    def forward(self, pixel_values):
        feat = self.proj(self.vision(pixel_values=pixel_values).pooler_output)
        feat = feat / feat.norm(dim=-1, keepdim=True)
        return self.jacket_head(feat), self.pants_head(feat), self.sport_head(feat)

def load_v3_without_camo(model, path):
    """v3's weights with camo's output removed from the jacket and pants heads (16 -> 15 colors)."""
    sd = torch.load(path, map_location='cpu')
    keep = [V3_COLORS.index(c) for c in ALL_COLORS]
    for head in ('jacket_head', 'pants_head'):
        sd[f'{head}.3.weight'] = sd[f'{head}.3.weight'][keep]
        sd[f'{head}.3.bias'] = sd[f'{head}.3.bias'][keep]
    model.load_state_dict(sd)

def decide(j, p, s):
    o = np.argsort(j)[::-1]
    jacket = {ALL_COLORS[o[0]]}
    if j[o[1]] >= SECOND_JACKET_MIN and j[o[0]] - j[o[1]] < SECOND_JACKET_GAP:
        jacket.add(ALL_COLORS[o[1]])
    return jacket, {ALL_COLORS[int(np.argmax(p))]}, int(np.argmax(s))

@torch.no_grad()
def check(model, dl):
    model.eval()
    J, P, S, TJ, TP, TS = [], [], [], [], [], []
    for x, j, p, s in dl:
        oj, op, os_ = model(x.to(DEVICE))
        J.append(torch.sigmoid(oj).cpu().numpy()); P.append(torch.sigmoid(op).cpu().numpy()); S.append(os_.cpu().numpy())
        TJ.append(j.numpy()); TP.append(p.numpy()); TS.append(s.numpy())
    J, P, S, TJ, TP, TS = map(np.concatenate, (J, P, S, TJ, TP, TS))
    ok = Counter()
    for i in range(len(J)):
        dj, dp, ds = decide(J[i], P[i], S[i])
        a = dj == {ALL_COLORS[k] for k in np.flatnonzero(TJ[i])}
        b = dp == {ALL_COLORS[k] for k in np.flatnonzero(TP[i])}
        c = ds == TS[i]
        ok['jacket'] += a; ok['pants'] += b; ok['sport'] += c; ok['no_fix'] += a and b and c
    return {k: ok[k] / len(J) for k in ('no_fix', 'jacket', 'pants', 'sport')}

fmt = lambda m: (f"photo needs no fix {m['no_fix']*100:.1f}%  (jacket {m['jacket']*100:.1f}%  "
                 f"pants {m['pants']*100:.1f}%  sport {m['sport']*100:.1f}%)")
bce, ce = nn.BCEWithLogitsLoss(), nn.CrossEntropyLoss()   # plain losses, like v3
loss_fn = lambda out, j, p, s: bce(out[0], j) + bce(out[1], p) + ce(out[2], s)
''')

code(r'''
# 7. Train both runs (each saved after every round; run all again after a stop and it continues)
import onnx, onnxruntime
results = {}
for name, prep in RUNS:
    print(f'\n========== {name}: {"as the site prepares photos" if prep == "as_v3" else "cropped to the skier"} ==========')
    workers = 4 if DEVICE == 'cuda' else 0
    train_dl = DataLoader(Photos(train_df, prep, train_tf), batch_size=BATCH, shuffle=True, num_workers=workers,
                          pin_memory=DEVICE == 'cuda', drop_last=True)
    check_dl = DataLoader(Photos(check_df, prep, check_tf), batch_size=64, shuffle=False, num_workers=workers)
    model = ColorClassifier(CLIPModel.from_pretrained('openai/clip-vit-base-patch32'))
    if V3_WEIGHTS:
        load_v3_without_camo(model, V3_WEIGHTS)
    model = model.to(DEVICE)
    for p in model.parameters():
        p.requires_grad = False
    for layer in model.vision.encoder.layers[-4:]:
        for p in layer.parameters(): p.requires_grad = True
    for p in list(model.vision.post_layernorm.parameters()) + list(model.proj.parameters()):
        p.requires_grad = True
    heads = [p for h in (model.jacket_head, model.pants_head, model.sport_head) for p in h.parameters()]
    for p in heads: p.requires_grad = True
    body = [p for p in list(model.vision.parameters()) + list(model.proj.parameters()) if p.requires_grad]
    opt = torch.optim.AdamW([{'params': body, 'lr': BODY_LR}, {'params': heads, 'lr': HEAD_LR}], weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=ROUNDS, eta_min=1e-7)

    ckpt = SAVE / f'checkpoint_{name}.pt'
    state = {'round': 0, 'best': -1.0, 'history': []}
    if ckpt.exists():
        ck = torch.load(ckpt, map_location=DEVICE, weights_only=False)
        model.load_state_dict(ck['model']); opt.load_state_dict(ck['opt']); sched.load_state_dict(ck['sched'])
        state = ck['state']
        print(f"Continuing after round {state['round']} (best so far {state['best']*100:.1f}%)")
    if state['round'] == 0:
        start = check(model, check_dl)
        state['start'] = start
        print('v3 (without camo) on the progress-check days, prepared this way:', fmt(start))
    for r in range(state['round'], ROUNDS):
        model.train(); t0 = time.time(); total = 0.0
        for x, j, p, s in train_dl:
            x, j, p, s = x.to(DEVICE), j.to(DEVICE), p.to(DEVICE), s.to(DEVICE)
            opt.zero_grad()
            loss = loss_fn(model(x), j, p, s)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); total += loss.item()
        sched.step()
        m = check(model, check_dl)
        better = m['no_fix'] > state['best']
        if better:
            state['best'] = m['no_fix']
            torch.save(model.state_dict(), SAVE / f'model_{name}.pt')
        state['round'] = r + 1
        state['history'].append({'round': r + 1, 'loss': total / len(train_dl), **m})
        torch.save({'model': model.state_dict(), 'opt': opt.state_dict(), 'sched': sched.state_dict(), 'state': state}, ckpt)
        print(f"[{name} round {r+1}/{ROUNDS}] {(time.time()-t0)/60:.1f} min  loss {total/len(train_dl):.3f}  {fmt(m)}"
              + ('  <-- best' if better else ''), flush=True)

    # The best round, as files for the site (same layout as v3 / v4a)
    model.load_state_dict(torch.load(SAVE / f'model_{name}.pt', map_location=DEVICE))
    model.eval().cpu()
    dummy = torch.randn(1, 3, 224, 224)
    path = SAVE / f'model_{name}.onnx'
    torch.onnx.export(model, dummy, str(path), opset_version=18, input_names=['pixel_values'],
                      output_names=['jacket_logits', 'pants_logits', 'sport_logits'],
                      dynamic_axes={k: {0: 'batch'} for k in ('pixel_values', 'jacket_logits', 'pants_logits', 'sport_logits')})
    onnx.checker.check_model(onnx.load(str(path)))
    sess = onnxruntime.InferenceSession(str(path), providers=['CPUExecutionProvider'])
    x = next(iter(check_dl))[0][:4]
    same = all(np.allclose(a.detach().numpy(), b, atol=1e-3) for a, b in zip(model(x), sess.run(None, {'pixel_values': x.numpy()})))
    json.dump({'version': name, 'base_model': 'openai/clip-vit-base-patch32', 'all_colors': ALL_COLORS, 'sports': SPORTS,
               'color_to_idx': C2I, 'sport_to_idx': S2I, 'best_score': float(state['best']),
               'score': 'photo needs no fix (site rules)', 'progress_check_days': sorted(PROGRESS_DAYS),
               'clip_mean': MEAN, 'clip_std': STD, 'clip_size': 224,
               'preprocess': {'decode_min_px': 900, 'upright': False, 'pad_square': False, 'fill_frame': True,
                              'crop_person': prep == 'crop',
                              **({'person_finder': 'torchvision fasterrcnn_mobilenet_v3_large_fpn (COCO), score >= 0.5, biggest person',
                                  'crop': f'square around the box, margin x {MARGIN_X} y {MARGIN_Y}, cut from the photo as stored'}
                                 if prep == 'crop' else {})},
               'start': state.get('start'), 'history': state['history']},
              open(SAVE / f'model_{name}_meta.json', 'w'), indent=2)
    results[name] = state
    print(f"{name}: best {fmt(state['history'][int(np.argmax([h['no_fix'] for h in state['history']]))])}  | ONNX matches: {same}")
    model.to(DEVICE)
''')

code(r'''
# 8. Summary
for name, prep in RUNS:
    st = results[name]
    best = max(st['history'], key=lambda h: h['no_fix'])
    print(f"{name} ({'as the site prepares photos' if prep == 'as_v3' else 'cropped to the skier'}): "
          f"v3 started at {st['start']['no_fix']*100:.1f}%, best round {best['round']}: {fmt(best)}")
print('\nFiles to download (Output):', ', '.join(sorted(f.name for f in SAVE.glob('model_v5*'))), '+ crop_examples.jpg')
''')

md(r'''
## Done
From **Output**, download for each run: `model_v5a.onnx`, `model_v5a.onnx.data`, `model_v5a_meta.json`, and the same three
for `v5b`, plus `crop_examples.jpg`. Keep `prepared_as_v3.zip` and `prepared_crop.zip` for next time (add them to the
dataset and the downloads are skipped). Then both are graded on the exam days (Feb 1, Feb 5, Mar 8, Mar 10 aren't in
training) against the live model; only a run that beats it goes live. If v5b wins, the site needs the same person
finder before labeling (measured first: it adds time to every upload).
''')

nb = {"cells": [{"cell_type": t, "metadata": {}, "source": s.splitlines(keepends=True),
                 **({"execution_count": None, "outputs": []} if t == "code" else {})} for t, s in CELLS],
      "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                   "language_info": {"name": "python"}, "accelerator": "GPU"},
      "nbformat": 4, "nbformat_minor": 5}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v5_train.ipynb")
json.dump(nb, open(out, "w"), indent=1)
print("wrote", out)
