"""Try decision rules on v3's scores; pick rules on some folders and check them on the others (no cheating)."""
import itertools, numpy as np
from analyze import rows, probs, decide, norm, score, LIVE, fmt, S
how = "fast"
J_GRID = [dict(thr=t, thr2=t2, gap=g, second=True) for t in (0, .3, .45) for t2 in (.2, .3, .4, .45, .5, .6) for g in (.15, .25, .35, .5, 1)]
P_GRID = [dict(thr=t, second=False) for t in (0, .2, .3, .45)] + \
         [dict(thr=t, thr2=t2, gap=g, second=True) for t in (0, .3, .45) for t2 in (.3, .45, .6) for g in (.15, .25, .5)]
cache = [(probs(r, how), norm(r["jacket"]), norm(r["pants"]), r) for r in rows]
def acc(sub, part, cfg):
    i = 0 if part == "j" else 1; truth = 1 if part == "j" else 2
    return np.mean([decide(c[0][i], **cfg) == c[truth] for c in sub])
folders = sorted({r["folder"] for r in rows})
def best(sub, part, grid):
    return max(grid, key=lambda cfg: acc(sub, part, cfg))
# leave-one-folder-out: choose rules without that folder, score on it
tot = {"live_j": 0, "new_j": 0, "live_p": 0, "new_p": 0}; n = 0
for f in folders:
    train = [c for c in cache if c[3]["folder"] != f]; test = [c for c in cache if c[3]["folder"] == f]
    bj, bp = best(train, "j", J_GRID), best(train, "p", P_GRID)
    k = len(test); n += k
    tot["live_j"] += acc(test, "j", LIVE["j"]) * k; tot["new_j"] += acc(test, "j", bj) * k
    tot["live_p"] += acc(test, "p", LIVE["p"]) * k; tot["new_p"] += acc(test, "p", bp) * k
    print(f"{f:28s} n={k:4d}  jacket {acc(test,'j',LIVE['j'])*100:5.1f}% -> {acc(test,'j',bj)*100:5.1f}%   pants {acc(test,'p',LIVE['p'])*100:5.1f}% -> {acc(test,'p',bp)*100:5.1f}%   rules j={bj} p={bp}")
print(f"\nON FOLDERS NOT USED TO PICK THE RULES: jacket {tot['live_j']/n*100:.1f}% -> {tot['new_j']/n*100:.1f}%   pants {tot['live_p']/n*100:.1f}% -> {tot['new_p']/n*100:.1f}%")
bj, bp = best(cache, "j", J_GRID), best(cache, "p", P_GRID)
print("rules picked on everything:", bj, bp)
cfg = dict(j=bj, p=bp)
print("live rules :", fmt(score(rows, how, LIVE)))
print("tuned rules:", fmt(score(rows, how, cfg)))
