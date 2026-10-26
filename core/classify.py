"""The color labeling model (ONNX, fine-tuned CLIP): sport, jacket and pants colors per photo."""
import json
import os
import threading
from io import BytesIO

import numpy as np
from PIL import Image

# Which trained model labels new photos (files <name>.onnx, .onnx.data, _meta.json, kept in storage
# under models/). Set COLOR_MODEL=model_v3 on the server to go back to v3. The name is also stamped on
# each photo the model labels, so it's always known which version labeled what.
MODEL_LABEL = os.getenv("COLOR_MODEL", "model_v4a").strip() or "model_v4a"

# Decision rules, tuned 2026-10-18 on 2,000 labeled photos v3 never trained on (6/6 + 6/7):
# photos needing no fix went from 60.7% to 62.9%, and every folder improved.
#  - always give the most likely color (when v3 used to say "none" it was almost always a real color)
#  - add a second jacket color only when it scores at least 0.50 and within 0.15 of the first
#  - pants get one color
SECOND_JACKET_MIN = 0.50
SECOND_JACKET_GAP = 0.15

CLIP_MEAN = np.array([0.48145466, 0.4578275,  0.40821073], dtype=np.float32).reshape(3, 1, 1)
CLIP_STD  = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32).reshape(3, 1, 1)


# Photos are decoded at reduced scale (still >= 900px): about half the time, same answers
# (checked on the same 2,000 photos)
MODEL_DECODE_PX = 900


def open_for_model(img_bytes: bytes, min_px: int = MODEL_DECODE_PX) -> Image.Image:
    """The photo decoded for the model (RGB, as stored: no rotation). min_px can be larger when
    the same decode also makes thumbnails."""
    img = Image.open(BytesIO(img_bytes))
    if img.format in ("JPEG", "MPO"):   # MPO = camera JPEG with a built-in preview
        img.draft("RGB", (max(min_px, MODEL_DECODE_PX),) * 2)
    return img.convert("RGB")


def preprocess_rgb(img: Image.Image) -> np.ndarray:
    pil = img.resize((224, 224), Image.LANCZOS)
    arr = np.array(pil, dtype=np.float32).transpose(2, 0, 1) / 255.0  # HWC -> CHW
    arr = (arr - CLIP_MEAN) / CLIP_STD
    return arr[np.newaxis]  # (1, 3, 224, 224)


def preprocess_image(img_bytes: bytes) -> np.ndarray:
    return preprocess_rgb(open_for_model(img_bytes))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def decide(j_logits, p_logits, s_logits, meta: dict) -> dict:
    """Turn the model's scores for one photo into labels (the decision rules above)."""
    colors, sports = meta["all_colors"], meta["sports"]
    j_prob, p_prob = _sigmoid(np.asarray(j_logits)), _sigmoid(np.asarray(p_logits))
    j_sorted = np.argsort(j_prob)[::-1]
    jacket = [colors[j_sorted[0]]]
    if (len(j_sorted) > 1 and j_prob[j_sorted[1]] >= SECOND_JACKET_MIN
            and j_prob[j_sorted[0]] - j_prob[j_sorted[1]] < SECOND_JACKET_GAP):
        jacket.append(colors[j_sorted[1]])
    pants = [colors[int(np.argmax(p_prob))]]
    sport = sports[int(np.argmax(s_logits))]
    remap = lambda cs: ['blue' if c == 'teal' else c for c in cs]
    return {"sport": sport, "jacket_colors": remap(jacket), "pants_colors": remap(pants)}


class ColorModel:
    """The model files, loaded on first use (one thread each, so labeling jobs can run several at once)."""

    def __init__(self, onnx_path: str, meta_path: str):
        self.onnx_path, self.meta_path = onnx_path, meta_path
        self.session = None
        self.meta = None
        self._lock = threading.Lock()

    def load(self) -> bool:
        with self._lock:
            if self.session is not None:
                return True
            if not (os.path.exists(self.onnx_path) and os.path.exists(self.meta_path)):
                return False
            try:
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 1
                opts.inter_op_num_threads = 1
                self.session = ort.InferenceSession(self.onnx_path, sess_options=opts, providers=["CPUExecutionProvider"])
                with open(self.meta_path) as f:
                    self.meta = json.load(f)
                print(f"ONNX model loaded (best_score={self.meta.get('best_score', '?'):.3f})")
                return True
            except Exception as e:
                print(f"ONNX load failed: {e}")
                return False

    def classify(self, img_bytes: bytes) -> dict | None:
        """Labels for one photo, or None if the model isn't available or the photo can't be read."""
        try:
            return self.classify_rgb(open_for_model(img_bytes)) if self.load() else None
        except Exception as e:
            print(f"ONNX classify failed: {e}")
            return None

    def classify_rgb(self, img: Image.Image) -> dict | None:
        """Labels for a photo already decoded by open_for_model."""
        if not self.load():
            return None
        try:
            j_logits, p_logits, s_logits = self.session.run(
                ["jacket_logits", "pants_logits", "sport_logits"],
                {"pixel_values": preprocess_rgb(img)},
            )
            return decide(j_logits[0], p_logits[0], s_logits[0], self.meta)
        except Exception as e:
            print(f"ONNX classify failed: {e}")
            return None
