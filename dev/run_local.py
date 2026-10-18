"""
Run the app locally against a fake, in-memory R2 (moto server). Never touches production.

    .venv/bin/python dev/run_local.py            # http://127.0.0.1:8777

Seeds a test photographer (PIN 1234), 250 made-up customers and 40 sample photos
so the public site, POS and studio have something to show. Admin password: "local".

WooCommerce, Resend and Square are replaced by in-memory fakes: frame prices can be
edited, follow-up "emails" are printed to the console, Square customer lookups are
answered locally, and nothing leaves this machine.
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
    "WC_CONSUMER_KEY":      "ck_fake_local",
    "WC_CONSUMER_SECRET":   "cs_fake_local",
    "RESEND_API_KEY":       "re_fake_local",
    "SQUARE_APP_ID":        "sq0idp-FAKE-LOCAL",
    "SQUARE_ACCESS_TOKEN":  "EAAA_fake_local",
    "SQUARE_LOCATION_ID":   "LOCAL_LOC",
})
for var in ("ANTHROPIC_API_KEY", "WC_WEBHOOK_SECRET", "POS_PASSCODE"):
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
FIRSTS = ["Ann", "Bob", "Cara", "Dan", "Eve", "Finn", "Gus", "Hana", "Ivy", "Jack"]
LASTS  = ["Smith", "Jones", "Smythe", "Adams", "Park", "Lee", "Moore", "Nolan", "Ortiz", "Price",
          "Quinn", "Reyes", "Stone", "Tate", "Urban", "Vance", "Wells", "Young", "Zane", "Baker",
          "Clark", "Diaz", "Evans", "Ford", "Gray"]
customers = []
for i in range(250):
    first, last = FIRSTS[i % len(FIRSTS)], LASTS[(i // len(FIRSTS)) % len(LASTS)]
    customers.append({"first": first, "last": last,
                      "email": "" if i % 40 == 7 else f"{first.lower()}.{last.lower()}{i}@example.com",
                      "phone": f"406-555-{i:04d}", "street": f"{i} Lone Peak Dr",
                      "city": "Big Sky", "state": "MT", "zip": "59716"})
put_json("pos_data/customers.json", customers)
# Sample photos: a real (solid-colour) JPEG original for each, so thumbnails can be made
from io import BytesIO
from PIL import Image
sample_photos = []
for i in range(40):
    key = f"images/2026-10-0{1 + i % 3}/mountain-biking/morning/IMG_{i:04d}.jpg"
    buf = BytesIO()
    Image.new("RGB", (4000, 3000), (20 + i * 5, 90, 160)).save(buf, "JPEG", quality=85)
    s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue(), ContentType="image/jpeg")
    sample_photos.append({"path": key, "date": key.split("/")[1], "location": "Mountain Biking",
                          "last_name": "", "group": "morning"})
put_json("images.json", sample_photos)

# Optional: DEV_SEED_DIR=/path loads extra files into the fake bucket (key = path
# relative to the folder), e.g. a copy of real pricing config to reproduce a bug.
seed_dir = os.environ.get("DEV_SEED_DIR")
if seed_dir:
    for root, _, files in os.walk(seed_dir):
        for name in files:
            full = os.path.join(root, name)
            key = os.path.relpath(full, seed_dir).replace(os.sep, "/")
            with open(full, "rb") as fh:
                s3.put_object(Bucket=BUCKET, Key=key, Body=fh.read(), ContentType="application/json")
            print(f"[seed] {key}")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

# ── Fake WooCommerce + Resend: the app's outbound HTTP never leaves this machine ──
FRAME_PRICES = {1057: "55.00", 1058: "55.00", 1059: "75.00", 1060: "75.00",
                1061: "95.00", 1062: "95.00", 1063: "150.00", 1064: "150.00"}


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeHTTP:
    """Stands in for `requests` inside main.py."""
    def get(self, url, **kw):
        if "squareup.com" in url and url.endswith("/locations"):
            return _FakeResp({"locations": [{"id": "LOCAL_LOC", "name": "Local Test Location"}]})
        if "squareup.com" in url and "/orders/" in url:
            # Fake paid orders: SQ_TX_OK... is a $1.04 cash payment, SQ_TX_CARD... a $1.04 card
            # payment with a 33c Square fee; others don't exist
            oid = url.rsplit("/", 1)[-1]
            print(f"[fake Square] order lookup {oid}")
            if oid.startswith("SQ_TX_OK"):
                return _FakeResp({"order": {"id": oid, "location_id": "LOCAL_LOC", "state": "COMPLETED",
                                            "tenders": [{"id": "PAY_" + oid, "type": "CASH",
                                                         "amount_money": {"amount": 104}}]}})
            if oid.startswith("SQ_TX_CARD"):
                return _FakeResp({"order": {"id": oid, "location_id": "LOCAL_LOC", "state": "COMPLETED",
                                            "tenders": [{"id": "PAY_" + oid, "type": "CARD",
                                                         "amount_money": {"amount": 104},
                                                         "processing_fee_money": {"amount": 33}}]}})
            return _FakeResp({"errors": [{"code": "NOT_FOUND"}]}, status=404)
        if "squareup.com" in url and "/payments/" in url:
            pid = url.rsplit("/", 1)[-1]
            return _FakeResp({"payment": {"id": pid, "receipt_number": pid[-4:].upper(),
                                          "receipt_url": "https://squareup.com/receipt/preview/" + pid}})
        if url.endswith("/products/1034/variations"):
            return _FakeResp([{"id": vid, "price": p, "regular_price": p, "stock_status": "instock"}
                              for vid, p in FRAME_PRICES.items()])
        return _FakeResp([])                       # orders, etc.: none locally

    def put(self, url, json=None, **kw):
        vid = int(url.rstrip("/").split("/")[-1])
        if vid in FRAME_PRICES and json and "regular_price" in json:
            FRAME_PRICES[vid] = json["regular_price"]
            print(f"[fake WooCommerce] variation {vid} price -> {json['regular_price']}")
        return _FakeResp({"id": vid})

    def post(self, url, json=None, **kw):
        if "squareup.com" in url:
            if url.endswith("/customers/search"):
                email = json['query']['filter']['email_address']['exact']
                print(f"[fake Square] customer search {email}")
                if "fail" in email:              # simulate a Square customer problem
                    return _FakeResp({"errors": [{"code": "INTERNAL_SERVER_ERROR"}]}, status=500)
                return _FakeResp({})
            if url.endswith("/customers"):
                print(f"[fake Square] create customer {json.get('email_address')}")
                return _FakeResp({"customer": {"id": "FAKE_SQ_CUSTOMER"}})
        if "resend.com" in url:
            print(f"[fake email] to={json.get('to')} subject={json.get('subject')!r}")
            return _FakeResp({"id": "fake-email"})
        if url.endswith("/wc/v3/orders"):
            print("[fake WooCommerce] create order:")
            for li in json.get("line_items", []):
                print(f"    {li['name']}  x{li.get('quantity', 1)}  ${li['total']}  {li.get('meta_data') or ''}")
            for f in json.get("fee_lines", []):
                print(f"    {f['name']}  ${f['total']}")
            if json.get("coupon_lines"):
                print(f"    coupon: {json['coupon_lines']}")
            return _FakeResp({"id": 9001, "order_key": "wc_order_local"}, status=201)
        if url.endswith("/notes"):
            print(f"[fake WooCommerce] customer note on {url.split('/orders/')[1].split('/')[0]} (download email)")
            return _FakeResp({"id": 1}, status=201)
        print(f"[fake HTTP] POST {url} (not sent)")
        return _FakeResp({})

    def delete(self, url, **kw):
        return _FakeResp({})


import main  # noqa: E402

main.http_requests = FakeHTTP()

import uvicorn  # noqa: E402

uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("PORT", 8777)))
