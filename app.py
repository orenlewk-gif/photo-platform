import streamlit as st
import json
import torch
import re
from transformers import CLIPProcessor, CLIPModel
from PIL import Image, ImageOps
from rapidfuzz import fuzz
from datetime import datetime
import os
import base64
from io import BytesIO

# ─────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────
st.set_page_config(
    page_title="Big Sky Photos",
    page_icon="🏔",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
    html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
    .stApp { background: #0d1117; }
    .block-container { padding: 2rem 3rem 4rem; max-width: 1400px; }
    #MainMenu, footer, header { visibility: hidden; }

    .bsp-header {
        display: flex;
        align-items: baseline;
        gap: 16px;
        margin-bottom: 2rem;
        padding-bottom: 1.5rem;
        border-bottom: 1px solid rgba(242,201,76,0.25);
    }
    .bsp-title {
        font-family: 'Playfair Display', serif;
        font-size: 2.4rem;
        font-weight: 700;
        color: #ffffff;
        margin: 0;
        letter-spacing: -0.5px;
    }
    .bsp-subtitle {
        font-size: 0.95rem;
        color: rgba(255,255,255,0.45);
        font-weight: 300;
        letter-spacing: 0.5px;
    }
    .bsp-accent { color: #F2C94C; }

    .search-panel {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        padding: 1.8rem 2rem;
        margin-bottom: 2rem;
    }
    .panel-label {
        font-size: 0.7rem;
        font-weight: 500;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        color: #F2C94C;
        margin-bottom: 1rem;
    }

    .stTextInput input {
        background: rgba(255,255,255,0.06) !important;
        border: 1px solid rgba(255,255,255,0.12) !important;
        border-radius: 10px !important;
        color: #ffffff !important;
        font-family: 'DM Sans', sans-serif !important;
        font-size: 1rem !important;
        padding: 0.75rem 1rem !important;
    }
    .stTextInput input::placeholder { color: rgba(255,255,255,0.3) !important; }
    .stTextInput input:focus {
        border-color: #F2C94C !important;
        box-shadow: 0 0 0 2px rgba(242,201,76,0.15) !important;
    }

    .stSelectbox > div > div {
        background: rgba(255,255,255,0.06) !important;
        border: 1px solid rgba(255,255,255,0.12) !important;
        border-radius: 10px !important;
        color: #ffffff !important;
    }

    label, .stSelectbox label, .stSlider label, .stTextInput label {
        color: rgba(255,255,255,0.6) !important;
        font-size: 0.82rem !important;
        font-weight: 400 !important;
        letter-spacing: 0.3px !important;
    }

    div[data-testid="stButton"] button {
        background: #F2C94C !important;
        color: #0d1117 !important;
        font-family: 'DM Sans', sans-serif !important;
        font-weight: 600 !important;
        font-size: 0.95rem !important;
        letter-spacing: 0.5px !important;
        border: none !important;
        border-radius: 10px !important;
        padding: 0.75rem 2rem !important;
        width: 100% !important;
        transition: all 0.2s ease !important;
    }
    div[data-testid="stButton"] button:hover {
        background: #ffd84d !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 6px 20px rgba(242,201,76,0.3) !important;
    }

    .results-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 1.2rem;
    }
    .results-count {
        background: rgba(242,201,76,0.15);
        border: 1px solid rgba(242,201,76,0.3);
        color: #F2C94C;
        font-size: 0.8rem;
        font-weight: 600;
        padding: 3px 12px;
        border-radius: 20px;
        letter-spacing: 0.5px;
    }
    hr { border-color: rgba(255,255,255,0.08) !important; margin: 1.5rem 0 !important; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def clean_location(raw):
    """Strip leading numbers/separators and normalise to readable title."""
    cleaned = re.sub(r'^[\d\-_\s]+', '', raw)
    return cleaned.replace('-', ' ').replace('_', ' ').strip().title()

def fix_orientation(img):
    """Auto-rotate using EXIF data — handles all camera orientations reliably."""
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

def image_to_base64(img, max_size=900):
    """Resize and encode PIL image to base64 JPEG."""
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=83)
    return base64.b64encode(buf.getvalue()).decode()


# ─────────────────────────────────────────
# LOAD MODEL & DATA
# ─────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def load_model():
    model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return model, processor

@st.cache_data(show_spinner=False)
def load_data():
    with open("images.json", "r") as f:
        return json.load(f)


# ─────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────
st.markdown("""
<div class="bsp-header">
    <img src="https://bigskyphotos.com/wp-content/uploads/2026/05/JRM_3584-copy-2.jpeg"
         style="height:52px;width:52px;object-fit:cover;border-radius:8px;flex-shrink:0;" />
    <div class="bsp-title">Crystal <span class="bsp-accent">Images</span></div>
    <div class="bsp-subtitle">AI-powered image search &nbsp;·&nbsp; Powered by CLIP</div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────
# LOAD
# ─────────────────────────────────────────
with st.spinner("Loading AI model…"):
    model, processor = load_model()

try:
    data = load_data()
except FileNotFoundError:
    st.error("❌  images.json not found — run index_images.py first.")
    st.stop()


# ─────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────
if "browse_items" not in st.session_state:
    st.session_state.browse_items = None
if "browse_label" not in st.session_state:
    st.session_state.browse_label = ""


# ─────────────────────────────────────────
# SEARCH PANEL
# ─────────────────────────────────────────
dates = sorted(set(item["date"] for item in data), reverse=True)

panel_left, panel_right = st.columns([3, 2])

# ── Left: Browse by date + location ──
with panel_left:
    st.markdown('<div class="panel-label">Browse by Location</div>', unsafe_allow_html=True)
    bc1, bc2 = st.columns(2)
    with bc1:
        date_choice = st.selectbox("Date", ["Select a date…"] + dates, key="date_sel")
    with bc2:
        if date_choice == "Select a date…":
            st.selectbox("Location", ["Select a date first"], disabled=True, key="loc_sel_off")
            location_display = None
        else:
            # Build display→raw map, normalising duplicates by cleaned name
            loc_map = {}
            for item in data:
                if item["date"] == date_choice:
                    display = clean_location(item["location"])
                    loc_map.setdefault(display, item["location"])
            display_names   = ["Select a location…"] + sorted(loc_map.keys())
            location_display = st.selectbox("Location", display_names, key="loc_sel")

    browse_clicked = st.button("📂  Browse All Photos")

# ── Right: Search by last name ──
with panel_right:
    st.markdown('<div class="panel-label">Find My Photos by Last Name</div>', unsafe_allow_html=True)
    last_name_input = st.text_input("Guest last name", placeholder="e.g. Smith",
                                    label_visibility="collapsed")
    name_search_clicked = st.button("🔍  Find My Photos")


# ─────────────────────────────────────────
# BROWSE ACTION — load all photos for date+location
# ─────────────────────────────────────────
if browse_clicked:
    if date_choice == "Select a date…":
        st.warning("Please select a date.")
        st.stop()
    if not location_display or location_display == "Select a location…":
        st.warning("Please select a location.")
        st.stop()

    raw_location = loc_map[location_display]
    pool = [item for item in data
            if item["date"] == date_choice
            and clean_location(item["location"]) == location_display]

    st.session_state.browse_items = pool
    st.session_state.browse_label = f"{location_display}  ·  {date_choice}"


# ─────────────────────────────────────────
# LAST NAME SEARCH ACTION
# ─────────────────────────────────────────
top_results = None

if name_search_clicked:
    if not last_name_input.strip():
        st.warning("Please enter a last name.")
        st.stop()

    ln = last_name_input.strip().lower()
    matched = [item for item in data
               if fuzz.partial_ratio(ln, item.get("last_name", "").strip().lower()) >= 80
               and item.get("last_name", "").strip()]
    top_results = [(1.0, item) for item in matched]
    st.session_state.browse_items = None  # clear browse state


# ─────────────────────────────────────────
# BROWSE VIEW — show pool + descriptive filter
# ─────────────────────────────────────────
if st.session_state.browse_items is not None and top_results is None:
    pool = st.session_state.browse_items
    st.markdown(f"""
    <div class="results-header" style="margin-top:1.5rem;">
        <span style="color:rgba(255,255,255,0.7);font-size:0.95rem;font-weight:500;">
            📍 {st.session_state.browse_label}
        </span>
        <span class="results-count">{len(pool)} photo{"s" if len(pool) != 1 else ""}</span>
    </div>
    """, unsafe_allow_html=True)

    # Descriptive filter within this location
    fc1, fc2 = st.columns([4, 1])
    with fc1:
        filter_query = st.text_input("Filter these photos by description",
                                     placeholder="e.g. red jacket, family portrait, jumping…",
                                     label_visibility="collapsed")
    with fc2:
        filter_clicked = st.button("Filter")

    if filter_clicked and filter_query.strip():
        with st.spinner("Filtering…"):
            inputs = processor(text=[filter_query], return_tensors="pt", padding=True)
            with torch.no_grad():
                text_emb = model.get_text_features(**inputs)[0]
            scored = []
            for item in pool:
                img_emb = torch.tensor(item["embedding"])
                sim     = torch.cosine_similarity(text_emb, img_emb, dim=0).item()
                scored.append((sim, item))
            scored.sort(reverse=True)
            top_results = scored
    else:
        # Show all photos unsorted
        top_results = [(1.0, item) for item in pool]

if top_results is None:
    st.stop()

# ─────────────────────────────────────────
# RENDER RESULTS
# ─────────────────────────────────────────

if name_search_clicked and not top_results:
    st.warning("No photos found for that last name.")
    st.stop()

# Build payloads
payloads = []
for score, item in top_results:
    path = item["path"]
    try:
        formatted_date = datetime.strptime(item["date"], "%m-%d-%y").strftime("%B %d, %Y")
    except Exception:
        formatted_date = item["date"]

    loc_display = clean_location(item["location"])
    p = {"b64": "", "date": formatted_date, "location": loc_display,
         "filename": os.path.basename(path), "score": f"{score:.3f}", "ok": False}

    if os.path.exists(path):
        try:
            img      = Image.open(path).convert("RGB")
            img      = fix_orientation(img)
            p["b64"] = image_to_base64(img)
            p["ok"]  = True
        except Exception:
            pass

    payloads.append(p)

found = sum(1 for p in payloads if p["ok"])

if not found:
    st.warning("No photos found.")
else:
    # Thumbnail cards
    thumbs = ""
    lb_items = []
    for i, p in enumerate(payloads):
        if not p["ok"]:
            continue
        idx = len(lb_items)
        lb_items.append(p)
        thumbs += f"""
        <div class="thumb-card" onclick="openLB({idx})">
            <img src="data:image/jpeg;base64,{p['b64']}" loading="lazy" alt="{p['filename']}" />
            <div class="thumb-meta">
                <span class="thumb-loc">📍 {p['location']}</span>
                <span class="thumb-date">{p['date']}</span>
            </div>
        </div>"""

    js_data = json.dumps([
        {"b64": p["b64"], "date": p["date"],
         "location": p["location"], "filename": p["filename"], "score": p["score"]}
        for p in lb_items
    ])

    rows   = (found + 2) // 3
    height = min(rows * 330 + 120, 12000)

    html = f"""
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:transparent; font-family:'DM Sans',sans-serif; }}

.gallery-grid {{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:14px;
    padding-bottom:24px;
}}
@media(max-width:680px){{ .gallery-grid{{ grid-template-columns:repeat(2,1fr); }} }}
@media(max-width:420px){{ .gallery-grid{{ grid-template-columns:1fr; }} }}

.thumb-card {{
    background:rgba(255,255,255,.04);
    border:1px solid rgba(255,255,255,.08);
    border-radius:12px;
    overflow:hidden;
    cursor:pointer;
    transition:transform .2s ease, border-color .2s, box-shadow .2s;
}}
.thumb-card:hover {{
    transform:translateY(-4px);
    border-color:rgba(242,201,76,.45);
    box-shadow:0 14px 36px rgba(0,0,0,.55);
}}
.thumb-card img {{
    width:100%;
    aspect-ratio:4/3;
    object-fit:cover;
    display:block;
}}
.thumb-meta {{
    padding:9px 12px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:6px;
    flex-wrap:wrap;
}}
.thumb-loc   {{ color:rgba(255,255,255,.75); font-size:.75rem; font-weight:500; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.thumb-date  {{ color:rgba(255,255,255,.4);  font-size:.7rem; }}
.thumb-score {{ background:#F2C94C; color:#0d1117; font-size:.68rem; font-weight:700; padding:2px 7px; border-radius:4px; }}

/* ── Lightbox ── */
.lb-overlay {{
    display:none;
    position:fixed;
    inset:0;
    background:rgba(0,0,0,.95);
    z-index:99999;
    align-items:center;
    justify-content:center;
    flex-direction:column;
}}
.lb-overlay.open {{ display:flex; }}

.lb-inner {{
    position:relative;
    display:flex;
    flex-direction:column;
    align-items:center;
    width:92vw;
    max-width:1080px;
}}
.lb-img-wrap {{
    position:relative;
    width:100%;
    display:flex;
    align-items:center;
    justify-content:center;
}}
#lb-photo {{
    max-width:100%;
    max-height:74vh;
    object-fit:contain;
    border-radius:8px;
    box-shadow:0 0 80px rgba(0,0,0,.9);
    display:block;
    transition:opacity .18s ease;
}}
.lb-arrow {{
    position:absolute;
    top:50%;
    transform:translateY(-50%);
    background:rgba(242,201,76,.92);
    color:#0d1117;
    border:none;
    border-radius:50%;
    width:50px; height:50px;
    font-size:22px; font-weight:bold;
    cursor:pointer;
    display:flex; align-items:center; justify-content:center;
    transition:background .2s, transform .2s;
    z-index:10;
    user-select:none;
}}
.lb-arrow:hover {{ background:#F2C94C; transform:translateY(-50%) scale(1.1); }}
.lb-arrow.left  {{ left:-64px; }}
.lb-arrow.right {{ right:-64px; }}
@media(max-width:820px){{
    .lb-arrow.left  {{ left:6px; }}
    .lb-arrow.right {{ right:6px; }}
}}
.lb-close {{
    position:fixed; top:16px; right:20px;
    background:rgba(255,255,255,.1);
    border:1px solid rgba(255,255,255,.2);
    color:#fff;
    border-radius:50%;
    width:42px; height:42px;
    font-size:18px; cursor:pointer;
    display:flex; align-items:center; justify-content:center;
    transition:background .2s;
    z-index:100001;
}}
.lb-close:hover {{ background:rgba(255,255,255,.22); }}

.lb-info {{
    margin-top:16px;
    text-align:center;
    color:rgba(255,255,255,.8);
    font-size:.9rem;
    line-height:1.7;
}}
.lb-filename {{ color:rgba(255,255,255,.35); font-size:.74rem; margin-top:4px; }}
.lb-counter  {{ margin-top:10px; color:rgba(255,255,255,.28); font-size:.74rem; letter-spacing:1px; }}
</style>

<div class="gallery-grid">{thumbs}</div>

<div class="lb-overlay" id="lb">
  <button class="lb-close" onclick="closeLB()">✕</button>
  <div class="lb-inner">
    <div class="lb-img-wrap">
      <button class="lb-arrow left"  onclick="stepLB(-1)">&#8592;</button>
      <img id="lb-photo" src="" alt="" />
      <button class="lb-arrow right" onclick="stepLB(1)">&#8594;</button>
    </div>
    <div class="lb-info"     id="lb-info"></div>
    <div class="lb-filename" id="lb-fn"></div>
    <div class="lb-counter"  id="lb-ct"></div>
  </div>
</div>

<script>
const IMGS = {js_data};
let cur = 0;

function openLB(i) {{
  cur = i; render();
  document.getElementById('lb').classList.add('open');
  const f = window.frameElement;
  if (f) {{
    f.style.position = 'fixed';
    f.style.top = '0';
    f.style.left = '0';
    f.style.width = '100vw';
    f.style.height = '100vh';
    f.style.zIndex = '99999';
    f.style.border = 'none';
  }}
}}
function closeLB() {{
  document.getElementById('lb').classList.remove('open');
  const f = window.frameElement;
  if (f) {{
    f.style.position = '';
    f.style.top = '';
    f.style.left = '';
    f.style.width = '';
    f.style.height = '';
    f.style.zIndex = '';
  }}
}}
function stepLB(dir) {{
  cur = (cur + dir + IMGS.length) % IMGS.length;
  render();
}}
function render() {{
  const d = IMGS[cur];
  const ph = document.getElementById('lb-photo');
  ph.style.opacity = '0';
  setTimeout(() => {{ ph.src = 'data:image/jpeg;base64,' + d.b64; ph.style.opacity = '1'; }}, 120);
  document.getElementById('lb-info').innerHTML =
    '📍 <strong>' + d.location + '</strong> &nbsp;·&nbsp; 📅 ' + d.date +
    ' &nbsp;·&nbsp; Score: <span style="color:#F2C94C;font-weight:600">' + d.score + '</span>';
  document.getElementById('lb-fn').textContent = d.filename;
  document.getElementById('lb-ct').textContent = (cur+1) + ' / ' + IMGS.length;
}}
document.addEventListener('keydown', e => {{
  if (!document.getElementById('lb').classList.contains('open')) return;
  if (e.key==='ArrowRight') stepLB(1);
  if (e.key==='ArrowLeft')  stepLB(-1);
  if (e.key==='Escape')     closeLB();
}});
document.getElementById('lb').addEventListener('click', function(e) {{
  if (e.target===this) closeLB();
}});
</script>
"""
    st.components.v1.html(html, height=height, scrolling=True)