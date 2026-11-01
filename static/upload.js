/* The upload form: the studio's New Upload and the store's Upload Station share this one copy.

   Everything about picking and sending photos lives here: the date, group and location, dropped
   folders and files, color labels read from the files (Eagle Mount's color, portrait poses),
   the pose panel, Upload all families, retries, skipping photos already on the site, progress.

   The page provides: the upload form's markup (the ids used below), the choice-modal and toast
   markup, upload.css, showToast(msg, type) and esc(s). It can set window.uploadHooks:
     refresh()                    after photos are added (the studio redraws its folder list)
     done(date, location, folder) after an upload finishes (the studio opens that folder)
     locations(d)                 when the location list loads (the studio fills its Move menu)
   and calls initUploadForm() once the page has loaded. */

async function uploadRefresh() { try { await window.uploadHooks?.refresh?.(); } catch (e) {} }
function uploadDone(date, location, folder) {
  try { window.uploadHooks?.done?.(date, location, folder); } catch (e) {}
}

// Today's date where the computer is (not UTC, which is already tomorrow on a Montana evening)
function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function initUploadForm() {
  document.getElementById('upload-date').value = localToday();
  loadUploadLocations();
  loadPartners();
  setupDropZone();
}

// Photos uploading at once. Chrome allows 6 at a time per address and storage speaks HTTP/1.1,
// so every other photo goes to storage's second address (url_alt): 12 really in flight.
const CONCURRENCY = 12;

// Read EXIF capture times from local File objects (first 16KB only — fast, in-memory)
// Runs fn over every item with n always in flight: the next one starts as soon as any finishes
// (not in groups of n that each wait for their slowest photo)
async function runPool(items, n, fn) {
  let next = 0;
  const worker = async () => { while (next < items.length) await fn(items[next++]); };
  await Promise.all(Array.from({length: Math.min(n, items.length)}, worker));
}

// One photo to storage, tried up to 4 times with growing pauses (a brief network or storage
// hiccup usually clears in a few seconds). A non-2xx answer doesn't throw in fetch(), so it's checked.
const PUT_WAITS = [0, 1000, 4000, 8000];
async function putPhoto(it, signal) {
  for (const [n, wait] of PUT_WAITS.entries()) {
    if (it.ok || signal?.aborted) break;
    if (wait) await new Promise(r => setTimeout(r, wait));
    if (signal?.aborted) break;
    // Every other photo tries the second address first; any retry uses the usual one
    const url = n === 0 && it.altFirst && it.url_alt ? it.url_alt : it.url;
    try {
      const res = await fetch(url, {method:'PUT', body:it.file, headers:{'Content-Type':it.file.type||'image/jpeg'}, signal});
      if (res.ok) it.ok = true;
    } catch (e) {}
  }
  return !!it.ok;
}

// ── Photos that still didn't upload: a box listing them, and a button to send just those ──
// The files stay in this page's memory, so a retry needs nothing dropped again (dropping the
// folder again also works: photos already on the site are offered to be skipped).
let _retryJob = null;     // {label, run: async () => still-failed count}
function showRetry(names, run) {
  const box = document.getElementById('upload-retry');
  if (!names.length) { box.style.display = 'none'; _retryJob = null; return; }
  const n = names.length;
  document.getElementById('upload-retry-text').innerHTML =
    `<b>${n} photo${n === 1 ? '' : 's'} didn't upload:</b> ${names.slice(0, 12).map(esc).join(', ')}${n > 12 ? ` and ${n - 12} more` : ''}`;
  const btn = document.getElementById('upload-retry-btn');
  btn.textContent = `Retry ${n} photo${n === 1 ? '' : 's'}`; btn.disabled = false;
  box.style.display = 'block';
  _retryJob = run;
}
async function retryFailedUploads() {
  if (!_retryJob || _uploadActive) return;
  document.getElementById('upload-retry-btn').disabled = true;
  const run = _retryJob; _retryJob = null;
  await run();
}

async function readExifTimes(files) {
  const times = {};
  await Promise.all([...files].map(async f => {
    if (!/\.(jpg|jpeg)$/i.test(f.name)) return;
    try {
      const txt = new TextDecoder('latin1').decode(await f.slice(0, 16384).arrayBuffer());
      const m = txt.match(/\d{4}:\d{2}:\d{2} (\d{2}):(\d{2}):\d{2}/);
      if (m) {
        const mins = parseInt(m[1]) * 60 + parseInt(m[2]);
        if (mins >= 0 && mins < 1440) times[f.name] = mins;
      }
    } catch(e) {}
  }));
  return times;
}
// ── Color labels written while culling (GraphicConverter, Lightroom, Bridge...) ──
// Read from inside each photo file (XMP), the same way the server's read_xmp_color_label does.
// Winter action: a partner's color (Eagle Mount = orange) marks that partner's photos.
// Portraits: each color is a pose.
async function readColorLabel(f) {
  if (!/\.(jpg|jpeg)$/i.test(f.name)) return '';
  try {
    const txt = new TextDecoder('latin1').decode(await f.slice(0, 1048576).arrayBuffer());
    const at = txt.indexOf('http://ns.adobe.com/xap/1.0/');
    if (at < 0) return '';
    const xmp = txt.slice(at);
    const m = xmp.match(/xmp:Label\s*=\s*"([^"]*)"/) || xmp.match(/<xmp:Label>\s*([^<]*?)\s*<\/xmp:Label>/);
    return m ? m[1].trim() : '';
  } catch(e) { return ''; }
}
const _labelCache = new WeakMap();   // File -> its label (read once: checked before upload and again for the poses)
async function readColorLabels(files) {
  const out = new Map();
  const list = [...files];
  for (let i = 0; i < list.length; i += 16)
    await Promise.all(list.slice(i, i + 16).map(async f => {
      if (!_labelCache.has(f)) _labelCache.set(f, await readColorLabel(f));
      out.set(f, _labelCache.get(f));
    }));
  return out;
}
let PARTNERS = [];                 // [{id, name, label}] from /api/admin/partners
let _uploadLabels = new Map();     // File -> color label, for the winter action files picked
async function loadPartners() {
  try { PARTNERS = (await (await fetch('/api/admin/partners')).json()).partners || []; } catch(e) {}
}
function partnerFilesFor(files) {
  // {partner id: [file names]} for the files whose color label is that partner's color
  const out = {};
  for (const f of files) {
    const lab = (_uploadLabels.get(f) || '').toLowerCase();
    const p = lab && PARTNERS.find(p => (p.label || '').toLowerCase() === lab);
    if (p) (out[p.id] = out[p.id] || []).push(f.name);
  }
  return out;
}
async function scanUploadLabels() {
  // Winter action files just picked: read their labels and say how many go to each partner
  const files = [...uploadFolders.flatMap(f => f.files), ...looseFiles];
  const el = document.getElementById('partner-summary');
  if (!el) return;
  if (!files.length || _currentLocationIsPortrait() || !PARTNERS.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<span style="color:rgba(255,255,255,.4)">Reading color labels…</span>';
  _uploadLabels = await readColorLabels(files);
  const counts = partnerFilesFor(files);
  el.innerHTML = PARTNERS.map(p => {
    const n = (counts[p.id] || []).length;
    return `<div class="det-folder"><span class="fname">${esc(p.name)} (${esc(p.label)} label)</span>`
      + `<span class="fcount">${n} photo${n === 1 ? '' : 's'}${n ? ' · go to the ' + esc(p.name) + ' page, not the gallery' : ''}</span></div>`;
  }).join('');
}

const ZIP_LOCS      = new Set(["adventure zip","adventure zip line","adventure zipline","nature zip","nature zip line","nature zipline"]);
let PORTRAIT_LOCS = new Set(["lone peak portraits","explorer gondola","ramcharger portraits","adventure zip line","adventure zip"]);

// ── What is being uploaded ──
let uploadFolders = [];
let looseFiles = [];
let zipTiers = null;

// ── Pose assignment state ──
const POSE_COLORS = ['#e53e3e','#ecc94b','#48bb78','#4299e1','#ed8936','#9f7aea','#718096'];
let poseFiles = [];        // sorted File[] in pose mode
let poseObjURLs = [];      // blob URLs parallel to poseFiles
let poseSelection = new Set(); // indices currently selected
let _batchQueue  = [];     // [{name, files}] for multi-family portrait batch
let _batchCursor = -1;     // index into _batchQueue; -1 = not in batch mode
let poseGroups = [];       // [{color, label, indices: Set}]
let poseLastClick = -1;    // for shift-click range
let poseNoLabel = new Set();  // indices of this family's photos with no color label (when some others have one)
function _isPoseMode() { return poseFiles.length > 0; }

let _uploadCategories = [];

async function loadUploadLocations() {
  try {
    const d = await fetch('/api/upload/locations').then(r => r.json());
    if (d.portrait) PORTRAIT_LOCS = new Set(d.portrait.map(l => l.toLowerCase()));
    _uploadCategories = d.categories || [];
    const catSel = document.getElementById('upload-category');
    if (catSel) {
      catSel.innerHTML = '<option value="">Select group…</option>';
      for (const cat of _uploadCategories) {
        if (cat.active === false) continue;
        const opt = document.createElement('option');
        opt.value = cat.name;
        opt.textContent = cat.name;
        catSel.appendChild(opt);
      }
    }
    window.uploadHooks?.locations?.(d);
  } catch(e) {}
}

function onUploadCategoryChange() {
  const catName = document.getElementById('upload-category').value;
  const cat = _uploadCategories.find(c => c.name === catName);
  const wrap = document.getElementById('upload-location-wrap');
  const locSel = document.getElementById('upload-location');
  locSel.innerHTML = '<option value="">Select location…</option>';
  if (!cat) { wrap.style.display = 'none'; clearPoseState(); renderDetectedFolders(); updateUploadBtn(); return; }
  for (const loc of (cat.acts || [])) {
    const opt = document.createElement('option');
    opt.value = loc; opt.textContent = loc;
    locSel.appendChild(opt);
  }
  if (cat.acts?.length === 1) locSel.value = cat.acts[0];
  wrap.style.display = '';
  clearPoseState(); renderDetectedFolders(); updateUploadBtn();
}

// ── Zip tiers ──
async function loadZipTiers() {
  if (zipTiers !== null) return zipTiers;
  try {
    const res = await fetch('/api/zip-pricing');
    const d = await res.json();
    zipTiers = d.tiers || [];
  } catch(e) { zipTiers = []; }
  return zipTiers;
}

// ── The upload form ──
// Back to an empty form (nothing picked, no families or poses)
function resetUploadForm() {
  _batchQueue=[]; _batchCursor=-1;
  document.getElementById('batch-header').style.display='none';
  document.getElementById('drop-zone').style.display='';
  uploadFolders=[]; looseFiles=[]; _uploadLabels = new Map();
  clearPoseState();
  document.getElementById('detected-folders').style.display='';
  document.getElementById('folder-name-field').style.display='none';
  document.getElementById('upload-folder-name').value='';
  renderDetectedFolders(); updateUploadBtn();
  const ps = document.getElementById('partner-summary'); if (ps) ps.innerHTML = '';
}

function setupDropZone() {
  const dz = document.getElementById('drop-zone');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', handleDrop);
  dz.addEventListener('click', () => browseFolder());
}
function browseFolder() { document.getElementById('folder-input').click(); }
function browseFiles()  { document.getElementById('file-input').click(); }
function handleFolderInput(files) {
  const map = {};
  for (const f of files) {
    if (!/\.(jpg|jpeg|png)$/i.test(f.name)) continue;
    const top = f.webkitRelativePath.split('/')[0];
    if (!map[top]) map[top] = [];
    map[top].push(f);
  }
  for (const [name, fls] of Object.entries(map)) {
    if (!uploadFolders.find(f => f.name === name)) uploadFolders.push({name, files: fls});
  }
  if (_currentLocationIsPortrait()) {
    initBatchUpload(uploadFolders);
  } else {
    clearPoseState();
    document.getElementById('detected-folders').style.display = '';
    renderDetectedFolders(); updateUploadBtn(); scanUploadLabels();
  }
}
function handleFileInput(files) {
  uploadFolders = [];
  looseFiles = [...files].filter(f => /\.(jpg|jpeg|png)$/i.test(f.name));
  if (_currentLocationIsPortrait()) {
    initBatchUpload([{name:'', files:looseFiles}]);
  } else {
    clearPoseState();
    document.getElementById('detected-folders').style.display = '';
    renderDetectedFolders(); updateUploadBtn(); scanUploadLabels();
  }
}

async function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const items = [...e.dataTransfer.items];
  uploadFolders=[]; looseFiles=[];
  const entries = items.map(i=>i.webkitGetAsEntry?i.webkitGetAsEntry():null).filter(Boolean);
  const dirs = entries.filter(en=>en.isDirectory), files = entries.filter(en=>en.isFile);
  if (dirs.length) {
    for (const dir of dirs) { const df=await readDirFiles(dir); if(df.length) uploadFolders.push({name:dir.name,files:df}); }
    for (const fe of files) { const f=await new Promise(r=>fe.file(r)); if(/\.(jpg|jpeg|png)$/i.test(f.name)) looseFiles.push(f); }
  } else {
    for (const fe of files) { const f=await new Promise(r=>fe.file(r)); if(/\.(jpg|jpeg|png)$/i.test(f.name)) looseFiles.push(f); }
  }
  if (_currentLocationIsPortrait()) {
    const folders = uploadFolders.length ? uploadFolders : [{name:'', files:looseFiles}];
    initBatchUpload(folders);
  } else {
    clearPoseState();
    document.getElementById('detected-folders').style.display = '';
    renderDetectedFolders(); updateUploadBtn(); scanUploadLabels();
  }
}

async function readAllEntries(reader) {
  const all=[];
  while(true) { const b=await new Promise(r=>reader.readEntries(r,()=>r([]))); if(!b.length) break; all.push(...b); }
  return all;
}
async function readDirFiles(dirEntry) {
  const files=[];
  const entries=await readAllEntries(dirEntry.createReader());
  for (const entry of entries) {
    if (entry.isFile) { const f=await new Promise(r=>entry.file(r)); if(/\.(jpg|jpeg|png)$/i.test(f.name)) files.push(f); }
    else if (entry.isDirectory) files.push(...await readDirFiles(entry));
  }
  return files;
}

function _isZipUpload() {
  return ZIP_LOCS.has((document.getElementById('upload-location').value||'').trim().toLowerCase());
}
function getUploadGroupSize(i) {
  const sel = document.querySelector(`.det-gs[data-idx="${i}"]`);
  return sel ? sel.value : '';
}
// ── Pose assignment functions ──
function _currentLocationIsPortrait() {
  return PORTRAIT_LOCS.has((document.getElementById('upload-location').value||'').trim().toLowerCase());
}

function clearPoseState() {
  poseObjURLs.forEach(u => URL.revokeObjectURL(u));
  poseFiles = []; poseObjURLs = []; poseSelection = new Set(); poseGroups = []; poseLastClick = -1; poseNoLabel = new Set();
  const panel = document.getElementById('pose-assign-panel');
  panel.style.display = 'none';
  panel.innerHTML = '';
}

function esc2(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function initBatchUpload(folders) {
  _batchQueue  = folders.filter(f => f.files && f.files.length);
  _batchCursor = -1;
  if (!_batchQueue.length) return;
  loadBatchFamily(0);
}

function loadBatchFamily(idx) {
  _batchCursor = idx;
  const fam = _batchQueue[idx];
  document.getElementById('upload-folder-name').value = fam.name || '';
  document.getElementById('drop-zone').style.display        = 'none';
  document.getElementById('folder-name-field').style.display = 'none';
  document.getElementById('detected-folders').style.display  = 'none';
  initPoseAssign(fam.files);
  renderBatchHeader();
  updateUploadBtn();
}

function renderBatchHeader() {
  const el = document.getElementById('batch-header');
  if (_batchCursor < 0 || !_batchQueue.length) { el.style.display = 'none'; return; }
  const total = _batchQueue.length;
  const cur   = _batchCursor;
  const name  = _batchQueue[cur].name || 'Unnamed';
  let inner = '';
  if (total > 1) {
    const chips = _batchQueue.map((f,i) => {
      const cls = i < cur ? 'done' : i === cur ? 'current' : '';
      const label = esc2(f.name || 'Unnamed');
      return `<span class="batch-fam-chip ${cls}">${label}${i < cur ? ' ✓' : ''}</span>`;
    }).join('');
    inner += `<div class="batch-progress-label">Family ${cur+1} of ${total}</div>`;
    inner += `<div class="batch-family-list">${chips}</div>`;
  }
  inner += `<div class="batch-family-name">${esc2(name)}</div>`;
  el.innerHTML = inner;
  el.style.display = '';
}

function initPoseAssign(files) {
  clearPoseState();
  const imgs = [...files].filter(f => /\.(jpg|jpeg|png)$/i.test(f.name));
  if (!imgs.length) return;
  // Sort numerically by filename
  imgs.sort((a,b) => {
    const na = a.name.replace(/\D+/g,''), nb = b.name.replace(/\D+/g,'');
    if (na && nb) return parseInt(na) - parseInt(nb);
    return a.name.localeCompare(b.name);
  });
  poseFiles = imgs;
  poseObjURLs = imgs.map(f => URL.createObjectURL(f));
  document.getElementById('detected-folders').style.display = 'none';
  document.getElementById('folder-name-field').style.display = 'block';
  document.getElementById('pose-assign-panel').style.display = 'block';
  renderPosePanel(true);
  posesFromLabels(imgs);
}

// Each color label set while culling becomes a pose (in photo order). Photos without a
// label stay unassigned; the photographer checks and can still change everything by hand.
// Poses from color labels: one per color, in the order they first appear. Photos without a
// label are a pose of their own (as the server reads them) when the family has any labels.
function labelPoseGroups(files, labels) {
  const order = [], byColor = {};
  files.forEach((f, i) => {
    const lab = (labels.get(f) || '').toLowerCase();
    if (!byColor[lab]) { byColor[lab] = []; order.push(lab); }
    byColor[lab].push(i);
  });
  const labeled = order.some(lab => lab);
  return {labeled, order: labeled ? order : [], byColor};
}

async function posesFromLabels(imgs) {
  const labels = await readColorLabels(imgs);
  if (poseFiles !== imgs) return;                            // another family loaded
  const {order, byColor} = labelPoseGroups(imgs, labels);
  // A family with some labels: the photos without one are flagged (a missed label while culling)
  poseNoLabel = new Set(order.length ? (byColor[''] || []) : []);
  if (poseGroups.length || !order.length) { renderPosePanel(true); return; }   // poses already picked by hand
  poseGroups = order.map((lab, gi) => ({color: POSE_COLORS[gi % POSE_COLORS.length], label: `Pose ${gi + 1}`,
                                        indices: new Set(byColor[lab])}));
  renderPosePanel(true);
  updateUploadBtn();
  showToast(`${order.length} pose${order.length === 1 ? '' : 's'} filled in from color labels — check them, then upload`);
}

function renderPosePanel(fullRebuild) {
  const panel = document.getElementById('pose-assign-panel');
  const total = poseFiles.length;
  const assigned = new Set(poseGroups.flatMap(g => [...g.indices]));
  const unassigned = total - assigned.size;
  const sel = poseSelection.size;

  // Build legend + toolbar HTML (lightweight, no imgs)
  let legendHtml = '';
  poseGroups.forEach((g, gi) => {
    legendHtml += `<span class="pose-pill" onclick="activatePosePill(${gi})" title="Click to add selection to this pose">
      <span class="pp-dot" style="background:${esc(g.color)}"></span>
      <span class="pp-label">${esc(g.label)}</span>
      <span class="pp-count">(${g.indices.size})</span>
    </span>`;
  });
  if (unassigned > 0) legendHtml += `<span class="pose-pill" style="cursor:default;opacity:.6">
    <span class="pp-dot" style="background:rgba(255,255,255,.2)"></span>
    <span class="pp-label">Unassigned</span>
    <span class="pp-count">(${unassigned})</span>
  </span>`;

  // First render: build the full panel structure including img elements
  if (fullRebuild || !panel.querySelector('.pose-grid')) {
    let thumbsHtml = '';
    for (let i = 0; i < poseFiles.length; i++) {
      const f = poseFiles[i];
      const isSel = poseSelection.has(i);
      const grpIdx = poseGroups.findIndex(g => g.indices.has(i));
      const color = grpIdx >= 0 ? POSE_COLORS[grpIdx % POSE_COLORS.length] : '';
      thumbsHtml += `<div class="pose-thumb${isSel?' pt-selected':''}${grpIdx>=0?' pt-assigned':''}${poseNoLabel.has(i)?' pt-unlabeled':''}"
        data-idx="${i}" onclick="poseThumbClick(event,${i})">
        <img src="${esc(poseObjURLs[i])}" alt="${esc(f.name)}">
        <div class="pt-badge" style="${color?'background:'+color:''}"></div>
        <div class="pt-num">${i+1}</div>
        <div class="pt-nolabel">NO LABEL</div>
      </div>`;
    }
    panel.innerHTML = `
      <div class="pose-header-row">
        <span class="pose-header-title">Pose Assignment</span>
        <span class="pose-header-meta" id="pose-meta">${total} photos${sel?' · '+sel+' selected':''}</span>
      </div>
      <div class="pose-toolbar" id="pose-toolbar">
        <button class="pose-tbtn primary" id="pose-btn-new" onclick="assignToNewPose()" ${sel===0?'disabled':''}>+ New Pose</button>
        <button class="pose-tbtn" onclick="poseSelectAll()">Select All</button>
        <button class="pose-tbtn" onclick="poseSelectUnassigned()">Unassigned</button>
        <button class="pose-tbtn" onclick="poseClearSelection()">Clear</button>
        ${poseGroups.length ? `<button class="pose-tbtn" id="pose-btn-reset" onclick="clearPoseGroups()" style="margin-left:auto;color:rgba(255,80,80,.7)">Reset Poses</button>` : '<span id="pose-btn-reset" style="display:none"></span>'}
      </div>
      <div class="pose-legend" id="pose-legend">${legendHtml||'<span class="pose-empty-hint">Select photos and click + New Pose to begin</span>'}</div>
      <div id="pose-warn">${poseWarnHtml()}</div>
      <div class="pose-grid">${thumbsHtml}</div>
    `;
    return;
  }

  // Fast update: only toggle CSS classes on existing thumb elements, update text nodes
  const metaEl = panel.querySelector('#pose-meta');
  if (metaEl) metaEl.textContent = `${total} photos${sel?' · '+sel+' selected':''}`;

  const newBtn = panel.querySelector('#pose-btn-new');
  if (newBtn) newBtn.disabled = sel === 0;

  const resetBtn = panel.querySelector('#pose-btn-reset');
  if (resetBtn) {
    if (poseGroups.length) {
      resetBtn.style.display = '';
      resetBtn.style.marginLeft = 'auto';
    } else {
      resetBtn.style.display = 'none';
    }
  }

  const legendEl = panel.querySelector('#pose-legend');
  if (legendEl) legendEl.innerHTML = legendHtml || '<span class="pose-empty-hint">Select photos and click + New Pose to begin</span>';
  const warnEl = panel.querySelector('#pose-warn');
  if (warnEl) warnEl.innerHTML = poseWarnHtml();

  panel.querySelectorAll('.pose-thumb').forEach(el => {
    const i = parseInt(el.dataset.idx, 10);
    const isSel = poseSelection.has(i);
    const grpIdx = poseGroups.findIndex(g => g.indices.has(i));
    const color = grpIdx >= 0 ? POSE_COLORS[grpIdx % POSE_COLORS.length] : '';
    el.classList.toggle('pt-selected', isSel);
    el.classList.toggle('pt-assigned', grpIdx >= 0);
    const badge = el.querySelector('.pt-badge');
    if (badge) badge.style.background = color || '';
  });
}

function poseThumbClick(e, idx) {
  e.preventDefault();
  if (e.shiftKey && poseLastClick >= 0) {
    // Range select
    const lo = Math.min(poseLastClick, idx), hi = Math.max(poseLastClick, idx);
    if (!e.metaKey && !e.ctrlKey) poseSelection = new Set();
    for (let i = lo; i <= hi; i++) poseSelection.add(i);
  } else if (e.metaKey || e.ctrlKey) {
    // Toggle individual
    if (poseSelection.has(idx)) poseSelection.delete(idx);
    else poseSelection.add(idx);
  } else {
    // Single click — if already the only one selected, deselect
    if (poseSelection.size === 1 && poseSelection.has(idx)) {
      poseSelection.clear();
    } else {
      poseSelection = new Set([idx]);
    }
  }
  poseLastClick = idx;
  renderPosePanel();
}

// ── Photos with no color label ──
// A family culled with color labels (one per pose) where some photos have none: usually a
// missed label. They're flagged here and before upload; uploaded as they are, they're a pose
// of their own. Photos left out of every pose are flagged the same way.
function poseUnassigned() {
  const assigned = new Set(poseGroups.flatMap(g => [...g.indices]));
  return [...Array(poseFiles.length).keys()].filter(i => !assigned.has(i));
}
function poseNoLabelOwnPose() {
  // The poses made only of photos with no label (photos moved into a labeled pose are fine)
  return poseGroups.filter(g => g.indices.size && [...g.indices].every(i => poseNoLabel.has(i)));
}
function poseWarnHtml() {
  if (!poseNoLabel.size) return '';
  const own = poseNoLabelOwnPose();
  const n = poseNoLabel.size;
  return `<div class="pose-warn"><span><b>${n} photo${n === 1 ? ' has' : 's have'} no color label.</b> ${
    own.length ? `${n === 1 ? 'It is' : 'They are'} in ${own.map(g => esc(g.label)).join(', ')} on ${n === 1 ? 'its' : 'their'} own.` : `Check ${n === 1 ? 'it is' : 'they are'} in the right pose.`}
    Select ${n === 1 ? 'it' : 'them'} and click a pose to move ${n === 1 ? 'it' : 'them'}, or upload as is.</span>
    <button class="pose-tbtn" onclick="poseSelectNoLabel()">Select ${n === 1 ? 'it' : 'them'}</button></div>`;
}
function poseSelectNoLabel() { poseSelection = new Set(poseNoLabel); renderPosePanel(); }

let _choiceAnswer = null;
function askChoice(title, bodyHtml, yes, no) {
  // A two-button question; resolves true for yes
  return new Promise(resolve => {
    document.getElementById('choice-title').textContent = title;
    document.getElementById('choice-body').innerHTML = bodyHtml;
    const y = document.getElementById('choice-yes'), n = document.getElementById('choice-no');
    y.textContent = yes; n.textContent = no;
    const done = v => { document.getElementById('choice-modal').classList.remove('open'); y.onclick = n.onclick = null; resolve(v); };
    y.onclick = () => done(true); n.onclick = () => done(false);
    document.getElementById('choice-modal').classList.add('open');
    y.focus();
  });
}

// Before a family's upload: photos in no pose, or photos with no label in a pose of their own.
// Returns true to go ahead (photos in no pose then become a pose of their own).
async function checkPosesBeforeUpload() {
  const loose = poseUnassigned();
  if (loose.length) {
    const ok = await askChoice(`${loose.length} photo${loose.length === 1 ? ' is' : 's are'} not in a pose`,
      `<p class="choice-text">Put ${loose.length === 1 ? 'it' : 'them'} in a pose first, or upload ${loose.length === 1 ? 'it' : 'them'} as a pose of ${loose.length === 1 ? 'its' : 'their'} own.</p>`,
      'Upload anyway', loose.length === 1 ? 'Put it in a pose' : 'Put them in poses');
    if (!ok) { poseSelection = new Set(loose); renderPosePanel(); return false; }
    poseGroups.push({color: POSE_COLORS[poseGroups.length % POSE_COLORS.length], label: `Pose ${poseGroups.length + 1}`, indices: new Set(loose)});
    renderPosePanel(); updateUploadBtn();
  }
  const own = poseNoLabelOwnPose();
  if (own.length) {
    const n = own.reduce((a, g) => a + g.indices.size, 0);
    const ok = await askChoice(`${n} photo${n === 1 ? ' has' : 's have'} no color label`,
      `<p class="choice-text">${n === 1 ? 'It goes' : 'They go'} on the site as ${own.map(g => esc(g.label)).join(', ')}, a pose of ${n === 1 ? 'its' : 'their'} own.
       If ${n === 1 ? 'it belongs' : 'they belong'} with another pose, label ${n === 1 ? 'it' : 'them'} first.</p>`,
      'Upload anyway', n === 1 ? 'Label it' : 'Label them');
    if (!ok) { poseSelectNoLabel(); return false; }
  }
  return true;
}

// "Upload all families": the families with photos missing a label, listed before anything uploads
async function checkFamiliesBeforeUpload(from) {
  const flagged = [];
  for (let idx = from; idx < _batchQueue.length; idx++) {
    if (idx === _batchCursor) {                              // the family on screen, as set up there
      const own = poseNoLabelOwnPose().reduce((a, g) => a + g.indices.size, 0) + poseUnassigned().length;
      if (own) flagged.push({idx, name: _batchQueue[idx].name, n: own, total: poseFiles.length});
      continue;
    }
    const files = sortedFamilyPhotos(_batchQueue[idx].files);
    const g = labelPoseGroups(files, await readColorLabels(files));
    const n = g.labeled ? (g.byColor[''] || []).length : 0;    // no labels at all: Upload all stops there anyway
    if (n) flagged.push({idx, name: _batchQueue[idx].name, n, total: files.length});
  }
  if (!flagged.length) return true;
  const ok = await askChoice(`${flagged.length} ${flagged.length === 1 ? 'family has' : 'families have'} photos with no color label`,
    `<p class="choice-text">Each family's photos with no label go on the site as a pose of their own.</p>
     <ul class="choice-list">${flagged.map(f => `<li>${esc(f.name || 'Unnamed')}: ${f.n} of ${f.total} photos</li>`).join('')}</ul>`,
    'Upload anyway', `Review ${flagged[0].name || 'the first'}`);
  if (!ok) { if (flagged[0].idx !== _batchCursor) loadBatchFamily(flagged[0].idx); else poseSelectNoLabel(); }
  return ok;
}

function poseSelectAll() { poseSelection = new Set(poseFiles.map((_,i)=>i)); renderPosePanel(); }
function poseClearSelection() { poseSelection = new Set(); poseLastClick = -1; renderPosePanel(); }
function poseSelectUnassigned() {
  const assigned = new Set(poseGroups.flatMap(g=>[...g.indices]));
  poseSelection = new Set([...Array(poseFiles.length).keys()].filter(i=>!assigned.has(i)));
  renderPosePanel();
}

function assignToNewPose() {
  if (!poseSelection.size) return;
  const color = POSE_COLORS[poseGroups.length % POSE_COLORS.length];
  const label = `Pose ${poseGroups.length + 1}`;
  // Remove selected from any existing pose
  poseGroups.forEach(g => poseSelection.forEach(i => g.indices.delete(i)));
  poseGroups.push({ color, label, indices: new Set(poseSelection) });
  poseSelection = new Set();
  poseLastClick = -1;
  renderPosePanel();
  updateUploadBtn();
}

document.addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') return;
  if (!poseFiles.length || !poseSelection.size) return;
  e.preventDefault();
  assignToNewPose();
});

function activatePosePill(gi) {
  if (!poseSelection.size) return;
  // Remove selected from any existing pose
  poseGroups.forEach(g => poseSelection.forEach(i => g.indices.delete(i)));
  poseSelection.forEach(i => poseGroups[gi].indices.add(i));
  // Clean up empty poses
  poseGroups = poseGroups.filter(g => g.indices.size > 0);
  // Re-number labels
  poseGroups.forEach((g, i) => g.label = `Pose ${i+1}`);
  poseSelection = new Set();
  poseLastClick = -1;
  renderPosePanel();
  updateUploadBtn();
}

function clearPoseGroups() {
  poseGroups = [];
  poseSelection = new Set();
  poseLastClick = -1;
  renderPosePanel();
  updateUploadBtn();
}

function getPoseGroupsForUpload() {
  return poseGroups.map(g => ({
    color: g.color,
    label: g.label,
    files: [...g.indices].sort((a,b)=>a-b).map(i => poseFiles[i].name)
  }));
}

async function renderDetectedFolders() {
  const el = document.getElementById('detected-folders');
  const fnField = document.getElementById('folder-name-field');
  const isZip = _isZipUpload();
  let gsOpts = '';
  if (isZip) {
    const tiers = await loadZipTiers();
    gsOpts = '<option value="">— Size —</option>' +
      tiers.map(t=>`<option value="${t.people}">${t.people}p</option>`).join('');
  }
  let html='';
  if (uploadFolders.length) {
    for (let i=0;i<uploadFolders.length;i++) {
      const f=uploadFolders[i];
      html+=`<div class="det-folder"><span class="fname">${esc(f.name)}</span><span class="fcount">${f.files.length} photos</span>${isZip?`<select class="det-gs" data-idx="${i}" onchange="updateUploadBtn()">${gsOpts}</select>`:''}<button class="det-remove" onclick="removeDetected(${i})">✕</button></div>`;
    }
    if (looseFiles.length) html+=`<div class="det-folder"><span class="fname">+ ${looseFiles.length} loose files</span></div>`;
    fnField.style.display='none';
  } else if (looseFiles.length) {
    html=`<div class="det-folder"><span class="fname">${looseFiles.length} photo${looseFiles.length!==1?'s':''} selected</span></div>`;
    fnField.style.display = _currentLocationIsPortrait() ? 'block' : 'none';
  }
  el.innerHTML=html;
  if (!uploadFolders.length && !looseFiles.length) {
    const ps = document.getElementById('partner-summary'); if (ps) ps.innerHTML = '';
  }
}
function removeDetected(i) { uploadFolders.splice(i,1); renderDetectedFolders(); updateUploadBtn(); scanUploadLabels(); }
function updateUploadBtn() {
  const btn = document.getElementById('upload-action-btn');
  const allBtn = document.getElementById('upload-all-btn');
  if (allBtn) allBtn.style.display = _isPoseMode() && _batchCursor >= 0 && _batchQueue.length - _batchCursor > 1 ? '' : 'none';
  if (_isPoseMode()) {
    btn.disabled = poseGroups.length === 0;
    if (_batchCursor >= 0 && _batchQueue.length > 1) {
      const isLast = _batchCursor === _batchQueue.length - 1;
      btn.textContent = isLast ? 'Upload & Finish' : 'Upload & Next →';
    } else {
      btn.textContent = 'Upload';
    }
    return;
  }
  btn.textContent = 'Upload';
  const hasFiles = uploadFolders.length || looseFiles.length;
  let valid = !!hasFiles;
  if (hasFiles && _isZipUpload() && uploadFolders.length) {
    if (uploadFolders.some((_,i) => !getUploadGroupSize(i))) valid = false;
  }
  document.getElementById('upload-action-btn').disabled = !valid;
}

let _uploadActive = false;
let _uploadAbortCtrl = null;
function _beforeUnloadGuard(e) { e.preventDefault(); e.returnValue = ''; }
async function pollEmbedJobs(jobs, label) {
  // Color indexing for every folder pushed live, shown as one count
  const total = jobs.reduce((a, j) => a + j.total, 0);
  const state = {};
  while (true) {
    await new Promise(r => setTimeout(r, 2000));
    let ok = true;
    await Promise.all(jobs.filter(j => !(state[j.id] && state[j.id].finished)).map(async j => {
      try {
        const res = await fetch(`/api/admin/embed-status?job_id=${j.id}`);
        if (res.ok) state[j.id] = await res.json(); else ok = false;
      } catch (e) { ok = false; }
    }));
    const doneCount = jobs.reduce((a, j) => a + ((state[j.id] && state[j.id].done) || 0), 0);
    label.textContent = `Indexing ${doneCount} / ${total} photos for color search…`;
    if (!ok || jobs.every(j => state[j.id] && state[j.id].finished)) break;
  }
}

function cancelUpload() {
  if (_uploadAbortCtrl) _uploadAbortCtrl.abort();
}

// One portrait family: upload its photos, then add them to the site with their poses.
// progress(done, failed) after each photo. Returns {done, failed}, or null if nothing could start.
async function uploadPoseFamily(date, location, folder, files, poses, progress) {
  const pr=await fetch('/api/admin/upload/presign',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date,location,folder,files:files.map(f=>({name:f.name,type:f.type||'image/jpeg'}))})});
  if (!pr.ok) return null;
  const {urls}=await pr.json();
  const items=urls.map((u,i)=>({...u,file:files[i],altFirst:i%2===1}));
  let done=0, failed=0;
  progress(done, failed);
  const timesReq=readExifTimes(files);          // read while the photos upload
  await runPool(items, CONCURRENCY, async it => {
    await putPhoto(it);
    done++; if(!it.ok) failed++; progress(done, failed);
  });
  const keys=items.filter(it=>it.ok).map(it=>it.key);
  const times=await timesReq;
  let failedFiles=items.filter(it=>!it.ok).map(it=>it.file);
  if (keys.length) {
    const ir=await fetch('/api/admin/upload/index',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date,location,folder,keys,poses,times})}).catch(()=>null);
    if (!ir?.ok) { failed=done; failedFiles=items.map(it=>it.file); }   // uploaded but not added to the site
  }
  return {done, failed, failedFiles};
}

// Retry a portrait family's photos that didn't upload: the same family and poses (the poses are
// saved again whole, so the retried photos land in theirs)
function poseRetry(date, location, fams) {
  // fams: [{folder, files (the failed ones), poses}]
  const names = fams.flatMap(f => f.files.map(x => `${f.folder}/${x.name}`));
  showRetry(names, async () => {
    _uploadActive = true;
    const progWrap=document.getElementById('upload-progress'); progWrap.style.display='block';
    const fill=document.getElementById('prog-fill'), label=document.getElementById('prog-label');
    const still = [];
    for (const f of fams) {
      const r = await uploadPoseFamily(date, location, f.folder, f.files, f.poses, (done, failed) => {
        fill.style.width = Math.round(done / f.files.length * 100) + '%';
        label.textContent = `Retrying ${f.folder}: ${done} / ${f.files.length}…` + (failed ? ` (${failed} failed)` : '');
      });
      const left = r ? r.failedFiles : f.files;
      if (left.length) still.push({...f, files: left});
    }
    _uploadActive = false;
    await uploadRefresh();
    const n = still.reduce((a, f) => a + f.files.length, 0);
    label.textContent = n ? `Retry done — ${n} still didn't upload` : 'Retry done — all photos uploaded';
    showToast(n ? `${n} photos still didn't upload — check the connection and retry` : 'All photos uploaded and live', n ? 'error' : undefined);
    poseRetry(date, location, still);
  });
}

// A family's photos in pose order (file number), as the pose panel shows them
function sortedFamilyPhotos(files) {
  return [...files].filter(f => /\.(jpg|jpeg|png)$/i.test(f.name)).sort((a,b) => {
    const na = a.name.replace(/\D+/g,''), nb = b.name.replace(/\D+/g,'');
    if (na && nb) return parseInt(na) - parseInt(nb);
    return a.name.localeCompare(b.name);
  });
}

// "Upload all families": the rest of the batch without a click per family, each family's poses
// from its color labels (the family on screen as it's set up there). Stops at a family with no
// color labels at all (labels lost, or never labeled) and opens it to check.
async function uploadAllFamilies() {
  const date=document.getElementById('upload-date').value;
  const location=document.getElementById('upload-location').value.trim();
  if (!date||!location) { showToast('Enter date and location','error'); return; }
  if (_batchCursor < 0 || !_batchQueue.length) return;
  const btn=document.getElementById('upload-action-btn'), allBtn=document.getElementById('upload-all-btn');
  btn.disabled=true; allBtn.disabled=true;
  const go = await checkFamiliesBeforeUpload(_batchCursor);
  if (go) showRetry([]);
  if (go && poseUnassigned().length && poseGroups.length) {    // the family on screen: its loose photos become a pose
    const loose = poseUnassigned();
    poseGroups.push({color: POSE_COLORS[poseGroups.length % POSE_COLORS.length], label: `Pose ${poseGroups.length + 1}`, indices: new Set(loose)});
    renderPosePanel();
  }
  if (!go) { btn.disabled=false; allBtn.disabled=false; updateUploadBtn(); return; }
  _uploadActive=true; window.addEventListener('beforeunload',_beforeUnloadGuard);
  const progWrap=document.getElementById('upload-progress'); progWrap.style.display='block';
  const fill=document.getElementById('prog-fill'), label=document.getElementById('prog-label');
  const total=_batchQueue.length;
  let families=0, photos=0, failedPhotos=0, stoppedAt=-1, why='';
  const failedFams=[];
  for (let idx=_batchCursor; idx<total; idx++) {
    const fam=_batchQueue[idx];
    const folder=idx===_batchCursor ? document.getElementById('upload-folder-name').value.trim() : (fam.name||'').trim();
    let files, poses, labeled=true;
    if (idx===_batchCursor && poseGroups.length) {
      files=poseFiles; poses=getPoseGroupsForUpload();      // as set up on screen
    } else {
      files=sortedFamilyPhotos(fam.files);
      const groups=labelPoseGroups(files, await readColorLabels(files));
      labeled=groups.labeled;
      poses=groups.order.map((lab, gi) => ({color: POSE_COLORS[gi % POSE_COLORS.length], label: `Pose ${gi + 1}`,
                                            files: groups.byColor[lab].map(i => files[i].name)}));
    }
    if (!folder) why='has no family name';
    else if (!files.length) why='has no photos';
    else if (!labeled) why='has no color labels';
    if (why) { stoppedAt=idx; break; }
    _batchCursor=idx; renderBatchHeader();
    const r=await uploadPoseFamily(date, location, folder, files, poses, (done, failed) => {
      fill.style.width=Math.round(done/files.length*100)+'%';
      label.textContent=`Family ${idx+1} of ${total} — ${folder}: ${done} / ${files.length} uploaded…`+(failed?` (${failed} failed)`:'');
    });
    if (!r) { stoppedAt=idx; why='couldn\'t start uploading'; break; }
    families++; photos+=r.done-r.failed; failedPhotos+=r.failed;
    if (r.failedFiles.length) failedFams.push({folder, files: r.failedFiles, poses});
  }
  _uploadActive=false; window.removeEventListener('beforeunload',_beforeUnloadGuard);
  await uploadRefresh();
  poseRetry(date, location, failedFams);
  if (stoppedAt>=0) {
    const name=_batchQueue[stoppedAt].name||'This family';
    loadBatchFamily(stoppedAt);
    label.textContent=`${families} famil${families===1?'y':'ies'} uploaded — stopped at ${name}`;
    showToast(`${name} ${why} — check its poses, then Upload`,'error');
    btn.disabled=false; allBtn.disabled=false; updateUploadBtn();
    return;
  }
  label.textContent=`All ${families} famil${families===1?'y':'ies'} uploaded — ${photos} photos`+(failedPhotos?`, ${failedPhotos} failed`:'');
  showToast(failedPhotos ? `${failedPhotos} photos failed — Retry sends just those` : `All ${families} ${families===1?'family':'families'} uploaded — live on the site`, failedPhotos ? 'error' : undefined);
  allBtn.disabled=false;
  resetUploadForm();                                          // ready for the next folder
  setTimeout(()=>{ progWrap.style.display='none'; fill.style.width='0'; },2200);
}

async function startUpload() {
  const date=document.getElementById('upload-date').value;
  const location=document.getElementById('upload-location').value.trim();
  if (!date||!location) { showToast('Enter date and location','error'); return; }

  // ── Portrait pose-mode upload ──
  if (_isPoseMode()) {
    const folder=document.getElementById('upload-folder-name').value.trim();
    if (!folder) { showToast('Enter family last name','error'); return; }
    if (!poseGroups.length) { showToast('Assign at least one pose first','error'); return; }
    if (!await checkPosesBeforeUpload()) return;
    showRetry([]);
    const files=poseFiles;
    const totalFiles=files.length;
    if (!totalFiles) { showToast('No photos to upload','error'); return; }
    _uploadActive=true; window.addEventListener('beforeunload',_beforeUnloadGuard);
    const btn=document.getElementById('upload-action-btn'); btn.disabled=true;
    const progWrap=document.getElementById('upload-progress'); progWrap.style.display='block';
    const fill=document.getElementById('prog-fill'), label=document.getElementById('prog-label');
    const poses=getPoseGroupsForUpload();
    const r=await uploadPoseFamily(date, location, folder, files, poses, (done, failed) => {
      fill.style.width=Math.round(done/totalFiles*100)+'%';
      label.textContent=`${done} / ${totalFiles} uploaded…`+(failed?` (${failed} failed)`:'');
    });
    if (!r) { showToast('Presign failed','error'); _uploadActive=false; window.removeEventListener('beforeunload',_beforeUnloadGuard); btn.disabled=false; progWrap.style.display='none'; return; }
    const {done, failed}=r;
    const succeeded=done-failed;
    poseRetry(date, location, r.failedFiles.length ? [{folder, files: r.failedFiles, poses}] : []);
    if (failed) {
      label.textContent=`Done — ${succeeded} uploaded, ${failed} failed`;
      showToast(`${succeeded} uploaded, ${failed} failed — Retry sends just those`,'error');
    } else {
      label.textContent=`Done — ${succeeded} photos, ${poses.length} poses saved`;
      showToast(`${succeeded} photos uploaded — ${poses.length} poses, live on the site`);
    }
    _uploadActive=false; window.removeEventListener('beforeunload',_beforeUnloadGuard);
    await uploadRefresh();

    if (_batchCursor >= 0 && _batchQueue.length > 0) {
      const nextIdx = _batchCursor + 1;
      if (nextIdx < _batchQueue.length) {
        const remaining = _batchQueue.length - nextIdx;
        label.textContent = `${esc2(_batchQueue[_batchCursor].name)} done — ${remaining} famil${remaining===1?'y':'ies'} left`;
        setTimeout(()=>{ progWrap.style.display='none'; fill.style.width='0'; }, 1100);
        setTimeout(()=>loadBatchFamily(nextIdx), 1300);
        btn.disabled=false;
      } else {
        const total = _batchQueue.length;
        label.textContent = `All ${total} ${total===1?'family':'families'} uploaded`;
        showToast(`All ${total} ${total===1?'family':'families'} uploaded — live on the site`);
        resetUploadForm();                                    // ready for the next folder
        setTimeout(()=>{ progWrap.style.display='none'; fill.style.width='0'; },2200);
      }
    } else {
      const capturedFolder=folder;
      resetUploadForm();                                      // ready for the next folder
      uploadDone(date, location, capturedFolder);
      setTimeout(()=>{ progWrap.style.display='none'; fill.style.width='0'; },2200);
    }
    return;
  }

  // ── Standard (non-portrait) upload ──
  const _isPortraitLoc = PORTRAIT_LOCS.has(location.trim().toLowerCase());
  let batches = uploadFolders.length
    ? [...uploadFolders.map(f=>({folder: _isPortraitLoc ? f.name : '', files:f.files})), ...(looseFiles.length?[{folder:'',files:looseFiles}]:[]) ]
    : [{folder: _isPortraitLoc ? document.getElementById('upload-folder-name').value.trim() : '', files:looseFiles}];
  let totalFiles=batches.reduce((a,b)=>a+b.files.length,0);
  if (!totalFiles) { showToast('No photos to upload','error'); return; }
  // Batches going to the same site folder (winter action subfolders all go into the location)
  // are uploaded and finished together, so every file's capture time and Eagle Mount label counts
  const targets = new Map();   // folder -> {files, fis (batch numbers, for zip group sizes), left}
  batches.forEach(({folder, files}, fi) => {
    if (!files.length) return;
    const t = targets.get(folder) || {files: [], fis: [], left: 0};
    t.files.push(...files); t.fis.push(fi);
    targets.set(folder, t);
  });
  // Two files with the same name in one folder would overwrite each other
  for (const [folder, t] of targets) {
    const seen = new Set(), dup = new Set();
    for (const f of t.files) { const n = f.name.toLowerCase(); if (seen.has(n)) dup.add(f.name); seen.add(n); }
    if (dup.size) {
      showToast(`Two photos are named ${[...dup].slice(0, 3).join(', ')}${dup.size > 3 ? '…' : ''} in ${folder || location} — rename one and try again`, 'error');
      return;
    }
  }
  _uploadActive = true;
  _uploadAbortCtrl = new AbortController();
  window.addEventListener('beforeunload', _beforeUnloadGuard);
  const btn=document.getElementById('upload-action-btn'); btn.disabled=true;
  const cancelBtn=document.getElementById('upload-cancel-btn'); cancelBtn.style.display='';
  const progWrap=document.getElementById('upload-progress'); progWrap.style.display='block';
  const fill=document.getElementById('prog-fill'), label=document.getElementById('prog-label');
  let done=0, failed=0, labeled=0, sentBytes=0, startedAt=0;
  const labelJobs=[];     // thumbnail + color label jobs started for each batch added to the site
  // The upload speed so far (photos finished / time), to compare with the line's speed test
  const speedText=()=>{ const secs=(Date.now()-startedAt)/1000;
    return startedAt && secs > 3 && sentBytes ? ` · ${(sentBytes*8/secs/1e6).toFixed(0)} Mbps` : ''; };
  const setProgress=()=>{ const p=totalFiles ? Math.round(done/totalFiles*100) : 100; fill.style.width=p+'%';
    label.textContent=`${done} / ${totalFiles} uploaded`+(labelJobs.length?` · ${labeled} ${_isPortraitLoc ? 'ready' : 'labeled'}`:'')+speedText()+'…'+(failed?` (${failed} failed)`:''); };
  setProgress();
  // Save group sizes immediately so local viewer shows correct pricing right away
  if (_isZipUpload()) {
    for (const [fi, {folder}] of batches.entries()) {
      const gs = getUploadGroupSize(fi);
      if (gs) await fetch('/api/zip-folder/set-group-size',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({folder_key:`${date}|${location}|${folder}`,group_size:gs})}).catch(()=>{});
    }
  }
  // Presign every folder up front so all uploads can run as one shared pool
  const allItems=[]; // {url,key,filename,folder,file,ok}
  for (const [folder, t] of targets) {
    const pr=await fetch('/api/admin/upload/presign',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date,location,folder,files:t.files.map(f=>({name:f.name,type:f.type||'image/jpeg',size:f.size}))})});
    if (!pr.ok) { showToast(`Presign failed for ${folder||'files'}`,'error'); continue; }
    const {urls}=await pr.json();
    urls.forEach((u,i)=>allItems.push({...u,folder,file:t.files[i]}));
  }
  // A different photo with a name already in this folder (another camera, or another computer
  // uploading the same day) would replace it: nothing uploads until those are renamed
  const clash = allItems.filter(it => it.name_taken);
  if (clash.length) {
    await askChoice(`${clash.length} photo${clash.length === 1 ? ' has' : 's have'} the same name as a different photo already here`,
      `<p class="choice-text">Uploading ${clash.length === 1 ? 'it' : 'them'} would replace the photo${clash.length === 1 ? '' : 's'} on the site
        (likely another camera numbering the same way). Rename ${clash.length === 1 ? 'it' : 'them'} (for example add the photographer's initials) and upload again.</p>
       <ul class="choice-list">${clash.slice(0, 10).map(it => `<li>${esc(it.file.name)}</li>`).join('')}${clash.length > 10 ? `<li>and ${clash.length - 10} more</li>` : ''}</ul>`,
      'OK', 'Close');
    _uploadActive = false; _uploadAbortCtrl = null; window.removeEventListener('beforeunload', _beforeUnloadGuard);
    btn.disabled = false; cancelBtn.style.display = 'none'; progWrap.style.display = 'none';
    return;
  }
  // Dropped again after a failed upload: the photos already on the site can be skipped
  const already = allItems.filter(it => it.on_site);
  if (already.length) {
    const fresh = allItems.length - already.length;
    const skip = await askChoice(`${already.length} of these photos ${already.length === 1 ? 'is' : 'are'} already on the site`,
      `<p class="choice-text">${fresh ? `Upload just the ${fresh} that ${fresh === 1 ? "isn't" : "aren't"} there yet, or all of them again (the ones on the site are replaced).`
                                      : 'All of them are already there. Upload them all again (the ones on the site are replaced)?'}</p>`,
      fresh ? `Upload the ${fresh} new` : 'Skip them all', 'Upload all again');
    if (skip) for (let i = allItems.length - 1; i >= 0; i--) if (allItems[i].on_site) allItems.splice(i, 1);
  }
  for (const [folder, t] of targets) t.left = allItems.filter(it => it.folder === folder).length;
  allItems.forEach((it, i) => { it.altFirst = i % 2 === 1; });
  totalFiles = allItems.length;
  setProgress();
  showRetry([]);
  const isZip = _isZipUpload();
  const jobs = [];          // color indexing, one per folder pushed live
  const notIndexed = new Set();
  let totalLive = 0;
  // Winter action photos are added to the site in chunks while the rest are still uploading, so
  // their thumbnails and color labels are made meanwhile (portrait families all at once). Each
  // folder's chunks go in order; when its last photo is in, action folders are pushed live.
  const CHUNK = 20;
  for (const t of targets.values()) { t.ready = []; t.chain = Promise.resolve(); }
  function addChunk(folder, last) {
    const t = targets.get(folder);
    const items = t.ready.splice(0);
    t.chain = t.chain.then(async () => {
      if (items.length) {
        const files = items.map(it => it.file);
        const times = await readExifTimes(files);     // reads file headers already in memory — fast
        const ir = await fetch('/api/admin/upload/index',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({date,location,folder,keys:items.map(it=>it.key),times,
                               partner_files: _isPortraitLoc ? {} : partnerFilesFor(files)})}).catch(()=>null);
        if (!ir?.ok) notIndexed.add(folder || location);
        else { const d = await ir.json().catch(() => ({})); if (d.thumbs_job_id) labelJobs.push({id: d.thumbs_job_id, done: 0, finished: false}); }
      }
      if (!last) return;
      if (isZip) {
        for (const fi of t.fis) {
          const gs = getUploadGroupSize(fi);
          if (gs) await fetch('/api/zip-folder/set-group-size',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({folder_key:`${date}|${location}|${folder}`,group_size:gs})}).catch(()=>{});
        }
      }
      if (!_isPortraitLoc && !notIndexed.has(folder || location) && allItems.some(it => it.folder === folder && it.ok)) {
        const lr = await fetch('/api/admin/push-live',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({date,location,folder})}).catch(()=>null);
        if (lr?.ok) {
          const ld = await lr.json();
          totalLive += ld.pushed || 0;
          if (ld.job_id) jobs.push({id: ld.job_id, total: ld.classify_total || 0});
        }
      }
    });
  }
  // A non-2xx PUT response doesn't throw in fetch(), so it must be checked explicitly
  const _sig = _uploadAbortCtrl.signal;
  async function putOne(it) {
    if (_sig.aborted) { done++; failed++; setProgress(); return; }
    await putPhoto(it, _sig);
    done++; if (!it.ok) failed++; else sentBytes += it.file.size; setProgress();
    if (_sig.aborted) return;
    const t = targets.get(it.folder);
    if (it.ok) t.ready.push(it);
    if (--t.left === 0) addChunk(it.folder, true);
    else if (t.ready.length >= CHUNK && !_isPortraitLoc) addChunk(it.folder, false);   // a family's poses are read from all its photos at once
  }
  // A folder with nothing left to upload (all on the site already) still finishes: push-live
  // puts anything an earlier, interrupted upload left as drafts on the site
  for (const [folder, t] of targets) if (t.left === 0) addChunk(folder, true);
  // While photos upload, the labeling of the ones already in shows next to the upload count
  let uploading = true;
  const watchLabels = (async () => {
    while (uploading) {
      await new Promise(r => setTimeout(r, 3000));
      await Promise.all(labelJobs.filter(j => !j.finished).map(async j => {
        try { const r = await fetch(`/api/admin/embed-status?job_id=${j.id}`); if (r.ok) Object.assign(j, await r.json()); } catch (e) {}
      }));
      labeled = labelJobs.reduce((a, j) => a + (j.done || 0), 0);
      if (uploading) setProgress();
    }
  })();
  startedAt = Date.now();
  await runPool(allItems, CONCURRENCY, putOne);
  uploading = false;
  const upSecs = (Date.now() - startedAt) / 1000;
  if (sentBytes && upSecs > 3) console.log(`Upload: ${(sentBytes / 1e6).toFixed(0)} MB in ${upSecs.toFixed(0)} s = ${(sentBytes * 8 / upSecs / 1e6).toFixed(0)} Mbps`);
  const finishing = [...targets.values()].map(t => t.chain);
  if (_sig.aborted) {
    await Promise.all(finishing);                     // photos already added stay (as drafts)
    label.textContent = 'Upload cancelled';
    fill.style.width = '0';
    cancelBtn.style.display = 'none';
    _uploadActive = false; _uploadAbortCtrl = null;
    window.removeEventListener('beforeunload', _beforeUnloadGuard);
    btn.disabled = false;
    await uploadRefresh();
    setTimeout(()=>{ progWrap.style.display='none'; }, 1500);
    return;
  }
  cancelBtn.style.display = 'none';
  await Promise.all(finishing);
  if (jobs.length) await pollEmbedJobs(jobs, label);
  if (notIndexed.size) showToast(`Couldn't add ${[...notIndexed].join(', ')} to the site — Retry sends them again`, 'error');
  // Still not on the site: failed uploads, and photos uploaded to a folder that couldn't be added.
  // Retry runs this same upload with just those files (date and location as they were).
  const retryItems = allItems.filter(it => !it.ok || notIndexed.has(it.folder || location));
  const retryBatches = [...new Set(retryItems.map(it => it.folder))].map(folder =>
    ({name: folder, files: retryItems.filter(it => it.folder === folder).map(it => it.file)}));
  const retryNames = retryItems.map(it => (it.folder ? it.folder + '/' : '') + it.file.name);
  const retryRun = async () => {
    if (document.getElementById('upload-location').value !== location) {
      showToast(`Set the location back to ${location} to retry`, 'error'); showRetry(retryNames, retryRun); return;
    }
    document.getElementById('upload-date').value = date;
    if (_isPortraitLoc) { uploadFolders = retryBatches.filter(b => b.name); looseFiles = retryBatches.filter(b => !b.name).flatMap(b => b.files); }
    else { uploadFolders = []; looseFiles = retryItems.map(it => it.file); }
    await startUpload();
  };
  showRetry(retryNames, retryRun);
  const succeeded = done - failed;
  const isPortraitUpload = PORTRAIT_LOCS.has((document.getElementById('upload-location').value||'').trim().toLowerCase());
  const avg = sentBytes && upSecs > 3 ? ` · ${(sentBytes * 8 / upSecs / 1e6).toFixed(0)} Mbps average` : '';
  if (failed) {
    label.textContent = `Done — ${succeeded} uploaded, ${failed} failed${avg}`;
    showToast(`${succeeded} uploaded, ${failed} failed — Retry sends just those`,'error');
  } else if (isPortraitUpload) {
    label.textContent = `Done — ${succeeded} photos live`;
    showToast(`${succeeded} photos uploaded and live — check the poses in the folder`);
  } else {
    label.textContent = `Done — ${totalLive} photos live${avg}`;
    showToast(`${done} photos uploaded and live`);
  }
  _uploadActive = false; _uploadAbortCtrl = null;
  window.removeEventListener('beforeunload', _beforeUnloadGuard);
  cancelBtn.style.display = 'none';
  await uploadRefresh();
  const firstBatch=batches[0];
  if (firstBatch) uploadDone(date, location, firstBatch.folder||'');
  btn.disabled=false; uploadFolders=[]; looseFiles=[]; renderDetectedFolders(); updateUploadBtn();
  setTimeout(()=>{ progWrap.style.display='none'; fill.style.width='0'; },2200);
}
