"""Report card from grade_versions.py: v3 vs v4, scored the way Oren sees it ("does a photo need a fix?").

Color rules (second jacket color cutoffs) are chosen on the progress-check day (6/8) and then
applied unchanged to the exam day (6/7), for both versions, so neither gets rules tuned on the exam."""
import itertools, json, os
from collections import Counter, defaultdict
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
COLORS = ['red', 'orange', 'yellow', 'green', 'blue', 'purple', 'pink', 'white', 'black', 'gray', 'brown', 'tan',
          'teal', 'camo', 'none', 'unclear']
SPORTS = ['ski', 'snowboard', 'unclear']
SAME = {"navy": "blue", "teal": "blue", "maroon": "red", "violet": "purple"}
LIVE = (0.50, 0.15)                       # the site's rules today: second jacket color at >= 0.50, within 0.15


def norm(cs):
    return frozenset(SAME.get(c, c) for c in (cs or []) if c) or frozenset(["none"])


sig = lambda x: 1 / (1 + np.exp(-np.asarray(x)))
rows = [json.loads(l) for l in open(os.path.join(HERE, "results_versions.jsonl")) if l.strip()]
rows = [r for r in rows if "error" not in r]
exam_list = set(json.load(open(os.path.join(HERE, "exam_photos.json"))))
for r in rows:
    r["tj"], r["tp"], r["ts"] = norm(r["jacket"]), norm(r["pants"]), (r["sport"] or "unclear")
    for m in ("v3", "v4"):
        r[m + "jp"], r[m + "pp"], r[m + "s"] = sig(r[m]["j"]), sig(r[m]["p"]), SPORTS[int(np.argmax(r[m]["s"]))]


def jacket(prob, rule):
    o = np.argsort(prob)[::-1]
    out = [COLORS[o[0]]]
    if rule and prob[o[1]] >= rule[0] and prob[o[0]] - prob[o[1]] < rule[1]:
        out.append(COLORS[o[1]])
    return norm(out)


def score(rs, m, rule):
    n = len(rs) or 1; J = P = S = A = 0
    for r in rs:
        j = jacket(r[m + "jp"], rule) == r["tj"]
        p = norm([COLORS[int(np.argmax(r[m + "pp"]))]]) == r["tp"]
        s = r[m + "s"] == r["ts"]
        J += j; P += p; S += s; A += j and p and s
    return dict(n=n, all=A / n, jacket=J / n, pants=P / n, sport=S / n)


fmt = lambda d: (f"photo needs NO fix {d['all']*100:5.1f}%   jacket {d['jacket']*100:5.1f}%   "
                 f"pants {d['pants']*100:5.1f}%   sport {d['sport']*100:5.1f}%   (n={d['n']})")
tune = [r for r in rows if r["set"] == "tune"]
exam = [r for r in rows if r["set"] == "exam"]
exam_old = [r for r in exam if r["path"] in exam_list]
# Folder dates are placeholders: by real capture day, only Feb 5 and Mar 8 are days neither
# version trained on (v3 trained on Mar 2 via the 6/8 folder; both saw some of Feb 1 and Mar 10)
real = {p: (t or "")[:10] for p, (t, _) in json.load(open(os.path.join(HERE, "capture_times.json"))).items()}
CLEAN_DAYS = {"2026-02-05", "2026-03-08"}
exam_clean = [r for r in exam if real.get(r["path"]) in CLEAN_DAYS]

# Second-color rules chosen on 6/8 only
grid = [None] + list(itertools.product(np.round(np.arange(0.30, 0.95, 0.05), 2), np.round(np.arange(0.05, 0.45, 0.05), 2)))
best = {m: max(grid, key=lambda g: score(tune, m, g)["all"]) for m in ("v3", "v4")}

print(f"{len(rows)} photos graded ({len(tune)} on 6/8, {len(exam)} on 6/7; {len(exam_old)} of them the original exam list)\n")
for label, rs in (("FAIR EXAM: real days neither version trained on (Feb 5 + Mar 8)", exam_clean),
                  ("EXAM folder 6/7, all photos (includes days both saw a little of)", exam),
                  ("EXAM 6/7, the original 1,099 (v3's 66.6% list)", exam_old),
                  ("progress-check folder 6/8 (v3 trained on it; v4 didn't)", tune)):
    print(label)
    print(f"  v3, site rules today      {fmt(score(rs, 'v3', LIVE))}")
    print(f"  v3, rules tuned on 6/8    {fmt(score(rs, 'v3', best['v3']))}   rule={best['v3']}")
    print(f"  v4, site rules today      {fmt(score(rs, 'v4', LIVE))}")
    print(f"  v4, rules tuned on 6/8    {fmt(score(rs, 'v4', best['v4']))}   rule={best['v4']}")
    print()

for part, key, tkey in (("JACKET", "jp", "tj"), ("PANTS", "pp", "tp")):
    print(f"{part} on the fair exam: how often each true color is included (v3 -> v4)")
    tot, got = Counter(), {m: Counter() for m in ("v3", "v4")}
    for r in exam_clean:
        for m in ("v3", "v4"):
            pred = jacket(r[m + key], best[m]) if key == "jp" else norm([COLORS[int(np.argmax(r[m + key]))]])
            for c in r[tkey]:
                got[m][c] += c in pred
        for c in r[tkey]:
            tot[c] += 1
    print("  " + "   ".join(f"{c} {got['v3'][c]*100//tot[c]}->{got['v4'][c]*100//tot[c]}% (n={tot[c]})"
                            for c, _ in tot.most_common()))
    print()

print("SPORT on the fair exam (true -> said):")
for m in ("v3", "v4"):
    print(f"  {m}:", dict(Counter((r['ts'], r[m + 's']) for r in exam_clean).most_common()))
print("\nBy folder on the exam (photo needs no fix, v3 -> v4, each with its 6/8 rules):")
by = defaultdict(list)
for r in exam:
    by[r["folder"]].append(r)
for f, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
    print(f"  {f:<28} {score(rs, 'v3', best['v3'])['all']*100:5.1f}% -> {score(rs, 'v4', best['v4'])['all']*100:5.1f}%  (n={len(rs)})")
