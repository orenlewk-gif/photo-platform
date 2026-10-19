"""Score v3 against Oren's corrected labels, the way he experiences it: does a photo need a fix?"""
import json, os, sys, itertools
import numpy as np
from collections import Counter, defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))
meta = json.load(open("/Users/orenlewkowski/Desktop/photo-platform/model_v3_meta.json"))
C, S = meta["all_colors"], meta["sports"]
FAM = {"navy": "blue", "teal": "blue", "maroon": "red", "violet": "purple"}
norm = lambda cs: frozenset(FAM.get(c, c) for c in (cs or []) if c) or frozenset(["none"])
rows = [json.loads(l) for l in open(os.path.join(HERE, "results.jsonl"))]
rows = [r for r in rows if "error" not in r]
sig = lambda x: 1 / (1 + np.exp(-np.asarray(x)))
def probs(r, how):
    if how == "flipavg":
        return (sig(r["live"]["j"]) + sig(r["flip"]["j"])) / 2, (sig(r["live"]["p"]) + sig(r["flip"]["p"])) / 2, \
               (np.asarray(r["live"]["s"]) + np.asarray(r["flip"]["s"])) / 2
    return sig(r[how]["j"]), sig(r[how]["p"]), np.asarray(r[how]["s"])
def decide(prob, thr=.45, thr2=.45, gap=.25, second=True, bias=None):
    q = prob + (bias if bias is not None else 0)
    o = np.argsort(q)[::-1]
    out = [C[o[0]]] if q[o[0]] >= thr else ["none"]
    if second and q[o[1]] >= thr2 and q[o[0]] - q[o[1]] < gap:
        out.append(C[o[1]])
    return norm(out)
LIVE = dict(j=dict(thr=.45, thr2=.45, gap=.25, second=True), p=dict(thr=.45, second=False))
def score(rs, how="live", cfg=LIVE):
    n = len(rs); jok = pok = sok = allok = 0
    for r in rs:
        jp, pp, sp = probs(r, how)
        J = decide(jp, **cfg["j"]) == norm(r["jacket"]); P = decide(pp, **cfg["p"]) == norm(r["pants"])
        Sok = S[int(np.argmax(sp))] == (r["sport"] or "unclear")
        jok += J; pok += P; sok += Sok; allok += J and P and Sok
    return dict(n=n, jacket=jok / n, pants=pok / n, sport=sok / n, all=allok / n)
fmt = lambda d: f"jacket {d['jacket']*100:5.1f}%  pants {d['pants']*100:5.1f}%  sport {d['sport']*100:5.1f}%  photo needs NO fix {d['all']*100:5.1f}%"
if __name__ == "__main__":
    print(f"{len(rows)} photos scored")
    for how in ("live", "fast", "flipavg"):
        print(f"{how:8s}", fmt(score(rows, how)))
    # where the jacket/pants mistakes are
    for part, key in (("JACKET", "j"), ("PANTS", "p")):
        conf = Counter(); tot = Counter(); right = Counter()
        for r in rows:
            jp, pp, _ = probs(r, "live"); pr = decide(jp if key == "j" else pp, **LIVE[key])
            tr = norm(r["jacket" if key == "j" else "pants"])
            for c in tr:
                tot[c] += 1; right[c] += c in pr
            if pr != tr:
                conf[(" + ".join(sorted(tr)), " + ".join(sorted(pr)))] += 1
        print(f"\n{part}: per true color, how often v3 includes it")
        print("  " + "  ".join(f"{c} {right[c]*100//tot[c]}% (n={tot[c]})" for c, _ in tot.most_common()))
        print(f"{part}: most common mistakes (true -> v3 said)")
        for (t, p), k in conf.most_common(12):
            print(f"  {t:18s} -> {p:18s} x{k}")
    sp = Counter((r["sport"], S[int(np.argmax(probs(r, 'live')[2]))]) for r in rows)
    print("\nSPORT (true, v3 said):", dict(sp))
