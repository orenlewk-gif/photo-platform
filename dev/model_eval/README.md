# Color model exam (read-only)

EXAM DAY: 2026-06-07. `exam_photos.json` lists the 1,099 labeled photos from that day used to grade v3.
The whole day is kept OUT of training: `export_training_data.py` skips it (EXAM_DAYS), so every model
version is graded on the same never-seen photos. (6/6 was also used for the first v3 test and may go into training.)

v3 on this exam: photos needing no fix 64.6% with the old rules, 66.6% with the tuned rules now live
(jacket 78.9%, pants 84.0%, sport 98.0%). On both days (2,000 photos): 60.7% -> 62.9%.

- `run_eval.py` streams the photos from storage into memory (reads only, nothing saved or written) and records the
  model's raw scores to `results.jsonl`. Needs images.json downloaded next to it and Oren's OK for storage reads.
- `analyze.py` prints the report card and the most common mistakes; `tune.py` tries decision rules, choosing them on
  some folders and scoring them on the others.
