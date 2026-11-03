"""Builds v6_train.ipynb (run: python3 dev/model_training/build_v6.py).

v5a (v3's recipe + all labels, camo folded into green) labels uploads now; on Oren's real uploads it
needs no fix on 66.7% of photos (6,209 photos in 11 checked folders, 2026-11-03). v6 keeps that recipe.
What's new (Oren 2026-11-03): ~6k more labels, and nothing is held back any more (the old exam and
progress-check days go into training) because real uploads are the test now. Tan is folded into brown
(14 colors). Two runs in one session:
  Run A (v6check): everything except the real uploads v5a labeled; scored on those each round, so v6 is
                   compared with v5a on the same photos, and the best number of rounds is found.
  Run B (v6):      everything, trained for that many rounds. This is the one that goes live."""
import json
import os

CELLS = []


def md(text):
    CELLS.append(("markdown", text.strip("\n")))


def code(text):
    CELLS.append(("code", text.strip("\n")))


md(r'''
# Crystal Images — color model v6

Same recipe as v5a (the model labeling uploads now). What's different:

* **every labeled photo** goes in, including the days that used to be held back (the old exam and progress-check days);
* **tan is part of brown** (14 colors instead of 15), like camo -> green before.

Two runs, one after the other:

| Run | Trains on | Checked on |
|---|---|---|
| **v6check** | everything **except** your real uploads that v5a labeled | those real uploads, after every round: v6 vs v5a on the very same photos, and which round is best |
| **v6** | **everything**, for the best number of rounds from v6check | (nothing unseen is left; it's judged on your next uploads) |

Both start from v3's weights (camo and tan removed), with v3's learning speed and small photo variations.

**Kaggle:** the dataset with the **new labels.csv** (New Version > replace labels.csv; keep model_v3.pt; add
`prepared_as_v3.zip` from the v5 run's Output if you have it, and only the new photos are downloaded), the same four
Secrets, **GPU T4 x2, Internet on**, then **Save Version > Save & Run All**. You can close the browser.
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
from PIL import Image
from torchvision import transforms
from transformers import CLIPModel
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEVICE = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
print('PyTorch', torch.__version__, '| device:', DEVICE, '|', torch.cuda.get_device_name(0) if DEVICE == 'cuda' else '')
''')

code(r'''
# 2. Settings
CHECK_MODEL = 'model_v5a'      # real uploads this model labeled (and Oren checked) are v6check's test photos
# v3's recipe (also v4a's and v5a's): last 4 vision blocks + projection at 5e-6, heads at 1e-4,
# AdamW (weight decay 1e-4), cosine over 10 rounds, batch 32, flip + light color jitter, plain losses
ROUNDS, BODY_LR, HEAD_LR, BATCH = 10, 5e-6, 1e-4, 32
SECOND_JACKET_MIN, SECOND_JACKET_GAP = 0.50, 0.15   # the site's rules for a second jacket color
SMOKE = os.environ.get('SMOKE_TEST') == '1'         # a quick end-to-end check on a few photos (not for real runs)
if SMOKE:
    ROUNDS = 2

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
# 3. Labels (camo is part of green, tan is part of brown). The real uploads v5a labeled are the check photos.
ALL_COLORS = ['red','orange','yellow','green','blue','purple','pink',
              'white','black','gray','brown','teal','none','unclear']   # v3's order without camo and tan (the site reads it from the meta file)
V3_COLORS = ['red','orange','yellow','green','blue','purple','pink',
             'white','black','gray','brown','tan','teal','camo','none','unclear']   # v3's heads, for loading its weights
SPORTS = ['ski', 'snowboard', 'unclear']
C2I = {c: i for i, c in enumerate(ALL_COLORS)}
S2I = {s: i for i, s in enumerate(SPORTS)}
SAME = {'navy': 'blue', 'teal': 'blue', 'maroon': 'red', 'violet': 'purple', 'camo': 'green', 'tan': 'brown'}   # how the site groups shades

def colors(s):
    out = []
    for c in str(s if isinstance(s, str) else '').split('|'):
        c = SAME.get(c.strip().lower(), c.strip().lower())
        if c in C2I and c not in out:
            out.append(c)
    return out

df = pd.read_csv(LABELS)
assert 'labeled_by' in df.columns, 'This labels.csv is the old one — export again (it says which model labeled each upload)'
df['jacket'] = df['jacket_colors'].apply(colors)
df['pants'] = df['pants_colors'].apply(colors)
df['sport_i'] = df['sport'].apply(lambda s: S2I.get(str(s).strip().lower(), S2I['unclear']))
df['real_day'] = df['taken'].fillna('').astype(str).str[:10]
df = df[df['jacket'].str.len() > 0].reset_index(drop=True)
if SMOKE:
    df = pd.concat([df[df['labeled_by'] == CHECK_MODEL].head(16), df[df['labeled_by'] != CHECK_MODEL].head(48)]).reset_index(drop=True)
is_check = df['labeled_by'].fillna('') == CHECK_MODEL
rest_df, check_df = df[~is_check].reset_index(drop=True), df[is_check].reset_index(drop=True)
print(f'{len(df)} labeled photos from {df["real_day"].nunique()} real days | real uploads {CHECK_MODEL} labeled: {len(check_df)} | '
      f'the rest: {len(rest_df)}')
print('jacket colors:', dict(Counter(c for cs in df['jacket'] for c in cs).most_common()))

# What v5a itself said on those photos (kept on each photo when it was uploaded), scored the same way
def v5a_score(frame):
    ok = Counter()
    for _, r in frame.iterrows():
        a = set(colors(r['model_jacket'])) == set(r['jacket'])
        b = set(colors(r['model_pants'])) == set(r['pants'])
        c = str(r['model_sport']).strip().lower() == SPORTS[r['sport_i']]
        ok['jacket'] += a; ok['pants'] += b; ok['sport'] += c; ok['no_fix'] += a and b and c
    return {k: ok[k] / max(1, len(frame)) for k in ('no_fix', 'jacket', 'pants', 'sport')}
V5A_ON_CHECK = v5a_score(check_df)
''')

code(r'''
# 4. Download each photo once, prepared as the site prepares it (fills the frame, as stored, 224px).
#    The prepared photos are zipped for next time (add prepared_as_v3.zip to the dataset to skip this).
def decoded(raw):
    img = Image.open(io.BytesIO(raw))
    if img.format in ('JPEG', 'MPO'):
        img.draft('RGB', (900, 900))         # reduced decode, as the site does
    return img.convert('RGB')

def local_name(key):
    return WORK / 'as_v3' / (key.replace('/', '__').rsplit('.', 1)[0] + '.png')

(WORK / 'as_v3').mkdir(parents=True, exist_ok=True)
old_zip = find_input('prepared_as_v3.zip')
if old_zip and not any((WORK / 'as_v3').iterdir()):
    print('Unpacking photos prepared earlier:', old_zip)
    with zipfile.ZipFile(old_zip) as z:
        z.extractall(WORK / 'as_v3')

need = [k for k in df['r2_key'] if not local_name(k).exists()]
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
            decoded(s3.get_object(Bucket=BUCKET, Key=key)['Body'].read()).resize((224, 224), Image.LANCZOS).save(local_name(key))
        except Exception as e:
            failed.append((key, str(e)[:80]))
        with lock:
            done[0] += 1
            if done[0] % 500 == 0 or done[0] == len(need):
                print(f'  {done[0]}/{len(need)}  ({(time.time()-t0)/60:.1f} min)', flush=True)
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(fetch, need))
    print(f'Failed: {len(failed)}', failed[:5])
    with zipfile.ZipFile(SAVE / 'prepared_as_v3.zip', 'w', zipfile.ZIP_STORED) as z:
        for p in (WORK / 'as_v3').iterdir():
            z.write(p, p.name)
ready = lambda frame: frame[frame['r2_key'].map(lambda k: local_name(k).exists())].reset_index(drop=True)
df, rest_df, check_df = ready(df), ready(rest_df), ready(check_df)
print(f'ready: everything {len(df)} | v6check trains on {len(rest_df)}, checked on {len(check_df)} real uploads')
''')

code(r'''
# 5. Photos, model and scoring — v3's exact setup (14 colors)
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
    def __init__(self, frame, tf):
        self.keys, self.tf = frame['r2_key'].tolist(), tf
        self.j = np.stack(frame['jacket'].apply(multihot)); self.p = np.stack(frame['pants'].apply(multihot))
        self.s = frame['sport_i'].to_numpy()
    def __len__(self): return len(self.keys)
    def __getitem__(self, i):
        return self.tf(Image.open(local_name(self.keys[i])).convert('RGB')), self.j[i], self.p[i], self.s[i]

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

def load_v3_trimmed(model, path):
    """v3's weights with camo's and tan's outputs removed from the jacket and pants heads (16 -> 14 colors)."""
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
print(f'v5a on the {len(check_df)} real uploads (what it said at upload):', fmt(V5A_ON_CHECK))
''')

code(r'''
# 6. Train (each run is saved after every round; run all again after a stop and it continues)
import onnx, onnxruntime
workers = 4 if DEVICE == 'cuda' else 0

def new_model():
    model = ColorClassifier(CLIPModel.from_pretrained('openai/clip-vit-base-patch32'))
    if V3_WEIGHTS:
        load_v3_trimmed(model, V3_WEIGHTS)
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
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=ROUNDS, eta_min=1e-7)   # same schedule in both runs
    return model, opt, sched

def train(name, frame, stop_after, check_dl=None):
    """Train on `frame` for `stop_after` rounds (of the same 10-round schedule); with check_dl, score every round
    and keep the best round's weights, else keep the last round's."""
    train_dl = DataLoader(Photos(frame, train_tf), batch_size=BATCH, shuffle=True, num_workers=workers,
                          pin_memory=DEVICE == 'cuda', drop_last=len(frame) > BATCH)
    model, opt, sched = new_model()
    ckpt = SAVE / f'checkpoint_{name}.pt'
    state = {'round': 0, 'best': -1.0, 'best_round': 0, 'history': []}
    if ckpt.exists():
        ck = torch.load(ckpt, map_location=DEVICE, weights_only=False)
        model.load_state_dict(ck['model']); opt.load_state_dict(ck['opt']); sched.load_state_dict(ck['sched'])
        state = ck['state']
        print(f"{name}: continuing after round {state['round']}")
    if check_dl is not None and state['round'] == 0:
        state['start'] = check(model, check_dl)
        print('v3 (camo and tan removed) on the real uploads:', fmt(state['start']))
    for r in range(state['round'], stop_after):
        model.train(); t0 = time.time(); total = 0.0
        for x, j, p, s in train_dl:
            x, j, p, s = x.to(DEVICE), j.to(DEVICE), p.to(DEVICE), s.to(DEVICE)
            opt.zero_grad()
            loss = loss_fn(model(x), j, p, s)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); total += loss.item()
        sched.step()
        line = f"[{name} round {r+1}/{stop_after}] {(time.time()-t0)/60:.1f} min  loss {total/len(train_dl):.3f}"
        if check_dl is not None:
            m = check(model, check_dl)
            better = m['no_fix'] > state['best']
            if better:
                state['best'], state['best_round'] = m['no_fix'], r + 1
                torch.save(model.state_dict(), SAVE / f'model_{name}.pt')
            state['history'].append({'round': r + 1, 'loss': total / len(train_dl), **m})
            line += '  ' + fmt(m) + ('  <-- best' if better else '')
        else:
            torch.save(model.state_dict(), SAVE / f'model_{name}.pt')
            state['history'].append({'round': r + 1, 'loss': total / len(train_dl)})
        state['round'] = r + 1
        torch.save({'model': model.state_dict(), 'opt': opt.state_dict(), 'sched': sched.state_dict(), 'state': state}, ckpt)
        print(line, flush=True)
    return state

# Run A: everything except the real uploads, checked on them every round
check_dl = DataLoader(Photos(check_df, check_tf), batch_size=64, shuffle=False, num_workers=workers)
print(f'\n========== v6check: trains on {len(rest_df)}, checked on {len(check_df)} real uploads ==========')
A = train('v6check', rest_df, ROUNDS, check_dl)
BEST_ROUNDS = A['best_round']
print(f"v6check best round {BEST_ROUNDS}: {fmt(A['history'][BEST_ROUNDS - 1])}")
print(f"v5a on the same photos:      {fmt(V5A_ON_CHECK)}")

# Run B: everything, for that many rounds
print(f'\n========== v6: trains on everything ({len(df)}) for {BEST_ROUNDS} rounds ==========')
B = train('v6', df, BEST_ROUNDS)
''')

code(r'''
# 7. v6 as files for the site (same layout as v5a)
model = ColorClassifier(CLIPModel.from_pretrained('openai/clip-vit-base-patch32'))
model.load_state_dict(torch.load(SAVE / 'model_v6.pt', map_location='cpu'))
model.eval()
dummy = torch.randn(1, 3, 224, 224)
path = SAVE / 'model_v6.onnx'
torch.onnx.export(model, dummy, str(path), opset_version=18, input_names=['pixel_values'],
                  output_names=['jacket_logits', 'pants_logits', 'sport_logits'],
                  dynamic_axes={k: {0: 'batch'} for k in ('pixel_values', 'jacket_logits', 'pants_logits', 'sport_logits')})
onnx.checker.check_model(onnx.load(str(path)))
sess = onnxruntime.InferenceSession(str(path), providers=['CPUExecutionProvider'])
x = next(iter(check_dl))[0][:4]
same = all(np.allclose(a.detach().numpy(), b, atol=1e-3) for a, b in zip(model(x), sess.run(None, {'pixel_values': x.numpy()})))
best = A['history'][BEST_ROUNDS - 1]
json.dump({'version': 'v6', 'base_model': 'openai/clip-vit-base-patch32', 'all_colors': ALL_COLORS, 'sports': SPORTS,
           'color_to_idx': C2I, 'sport_to_idx': S2I, 'rounds': BEST_ROUNDS, 'trained_on': len(df),
           'score': 'photo needs no fix (site rules)',
           'check': {'how': f'v6check (same recipe without the {len(check_df)} real uploads {CHECK_MODEL} labeled), scored on them',
                     'v6check': best, CHECK_MODEL: V5A_ON_CHECK, 'v3_start': A.get('start'), 'history': A['history']},
           'clip_mean': MEAN, 'clip_std': STD, 'clip_size': 224,
           'preprocess': {'decode_min_px': 900, 'upright': False, 'pad_square': False, 'fill_frame': True, 'crop_person': False}},
          open(SAVE / 'model_v6_meta.json', 'w'), indent=2)
print(f"ONNX matches: {same}")
print(f"\nOn {len(check_df)} real uploads: v5a {V5A_ON_CHECK['no_fix']*100:.1f}%  ->  v6check {best['no_fix']*100:.1f}% "
      f"(round {BEST_ROUNDS}). v6 = the same recipe on everything, {BEST_ROUNDS} rounds.")
print('Files to download (Output): model_v6.onnx, model_v6.onnx.data, model_v6_meta.json (+ model_v6.pt, prepared_as_v3.zip to keep)')
''')

md(r'''
## Done
From **Output**, download `model_v6.onnx`, `model_v6.onnx.data` and `model_v6_meta.json` (the meta may arrive as .txt;
rename it .json). Keep `model_v6.pt` and `prepared_as_v3.zip` for next time. The last cell printed v6check against v5a on
your real uploads: if v6check is better, v6 goes on uploads and is judged on your next checked folders (Settings >
Color Labels, Model accuracy on real uploads).
''')

nb = {"cells": [{"cell_type": t, "metadata": {}, "source": s.splitlines(keepends=True),
                 **({"execution_count": None, "outputs": []} if t == "code" else {})} for t, s in CELLS],
      "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                   "language_info": {"name": "python"}, "accelerator": "GPU"},
      "nbformat": 4, "nbformat_minor": 5}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v6_train.ipynb")
json.dump(nb, open(out, "w"), indent=1)
print("wrote", out)
