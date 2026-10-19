"""Saving the photo list: saves take turns and each writes the latest list."""
import json
import threading

import pytest


@pytest.fixture()
def photos(main_module):
    saved = main_module.data
    main_module.data = [{"path": f"images/2026-12-10/x/P{i}.jpg", "date": "2026-12-10", "location": "X"} for i in range(3)]
    yield main_module.data
    main_module.data = saved


def _stored(main_module):
    return json.loads(main_module.s3.get_object(Bucket=main_module.R2_BUCKET, Key="images.json")["Body"].read())


def test_slow_save_from_before_a_delete_cannot_bring_the_photo_back(main_module, photos, monkeypatch):
    real_put = main_module.s3.put_object
    first_started, release = threading.Event(), threading.Event()
    calls = []

    def slow_first(**kw):
        calls.append(1)
        if len(calls) == 1:              # e.g. a labeling job's save, stuck on a slow upload
            first_started.set(); release.wait(5)
        return real_put(**kw)
    monkeypatch.setattr(main_module.s3, "put_object", slow_first)

    t = threading.Thread(target=main_module._save_photo_list); t.start()
    first_started.wait(5)
    del photos[0]                        # a delete in the studio while that save is uploading...
    saver = threading.Thread(target=main_module._save_photo_list); saver.start()
    release.set(); t.join(5); saver.join(5)
    assert [p["path"] for p in _stored(main_module)] == ["images/2026-12-10/x/P1.jpg", "images/2026-12-10/x/P2.jpg"]


def test_save_writes_the_current_list_even_after_it_was_replaced(main_module, photos):
    main_module.data = [{"path": "images/2026-12-11/y/N.jpg", "date": "2026-12-11", "location": "Y"}]
    main_module._save_photo_list()
    assert [p["path"] for p in _stored(main_module)] == ["images/2026-12-11/y/N.jpg"]
