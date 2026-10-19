# Color model exam (read-only)

`exam_photos.json` lists the 2,000 labeled photos (6/6 + 6/7) used to grade v3 on 2026-10-18.
Keep these photos (ideally those whole days) OUT of v4 training so every version is graded on the same exam.

v3 results on this exam: photos needing no fix 60.7% with the old rules, 62.9% with the tuned rules now live
(jacket 75.6%, pants 84.2%, sport 95.0%).

- `run_eval.py` streams the photos from storage into memory (reads only, nothing saved or written) and records the
  model's raw scores to `results.jsonl`. Needs images.json downloaded next to it and Oren's OK for storage reads.
- `analyze.py` prints the report card and the most common mistakes; `tune.py` tries decision rules, choosing them on
  some folders and scoring them on the others.
