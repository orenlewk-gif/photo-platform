"""Builds v4_train.ipynb (run: python3 dev/model_training/build_notebook.py). The notebook runs on
Kaggle, Google Colab, or a computer, and trains the color model (v4, then v5, v6...)."""
import json
import os

CELLS = []


def md(text):
    CELLS.append(("markdown", text.strip("\n")))


def code(text):
    CELLS.append(("code", text.strip("\n")))


md(r'''
# Crystal Images — color model training (v4)

Trains the model that labels jacket colors, pants colors and ski/snowboard. Changes from v3:
- photos are turned **upright** (most vertical shots are stored sideways) and **padded to a square** instead of squashed
- the progress check uses **whole days** the model doesn't train on (no near-copies from the same burst)
- **rare colors count more** while training, so you don't have to hunt for them
- each round is scored the way you see it: **"photo needs no fix"**, with the same rules the site uses
- progress is **saved after every round**: if the session stops, run all cells again and it picks up where it left off
- the prepared photos are saved, so **v5 only downloads the new ones**

**Kaggle** (runs in the background — you can close the browser):
1. Add `labels.csv` as a Dataset (Create > New Dataset, private), then in this notebook: Add Input > your dataset.
   For v5+, also add the `prepared_photos.zip` and `model_v4.pt` from this run's output (add the output as input).
2. Add-ons > Secrets: add `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL`, `R2_BUCKET_NAME` (turn each on for this notebook).
3. Settings: Accelerator **GPU T4 x2** (or P100), Internet **on**.
4. **Save Version > Save & Run All (Commit)**. When it finishes, the files are under the version's **Output**.

**Colab** (keep the tab open and the computer awake):
1. Put `labels.csv` in Google Drive at `My Drive/crystal_images/labels.csv` (and `model_v3.pt` there if you have it).
2. Key icon (Secrets) on the left: add the same four names with your R2 values, and allow notebook access.
3. Runtime > Change runtime type > **T4 GPU**, then Runtime > **Run all**.
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
            __import__(p.split('==')[0].replace('-', '_'))
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
from PIL import Image, ImageOps
from torchvision import transforms
from transformers import CLIPModel
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEVICE = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
print('PyTorch', torch.__version__, '| device:', DEVICE,
      '|', torch.cuda.get_device_name(0) if DEVICE == 'cuda' else '')
if DEVICE == 'cpu':
    print('WARNING: no GPU — turn one on (Kaggle: Accelerator; Colab: Runtime type) or this will take days.')
''')

code(r'''
# 2. Settings and where files live
VERSION      = 'v4'            # the files this run makes: model_v4.onnx / model_v4_meta.json / model_v4.pt
START_FROM   = 'v3'            # 'v3' = keep learning from v3's weights (model_v3.pt) if found, else start from plain CLIP
PREP_SIZE    = 256             # prepared photos (upright, padded square); training crops to 224 like the site
PAD_COLOR    = (122, 116, 104) # the neutral color around photos padded to a square (CLIP's average color)
HEAD_EPOCHS  = 3               # only when starting from plain CLIP: train the color/sport layers first
EPOCHS       = 14              # rounds of fine-tuning
BATCH        = 64 if DEVICE == 'cuda' else 16
SMOKE        = os.environ.get('SMOKE_TEST') == '1'    # a quick end-to-end check on a few photos (not for real runs)
if SMOKE:
    EPOCHS, HEAD_EPOCHS = 1, 1
SECOND_JACKET_MIN, SECOND_JACKET_GAP = 0.50, 0.15   # the site's rules for a second jacket color

if ON_KAGGLE:
    found = glob.glob('/kaggle/input/**/labels.csv', recursive=True)
    assert found, 'Add your labels.csv dataset as an Input (Add Input > Your Datasets)'
    LABELS = Path(found[0])
    SAVE   = Path('/kaggle/working')                    # everything here is kept as the version's Output
    INPUTS = [Path(p).parent for p in glob.glob('/kaggle/input/**/*', recursive=True)]
elif ON_COLAB:
    from google.colab import drive
    drive.mount('/content/drive')
    SAVE   = Path('/content/drive/MyDrive/crystal_images')
    LABELS = SAVE / 'labels.csv'
    INPUTS = [SAVE]
else:
    SAVE   = Path('training_run'); LABELS = Path('training_data/labels.csv'); INPUTS = [SAVE, Path('.')]
SAVE.mkdir(parents=True, exist_ok=True)
WORK = Path('/kaggle/temp' if ON_KAGGLE else '/content/work' if ON_COLAB else 'training_run/work')
PHOTOS = WORK / 'prepared'
PHOTOS.mkdir(parents=True, exist_ok=True)
CKPT = SAVE / f'checkpoint_{VERSION}.pt'

def find_input(name):
    """A file from an earlier run (Kaggle inputs, Colab Drive folder, or this computer)."""
    for base in [SAVE] + INPUTS:
        p = base / name
        if p.exists():
            return p
    hits = glob.glob(f'/kaggle/input/**/{name}', recursive=True) if ON_KAGGLE else []
    return Path(hits[0]) if hits else None

def secret(name):
    if ON_KAGGLE:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret(name)
    if ON_COLAB:
        from google.colab import userdata
        return userdata.get(name)
    return os.environ[name]

print('labels:', LABELS, '| saving to:', SAVE)
''')

code(r'''
# 3. Labels, and the days kept back to check progress
ALL_COLORS = ['red','orange','yellow','green','blue','purple','pink',
              'white','black','gray','brown','tan','teal','camo','none','unclear']   # same order as v3 (the site reads it)
SPORTS = ['ski', 'snowboard', 'unclear']
C2I = {c: i for i, c in enumerate(ALL_COLORS)}
S2I = {s: i for i, s in enumerate(SPORTS)}
SAME = {'navy': 'blue', 'teal': 'blue', 'maroon': 'red', 'violet': 'purple'}   # how the site groups shades

def colors(s):
    out = []
    for c in str(s if isinstance(s, str) else '').split('|'):
        c = SAME.get(c.strip().lower(), c.strip().lower())
        if c in C2I and c not in out:
            out.append(c)
    return out

df = pd.read_csv(LABELS)
df['jacket'] = df['jacket_colors'].apply(colors)
df['pants']  = df['pants_colors'].apply(colors)
df['sport_i'] = df['sport'].apply(lambda s: S2I.get(str(s).strip().lower(), S2I['unclear']))
df['day'] = df['r2_key'].str.split('/').str[1]
df = df[df['jacket'].str.len() > 0].reset_index(drop=True)
if SMOKE:
    df = df.groupby('day').head(14).reset_index(drop=True)

# Progress is checked on whole days the model never trains on: the day closest to 12% of the photos
per_day = df['day'].value_counts()
VAL_DAYS = [min(per_day.index, key=lambda d: abs(per_day[d] / len(df) - 0.12))]
is_val = df['day'].isin(VAL_DAYS)
train_df, val_df = df[~is_val].reset_index(drop=True), df[is_val].reset_index(drop=True)
print(f'{len(df)} labeled photos over {len(per_day)} days | training {len(train_df)} | checking on {VAL_DAYS}: {len(val_df)}')
print('jacket colors:', Counter(c for cs in df['jacket'] for c in cs).most_common())
print('sports:', Counter(df['sport']).most_common())
''')

code(r'''
# 4. Download and prepare the photos (upright, padded square, small) — kept for the next version
def prepare(raw: bytes) -> Image.Image:
    """Exactly what the site will do before the model looks at a photo (keep in sync with core/classify.py)."""
    img = Image.open(io.BytesIO(raw))
    if img.format in ('JPEG', 'MPO'):
        img.draft('RGB', (900, 900))            # decode at reduced size (still >= 900 px)
    img = ImageOps.exif_transpose(img.convert('RGB'))   # stand vertical shots up (the camera stores them sideways)
    side = max(img.size)
    square = Image.new('RGB', (side, side), PAD_COLOR)
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return square

def local_name(key):
    return PHOTOS / (key.replace('/', '__').rsplit('.', 1)[0] + '.jpg')

# Photos prepared by an earlier run
old_zip = find_input('prepared_photos.zip')
if old_zip and not any(PHOTOS.iterdir()):
    print('Unpacking photos prepared earlier:', old_zip)
    with zipfile.ZipFile(old_zip) as z:
        z.extractall(PHOTOS)

need = [k for k in df['r2_key'] if not local_name(k).exists()]
print(f'{len(df) - len(need)} photos already prepared, {len(need)} to download')
if need:
    import boto3
    from botocore.config import Config
    s3 = boto3.client('s3', endpoint_url=secret('R2_ENDPOINT_URL'),
                      aws_access_key_id=secret('R2_ACCESS_KEY_ID'),
                      aws_secret_access_key=secret('R2_SECRET_ACCESS_KEY'),
                      config=Config(max_pool_connections=64, retries={'max_attempts': 5}))
    BUCKET = secret('R2_BUCKET_NAME')
    done, failed, lock, t0 = [0], [], threading.Lock(), time.time()
    def fetch(key):
        try:
            raw = s3.get_object(Bucket=BUCKET, Key=key)['Body'].read()
            prepare(raw).resize((PREP_SIZE, PREP_SIZE), Image.LANCZOS).save(local_name(key), 'JPEG', quality=92)
        except Exception as e:
            failed.append((key, str(e)[:80]))
        with lock:
            done[0] += 1
            if done[0] % 500 == 0 or done[0] == len(need):
                print(f'  {done[0]}/{len(need)}  ({(time.time()-t0)/60:.1f} min)', flush=True)
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(fetch, need))
    print(f'Failed: {len(failed)}', failed[:5])
    # Keep them for the next version
    with zipfile.ZipFile(SAVE / 'prepared_photos.zip', 'w', zipfile.ZIP_STORED) as z:
        for p in PHOTOS.iterdir():
            z.write(p, p.name)
    print('Saved', SAVE / 'prepared_photos.zip')

have = df['r2_key'].map(lambda k: local_name(k).exists())
print(f'{have.sum()} of {len(df)} photos ready')
train_df = train_df[train_df['r2_key'].map(lambda k: local_name(k).exists())].reset_index(drop=True)
val_df   = val_df[val_df['r2_key'].map(lambda k: local_name(k).exists())].reset_index(drop=True)
''')

code(r'''
# 5. Photos for training (small random changes so it learns the person, not the exact frame)
MEAN = [0.48145466, 0.4578275, 0.40821073]
STD  = [0.26862954, 0.26130258, 0.27577711]
train_tf = transforms.Compose([
    transforms.RandomResizedCrop(224, scale=(0.85, 1.0), ratio=(0.95, 1.05)),
    transforms.RandomHorizontalFlip(),
    transforms.ColorJitter(brightness=0.15, contrast=0.15),   # no hue/saturation changes: the colors are the answer
    transforms.ToTensor(), transforms.Normalize(MEAN, STD)])
val_tf = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(), transforms.Normalize(MEAN, STD)])

def multihot(cs):
    v = np.zeros(len(ALL_COLORS), dtype=np.float32)
    for c in cs:
        v[C2I[c]] = 1
    return v

class Photos(Dataset):
    def __init__(self, frame, tf):
        self.keys = frame['r2_key'].tolist(); self.tf = tf
        self.j = np.stack(frame['jacket'].apply(multihot)); self.p = np.stack(frame['pants'].apply(multihot))
        self.s = frame['sport_i'].to_numpy()
    def __len__(self): return len(self.keys)
    def __getitem__(self, i):
        img = Image.open(local_name(self.keys[i])).convert('RGB')
        return self.tf(img), self.j[i], self.p[i], self.s[i]

WORKERS = 4 if DEVICE == 'cuda' else 0
train_dl = DataLoader(Photos(train_df, train_tf), batch_size=BATCH, shuffle=True, num_workers=WORKERS,
                      pin_memory=DEVICE == 'cuda', drop_last=True, persistent_workers=WORKERS > 0)
val_dl = DataLoader(Photos(val_df, val_tf), batch_size=BATCH, shuffle=False, num_workers=WORKERS,
                    pin_memory=DEVICE == 'cuda', persistent_workers=WORKERS > 0)
print(f'{len(train_dl)} training batches, {len(val_dl)} checking batches')
''')

code(r'''
# 6. The model (same shape as v3, so the site loads it the same way)
class ColorClassifier(nn.Module):
    def __init__(self, clip_model, n_colors=16, n_sports=3, dropout=0.3):
        super().__init__()
        self.vision = clip_model.vision_model
        self.proj = clip_model.visual_projection
        head = lambda out: nn.Sequential(nn.Linear(512, 256), nn.ReLU(), nn.Dropout(dropout), nn.Linear(256, out))
        self.jacket_head, self.pants_head, self.sport_head = head(n_colors), head(n_colors), head(n_sports)
    def forward(self, pixel_values):
        feat = self.proj(self.vision(pixel_values=pixel_values).pooler_output)
        feat = feat / feat.norm(dim=-1, keepdim=True)
        return self.jacket_head(feat), self.pants_head(feat), self.sport_head(feat)

model = ColorClassifier(CLIPModel.from_pretrained('openai/clip-vit-base-patch32')).to(DEVICE)
start = find_input('model_v3.pt') if START_FROM == 'v3' else None
if start:
    model.load_state_dict(torch.load(start, map_location=DEVICE))
    print('Starting from', start)
else:
    print('Starting from plain CLIP')
FROM_CLIP = start is None
''')

code(r'''
# 7. Scoring the way you see it: does a photo need a fix? (the site's rules)
def decide(j_prob, p_prob, s_logit):
    o = np.argsort(j_prob)[::-1]
    jacket = {ALL_COLORS[o[0]]}
    if j_prob[o[1]] >= SECOND_JACKET_MIN and j_prob[o[0]] - j_prob[o[1]] < SECOND_JACKET_GAP:
        jacket.add(ALL_COLORS[o[1]])
    return jacket, {ALL_COLORS[int(np.argmax(p_prob))]}, int(np.argmax(s_logit))

@torch.no_grad()
def check(dl):
    model.eval()
    J, P, S, TJ, TP, TS = [], [], [], [], [], []
    for x, j, p, s in dl:
        with torch.autocast('cuda', enabled=DEVICE == 'cuda'):
            oj, op, os_ = model(x.to(DEVICE))
        J.append(torch.sigmoid(oj.float()).cpu().numpy()); P.append(torch.sigmoid(op.float()).cpu().numpy())
        S.append(os_.float().cpu().numpy()); TJ.append(j.numpy()); TP.append(p.numpy()); TS.append(s.numpy())
    J, P, S, TJ, TP, TS = map(np.concatenate, (J, P, S, TJ, TP, TS))
    jok = pok = sok = allok = 0
    per = Counter(); tot = Counter()
    for i in range(len(J)):
        dj, dp, ds = decide(J[i], P[i], S[i])
        tj = {ALL_COLORS[k] for k in np.flatnonzero(TJ[i])}; tp = {ALL_COLORS[k] for k in np.flatnonzero(TP[i])}
        a, b, c = dj == tj, dp == tp, ds == TS[i]
        jok += a; pok += b; sok += c; allok += a and b and c
        for col in tj:
            tot[col] += 1; per[col] += col in dj
    n = len(J)
    return dict(no_fix=allok / n, jacket=jok / n, pants=pok / n, sport=sok / n,
                per_color={c: (per[c] / tot[c], tot[c]) for c in tot})

fmt = lambda m: (f"photo needs no fix {m['no_fix']*100:.1f}%  (jacket {m['jacket']*100:.1f}%  "
                 f"pants {m['pants']*100:.1f}%  sport {m['sport']*100:.1f}%)")

# Rare colors count more (the square root of how rare, at most 10x); same for snowboard
def pos_weight(col):
    pos = np.stack(train_df[col].apply(multihot)).sum(0)
    return torch.tensor(np.clip(np.sqrt((len(train_df) - pos) / np.maximum(pos, 1)), 1, 10), dtype=torch.float32)
bce_j = nn.BCEWithLogitsLoss(pos_weight=pos_weight('jacket').to(DEVICE))
bce_p = nn.BCEWithLogitsLoss(pos_weight=pos_weight('pants').to(DEVICE))
sc = np.bincount(train_df['sport_i'], minlength=3).astype(float)
ce_s = nn.CrossEntropyLoss(weight=torch.tensor(np.clip(np.sqrt(sc.max() / np.maximum(sc, 1)), 1, 5), dtype=torch.float32).to(DEVICE))
loss_fn = lambda out, j, p, s: bce_j(out[0], j) + bce_p(out[1], p) + ce_s(out[2], s)

print('Before training:', fmt(check(val_dl)))
''')

code(r'''
# 8. Training — saved after every round; if the session stops, run all again and it continues
def set_trainable(heads_only):
    for p in model.parameters():
        p.requires_grad = False
    for h in (model.jacket_head, model.pants_head, model.sport_head):
        for p in h.parameters(): p.requires_grad = True
    if not heads_only:
        for layer in model.vision.encoder.layers[-4:]:
            for p in layer.parameters(): p.requires_grad = True
        for p in list(model.vision.post_layernorm.parameters()) + list(model.proj.parameters()):
            p.requires_grad = True

def make_opt(heads_only):
    heads = [p for h in (model.jacket_head, model.pants_head, model.sport_head) for p in h.parameters()]
    if heads_only:
        return torch.optim.AdamW(heads, lr=3e-4, weight_decay=1e-4)
    body = [p for p in list(model.vision.parameters()) + list(model.proj.parameters()) if p.requires_grad]
    return torch.optim.AdamW([{'params': body, 'lr': 1e-5}, {'params': heads, 'lr': 1e-4}], weight_decay=1e-4)

plan = (['heads'] * HEAD_EPOCHS if FROM_CLIP else []) + ['fine'] * EPOCHS
state = {'round': 0, 'best': -1.0, 'history': []}
resume = None
if CKPT.exists():
    resume = torch.load(CKPT, map_location=DEVICE, weights_only=False)
    model.load_state_dict(resume['model']); state = resume['state']
    print(f"Continuing after round {state['round']} of {len(plan)} (best so far {state['best']*100:.1f}%)")

scaler = torch.cuda.amp.GradScaler(enabled=DEVICE == 'cuda')
opt = sched = None; phase = None
for r in range(state['round'], len(plan)):
    if plan[r] != phase:
        phase = plan[r]; set_trainable(phase == 'heads'); opt = make_opt(phase == 'heads')
        n_phase = plan.count(phase); done_in_phase = r - plan.index(phase)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=n_phase, eta_min=1e-7)
        if resume is not None and resume.get('phase') == phase:   # continuing mid-phase after a stop
            opt.load_state_dict(resume['opt'])
        resume = None
        for _ in range(done_in_phase): sched.step()
    model.train(); t0 = time.time(); total = 0.0
    for x, j, p, s in train_dl:
        x, j, p, s = x.to(DEVICE), j.to(DEVICE), p.to(DEVICE), s.to(DEVICE)
        opt.zero_grad(set_to_none=True)
        with torch.autocast('cuda', enabled=DEVICE == 'cuda'):
            out = model(x)
        loss = loss_fn([o.float() for o in out], j, p, s)
        scaler.scale(loss).backward(); scaler.unscale_(opt)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        scaler.step(opt); scaler.update(); total += loss.item()
    sched.step()
    m = check(val_dl)
    better = m['no_fix'] > state['best']
    if better:
        state['best'] = m['no_fix']
        torch.save(model.state_dict(), SAVE / f'model_{VERSION}.pt')
    state['round'] = r + 1
    state['history'].append({'round': r + 1, 'phase': phase, 'loss': total / len(train_dl), **{k: m[k] for k in ('no_fix', 'jacket', 'pants', 'sport')}})
    torch.save({'model': model.state_dict(), 'opt': opt.state_dict(), 'phase': phase, 'state': state}, CKPT)
    print(f"[round {r+1}/{len(plan)} {phase}] {(time.time()-t0)/60:.1f} min  loss {total/len(train_dl):.3f}  {fmt(m)}"
          + ('  <-- best' if better else ''), flush=True)

print(f"\nBest: photo needs no fix {state['best']*100:.1f}% on {VAL_DAYS}")
''')

code(r'''
# 9. Report card for the best version
model.load_state_dict(torch.load(SAVE / f'model_{VERSION}.pt', map_location=DEVICE))
m = check(val_dl)
print(fmt(m))
print('\nJacket: how often each true color is included')
for c, (rate, n) in sorted(m['per_color'].items(), key=lambda kv: -kv[1][1]):
    print(f'  {c:<8} {rate*100:5.1f}%  (n={n})')
''')

code(r'''
# 10. Files for the site: model_v4.onnx + model_v4_meta.json (+ model_v4.pt for the next version)
import onnx, onnxruntime
model.eval().cpu()
onnx_path = SAVE / f'model_{VERSION}.onnx'
dummy = torch.randn(1, 3, 224, 224)
torch.onnx.export(model, dummy, str(onnx_path), opset_version=14, input_names=['pixel_values'],
                  output_names=['jacket_logits', 'pants_logits', 'sport_logits'],
                  dynamic_axes={k: {0: 'batch'} for k in ('pixel_values', 'jacket_logits', 'pants_logits', 'sport_logits')})
onnx.checker.check_model(onnx.load(str(onnx_path)))
meta = {'version': VERSION, 'base_model': 'openai/clip-vit-base-patch32', 'all_colors': ALL_COLORS, 'sports': SPORTS,
        'color_to_idx': C2I, 'sport_to_idx': S2I, 'best_score': float(state['best']), 'score': 'photo needs no fix (site rules)',
        'checked_on_days': VAL_DAYS, 'clip_mean': MEAN, 'clip_std': STD, 'clip_size': 224,
        'preprocess': {'decode_min_px': 900, 'upright': True, 'pad_square': True, 'pad_color': list(PAD_COLOR)},
        'history': state['history']}
json.dump(meta, open(SAVE / f'model_{VERSION}_meta.json', 'w'), indent=2)

# The exported file gives the same answers as the trained model
sess = onnxruntime.InferenceSession(str(onnx_path), providers=['CPUExecutionProvider'])
x = next(iter(val_dl))[0][:4]
ref = [o.detach().numpy() for o in model(x)]
got = sess.run(None, {'pixel_values': x.numpy()})
print('ONNX matches:', all(np.allclose(a, b, atol=1e-3) for a, b in zip(ref, got)))
t0 = time.time()
for _ in range(20): sess.run(None, {'pixel_values': dummy.numpy()})
print(f'{(time.time()-t0)/20*1000:.0f} ms per photo on CPU')
for f in sorted(SAVE.glob(f'model_{VERSION}*')):
    print(f'  {f.name}  {f.stat().st_size/1e6:.1f} MB')
''')

md(r'''
## Done
Download `model_v4.onnx`, `model_v4_meta.json` (and `model_v4.onnx.data` if there is one) and send them over (or put them on the Desktop).
It isn't live yet: next it's graded on 6/7, the day it never saw, against v3's 66.6%.
Keep `model_v4.pt` and `prepared_photos.zip` for v5.
''')

nb = {"cells": [{"cell_type": t, "metadata": {}, "source": s.splitlines(keepends=True),
                 **({"execution_count": None, "outputs": []} if t == "code" else {})} for t, s in CELLS],
      "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                   "language_info": {"name": "python"}, "accelerator": "GPU"},
      "nbformat": 4, "nbformat_minor": 5}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4_train.ipynb")
json.dump(nb, open(out, "w"), indent=1)
print("wrote", out)
