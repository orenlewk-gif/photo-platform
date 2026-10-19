"""core.storage.JsonDoc: one JSON settings file in storage, optionally remembered for a while."""
import json

import boto3

from core.storage import JsonDoc

S3 = boto3.client("s3", region_name="us-east-1")
B = "test-bucket"


def test_missing_or_bad_file_gives_a_fresh_default(main_module):
    doc = JsonDoc(S3, B, "meta/doc_missing.json", default=lambda: {"tiers": []}, valid=lambda d: isinstance(d, dict))
    a = doc.load(); a["tiers"].append(1)
    assert doc.load() == {"tiers": []}                       # a new default each time, not a shared one
    S3.put_object(Bucket=B, Key="meta/doc_missing.json", Body=b"[1, 2]")
    assert doc.load() == {"tiers": []}                       # wrong shape -> default
    S3.put_object(Bucket=B, Key="meta/doc_missing.json", Body=b"{not json")
    assert doc.load() == {"tiers": []}


def test_remembered_for_its_ttl_and_refreshed_by_save(main_module):
    S3.put_object(Bucket=B, Key="meta/doc_ttl.json", Body=b'{"v": 1}')
    doc = JsonDoc(S3, B, "meta/doc_ttl.json", default=dict, ttl=60, indent=2)
    assert doc.load() == {"v": 1}
    S3.put_object(Bucket=B, Key="meta/doc_ttl.json", Body=b'{"v": 2}')
    assert doc.load() == {"v": 1}                            # still remembered
    doc.cache["data"] = None
    assert doc.load() == {"v": 2}                            # clearing the memory re-reads
    doc.save({"v": 3})
    assert doc.load() == {"v": 3}
    assert S3.get_object(Bucket=B, Key="meta/doc_ttl.json")["Body"].read() == json.dumps({"v": 3}, indent=2).encode()


def test_without_ttl_every_load_reads_storage(main_module):
    doc = JsonDoc(S3, B, "meta/doc_nottl.json", default=list)
    doc.save(["a"])
    S3.put_object(Bucket=B, Key="meta/doc_nottl.json", Body=b'["b"]')
    assert doc.load() == ["b"]


def test_settings_files_keep_their_cache_handles(main_module):
    # tests and studio saves clear these by setting ["data"] = None
    assert main_module._pricing_cache is main_module._pricing_doc.cache
    assert main_module._folder_meta_cache is main_module._folder_meta_doc.cache
