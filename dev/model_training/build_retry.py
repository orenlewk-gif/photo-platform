"""Builds v4_retry.ipynb (run: python3 dev/model_training/build_retry.py).

The first v4 changed six things at once and came out worse than v3 (53.8% vs 58.7% "photo needs
no fix" on the exam days). This notebook goes back to v3's exact recipe and changes one thing per run:
  Run A (v4a): v3's recipe, v3's photo preparation, just more photos
  Run B (v4b): the same, with photos turned upright (still filling the frame, no padding)
Both run back to back in one session; each is checked on real days v3 never trained on."""
import json
import os

CELLS = []


def md(text):
    CELLS.append(("markdown", text.strip("\n")))


def code(text):
    CELLS.append(("code", text.strip("\n")))


md(r'''
# Crystal Images — color model v4, second try (two careful runs)

The first v4 changed several things at once and came out worse than v3. This one goes back to
**v3's exact recipe** and changes **one thing per run**:

| Run | What's different from v3 |
|---|---|
| **v4a** | only more photos (photos prepared exactly the way v3 and the site do it) |
| **v4b** | more photos **and photos turned upright** (still filling the frame — no padding) |

Both use v3's learning speed, rounds and small photo variations, no extra weights, and start from v3's weights.
Progress is checked on **real days v3 never trained on** (Jan 23 and Mar 7). The exam days (Feb 5, Mar 8) aren't in labels.csv at all.

**Kaggle:** the same dataset with the **new labels.csv** (New Version > replace labels.csv; keep model_v3.pt), the same four
Secrets, GPU T4 x2, Internet on, then **Save Version > Save & Run All**. About an hour; you can close the browser.
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
from PIL import Image, ImageOps
from torchvision import transforms
from transformers import CLIPModel
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEVICE = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
print('PyTorch', torch.__version__, '| device:', DEVICE, '|', torch.cuda.get_device_name(0) if DEVICE == 'cuda' else '')
''')

code(r'''
# 2. Settings
RUNS = [('v4a', 'as_v3'),      # v3's photo preparation (what the site does today)
        ('v4b', 'upright')]    # the same, turned upright
PROGRESS_DAYS = {'2026-01-23', '2026-03-07'}   # real capture days v3 never trained on
# v3's recipe (clip_finetune.ipynb, round 3): last 4 vision blocks + projection at 5e-6, heads at 1e-4,
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
# 3. Labels; progress-check photos are the real days v3 never trained on
ALL_COLORS = ['red','orange','yellow','green','blue','purple','pink',
              'white','black','gray','brown','tan','teal','camo','none','unclear']   # v3's order (the site reads it)
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
assert 'taken' in df.columns, 'This labels.csv is the old one — export again (it now includes each photo\'s real date)'
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
''')

code(r'''
# 4. Download each photo once and prepare it both ways (kept for v5)
PREPS = sorted({p for _, p in RUNS})

def prepare(raw, prep):
    img = Image.open(io.BytesIO(raw))
    if img.format in ('JPEG', 'MPO'):
        img.draft('RGB', (900, 900))         # reduced decode, as the site does
    img = img.convert('RGB')
    if prep == 'upright':
        img = ImageOps.exif_transpose(img)   # stand vertical shots up
    return img.resize((224, 224), Image.LANCZOS)   # fill the frame, exactly like the site (no padding)

def local_name(key, prep):
    return WORK / prep / (key.replace('/', '__').rsplit('.', 1)[0] + '.png')

for prep in PREPS:
    (WORK / prep).mkdir(parents=True, exist_ok=True)
    old_zip = find_input(f'prepared_{prep}.zip')
    if old_zip and not any((WORK / prep).iterdir()):
        print('Unpacking photos prepared earlier:', old_zip)
        with zipfile.ZipFile(old_zip) as z:
            z.extractall(WORK / prep)

need = [k for k in df['r2_key'] if not all(local_name(k, p).exists() for p in PREPS)]
print(f'{len(df) - len(need)} photos already prepared, {len(need)} to download')
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
            raw = s3.get_object(Bucket=BUCKET, Key=key)['Body'].read()
            for prep in PREPS:
                prepare(raw, prep).save(local_name(key, prep))   # PNG: no extra compression on top
        except Exception as e:
            failed.append((key, str(e)[:80]))
        with lock:
            done[0] += 1
            if done[0] % 500 == 0 or done[0] == len(need):
                print(f'  {done[0]}/{len(need)}  ({(time.time()-t0)/60:.1f} min)', flush=True)
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(fetch, need))
    print(f'Failed: {len(failed)}', failed[:5])
    for prep in PREPS:
        with zipfile.ZipFile(SAVE / f'prepared_{prep}.zip', 'w', zipfile.ZIP_STORED) as z:
            for p in (WORK / prep).iterdir():
                z.write(p, p.name)
ready = lambda frame: frame[frame['r2_key'].map(lambda k: all(local_name(k, p).exists() for p in PREPS))].reset_index(drop=True)
train_df, check_df = ready(train_df), ready(check_df)
print(f'ready: training {len(train_df)}, progress check {len(check_df)}')
''')

code(r'''
# 5. Photos, model and scoring — v3's exact setup
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
    def __init__(self, clip_model, n_colors=16, n_sports=3, dropout=0.3):
        super().__init__()
        self.vision, self.proj = clip_model.vision_model, clip_model.visual_projection
        head = lambda out: nn.Sequential(nn.Linear(512, 256), nn.ReLU(), nn.Dropout(dropout), nn.Linear(256, out))
        self.jacket_head, self.pants_head, self.sport_head = head(n_colors), head(n_colors), head(n_sports)
    def forward(self, pixel_values):
        feat = self.proj(self.vision(pixel_values=pixel_values).pooler_output)
        feat = feat / feat.norm(dim=-1, keepdim=True)
        return self.jacket_head(feat), self.pants_head(feat), self.sport_head(feat)

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
# 6. Train both runs (each saved after every round; run all again after a stop and it continues)
import onnx, onnxruntime
results = {}
for name, prep in RUNS:
    print(f'\n========== {name}: {"v3 preparation" if prep == "as_v3" else "turned upright"} ==========')
    workers = 4 if DEVICE == 'cuda' else 0
    train_dl = DataLoader(Photos(train_df, prep, train_tf), batch_size=BATCH, shuffle=True, num_workers=workers,
                          pin_memory=DEVICE == 'cuda', drop_last=True)
    check_dl = DataLoader(Photos(check_df, prep, check_tf), batch_size=64, shuffle=False, num_workers=workers)
    model = ColorClassifier(CLIPModel.from_pretrained('openai/clip-vit-base-patch32')).to(DEVICE)
    if V3_WEIGHTS:
        model.load_state_dict(torch.load(V3_WEIGHTS, map_location=DEVICE))
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
        print('v3 on the progress-check days, prepared this way:', fmt(start))
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

    # The best round, as files for the site (same layout as v3)
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
               'preprocess': {'decode_min_px': 900, 'upright': prep == 'upright', 'pad_square': False, 'fill_frame': True},
               'start': state.get('start'), 'history': state['history']},
              open(SAVE / f'model_{name}_meta.json', 'w'), indent=2)
    results[name] = state
    print(f"{name}: best {fmt(state['history'][int(np.argmax([h['no_fix'] for h in state['history']]))])}  | ONNX matches: {same}")
    model.to(DEVICE)
''')

code(r'''
# 7. Summary
for name, prep in RUNS:
    st = results[name]
    best = max(st['history'], key=lambda h: h['no_fix'])
    print(f"{name} ({'v3 preparation' if prep == 'as_v3' else 'upright'}): v3 started at {st['start']['no_fix']*100:.1f}%, "
          f"best round {best['round']}: {fmt(best)}")
print('\nFiles to download (Output):', ', '.join(sorted(f.name for f in SAVE.glob('model_v4*'))))
''')

md(r'''
## Done
From **Output**, download for each run: `model_v4a.onnx`, `model_v4a.onnx.data`, `model_v4a_meta.json`, and the same three
for `v4b`. Keep `prepared_as_v3.zip` and `prepared_upright.zip` for v5. Then both get graded on the exam days (Feb 5, Mar 8)
against v3's 58.7%; only a run that beats it goes live.
''')

nb = {"cells": [{"cell_type": t, "metadata": {}, "source": s.splitlines(keepends=True),
                 **({"execution_count": None, "outputs": []} if t == "code" else {})} for t, s in CELLS],
      "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                   "language_info": {"name": "python"}, "accelerator": "GPU"},
      "nbformat": 4, "nbformat_minor": 5}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4_retry.ipynb")
json.dump(nb, open(out, "w"), indent=1)
print("wrote", out)
