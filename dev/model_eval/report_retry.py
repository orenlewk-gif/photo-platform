"""Report card for the v4 retry: v3 vs v4a vs v4b, "does a photo need a fix?", with the site's rules.
The fair exam is the real capture days none of them trained on (Feb 5 + Mar 8)."""
import json, os
from collections import Counter, defaultdict
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
COLORS = ['red', 'orange', 'yellow', 'green', 'blue', 'purple', 'pink', 'white', 'black', 'gray', 'brown', 'tan',
          'teal', 'camo', 'none', 'unclear']
SPORTS = ['ski', 'snowboard', 'unclear']
SAME = {"navy": "blue", "teal": "blue", "maroon": "red", "violet": "purple"}
RULE = (0.50, 0.15)                    # the site's second-jacket-color rule
MODELS = ("v3", "v4a", "v4b")
norm = lambda cs: frozenset(SAME.get(c, c) for c in (cs or []) if c) or frozenset(["none"])
sig = lambda x: 1 / (1 + np.exp(-np.asarray(x)))
real = {p: (t or "")[:10] for p, (t, _) in json.load(open(os.path.join(HERE, "capture_times.json"))).items()}
rows = [json.loads(l) for l in open(os.path.join(HERE, "results_retry.jsonl")) if l.strip()]
rows = [r for r in rows if "error" not in r]


def jacket(prob, rule=RULE):
    o = np.argsort(prob)[::-1]
    out = [COLORS[o[0]]]
    if rule and prob[o[1]] >= rule[0] and prob[o[0]] - prob[o[1]] < rule[1]:
        out.append(COLORS[o[1]])
    return norm(out)


for r in rows:
    r["tj"], r["tp"], r["ts"], r["day"] = norm(r["jacket"]), norm(r["pants"]), r["sport"] or "unclear", real.get(r["path"], "")
    for m in MODELS:
        r[m + "J"] = jacket(sig(r[m]["j"]))
        r[m + "P"] = norm([COLORS[int(np.argmax(r[m]["p"]))]])
        r[m + "S"] = SPORTS[int(np.argmax(r[m]["s"]))]


def score(rs, m):
    n = len(rs) or 1
    j = sum(r[m + "J"] == r["tj"] for r in rs); p = sum(r[m + "P"] == r["tp"] for r in rs)
    s = sum(r[m + "S"] == r["ts"] for r in rs)
    a = sum(r[m + "J"] == r["tj"] and r[m + "P"] == r["tp"] and r[m + "S"] == r["ts"] for r in rs)
    return dict(n=len(rs), all=a / n, jacket=j / n, pants=p / n, sport=s / n)


fmt = lambda d: (f"photo needs NO fix {d['all']*100:5.1f}%   jacket {d['jacket']*100:5.1f}%   pants {d['pants']*100:5.1f}%"
                 f"   sport {d['sport']*100:5.1f}%   (n={d['n']})")
fair = [r for r in rows if r["day"] in ("2026-02-05", "2026-03-08")]
print(f"{len(rows)} exam-folder photos graded; fair exam (Feb 5 + Mar 8): {len(fair)}\n")
for label, rs in (("FAIR EXAM (real days none of them trained on)", fair), ("whole exam folder 6/7", rows)):
    print(label)
    for m in MODELS:
        print(f"  {m:<4} {fmt(score(rs, m))}")
    print()
for part, key, tkey in (("JACKET", "J", "tj"), ("PANTS", "P", "tp")):
    tot, got = Counter(), {m: Counter() for m in MODELS}
    for r in fair:
        for c in r[tkey]:
            tot[c] += 1
            for m in MODELS:
                got[m][c] += c in r[m + key]
    print(f"{part} on the fair exam: how often each true color is included (v3 / v4a / v4b)")
    print("  " + "   ".join(f"{c} {'/'.join(str(got[m][c]*100//tot[c]) for m in MODELS)}% (n={tot[c]})" for c, _ in tot.most_common()))
    print()
print("SPORT on the fair exam (true -> said):")
for m in MODELS:
    print(f"  {m}:", dict(Counter((r['ts'], r[m + 'S']) for r in fair).most_common()))
print("\nBy real day (photo needs no fix, v3 / v4a / v4b):")
by = defaultdict(list)
for r in fair:
    by[(r["day"], r["folder"])].append(r)
for (d, f), rs in sorted(by.items()):
    print(f"  {d} {f:<24} " + " / ".join(f"{score(rs, m)['all']*100:5.1f}%" for m in MODELS) + f"   (n={len(rs)})")
