"""
Run the app locally against a fake, in-memory R2 (moto server). Never touches production.

    .venv/bin/python dev/run_local.py            # http://127.0.0.1:8777

Seeds a test photographer (PIN 1234), a few customers and one sample folder so
the public site, POS and studio have something to show. Admin password: "local".
"""
import json
import os
import sys

from moto.server import ThreadedMotoServer

MOTO_PORT = 5055
BUCKET = "local-dev-bucket"

os.environ.update({
    "R2_ENDPOINT_URL":      f"http://127.0.0.1:{MOTO_PORT}",
    "R2_ACCESS_KEY_ID":     "local",
    "R2_SECRET_ACCESS_KEY": "local",
    "R2_BUCKET_NAME":       BUCKET,
    "AWS_DEFAULT_REGION":   "us-east-1",
    "ADMIN_PASSWORD":       "local",
})
for var in ("WC_CONSUMER_KEY", "WC_CONSUMER_SECRET", "RESEND_API_KEY", "ANTHROPIC_API_KEY",
            "SQUARE_ACCESS_TOKEN", "WC_WEBHOOK_SECRET", "POS_PASSCODE"):
    os.environ.pop(var, None)
assert "crystal-images" not in BUCKET

server = ThreadedMotoServer(ip_address="127.0.0.1", port=MOTO_PORT, verbose=False)
server.start()

import boto3  # noqa: E402

s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"], region_name="us-east-1",
                  aws_access_key_id="local", aws_secret_access_key="local")
s3.create_bucket(Bucket=BUCKET)


def put_json(key, payload):
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(payload).encode(),
                  ContentType="application/json")


put_json("meta/photographers.json", [{"id": "dev1", "name": "Local Tester", "pin": "1234"}])
put_json("pos_data/customers.json", [
    {"first": "Ann",  "last": "Smith",  "email": "ann@example.com",  "phone": "555-0101"},
    {"first": "Bob",  "last": "Jones",  "email": "bob@example.com",  "phone": "555-0102"},
    {"first": "Cara", "last": "Smythe", "email": "cara@example.com", "phone": "555-0103"},
])
put_json("images.json", [
    {"path": "images/2026-10-01/mountain-biking/morning/IMG_0001.jpg", "date": "2026-10-01",
     "location": "Mountain Biking", "last_name": "", "group": "morning"},
])

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

import uvicorn  # noqa: E402

uvicorn.run("main:app", host="127.0.0.1", port=int(os.environ.get("PORT", 8777)))
