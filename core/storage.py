"""Storage helpers: JSON settings files kept in the R2 bucket."""
import json
import time


class JsonDoc:
    """One JSON file in storage (a settings file or list), optionally remembered for `ttl` seconds.

    load() returns what's stored, or a fresh `default()` when the file is missing, unreadable,
    or fails `valid`. save() writes it (and refreshes the memory when remembering).
    `cache` is a plain {"data": ..., "ts": ...} dict, so setting cache["data"] = None forces
    the next load to read storage again."""

    def __init__(self, client, bucket, key, default, ttl=None, indent=None, valid=None):
        self.client, self.bucket, self.key = client, bucket, key
        self.default, self.ttl, self.indent, self.valid = default, ttl, indent, valid
        self.cache = {"data": None, "ts": 0.0}

    def load(self):
        now = time.monotonic()
        if self.ttl is not None and self.cache["data"] is not None and now - self.cache["ts"] < self.ttl:
            return self.cache["data"]
        try:
            d = json.loads(self.client.get_object(Bucket=self.bucket, Key=self.key)["Body"].read())
            if self.valid and not self.valid(d):
                raise ValueError(f"{self.key} has the wrong shape")
        except Exception:
            d = self.default()
        if self.ttl is not None:
            self.cache["data"], self.cache["ts"] = d, now
        return d

    def save(self, d):
        self.client.put_object(Bucket=self.bucket, Key=self.key, ContentType="application/json",
                               Body=json.dumps(d, indent=self.indent).encode())
        if self.ttl is not None:
            self.cache["data"], self.cache["ts"] = d, time.monotonic()
