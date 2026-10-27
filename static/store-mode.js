/* Store mode: the gallery page on the shop's approved screens (/store). Customers heart their
   favorite photos; there are no prices, shop or watermarks. Favorites are the gallery's own
   selection (so "Save for later" is the website's saved cart, both ways), shown as hearts.

   Loaded just before the gallery's script, which calls the functions below when STORE is on. */
document.documentElement.classList.add('store-mode');

const STORE_HEART = '<svg class="store-heart" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
  + '<path d="M12 21s-7.5-4.6-9.6-9.3C.9 8.2 3 4.5 6.6 4.5c2.1 0 3.6 1.2 4.4 2.6.8-1.4 2.3-2.6 4.4-2.6 3.6 0 5.7 3.7 4.2 7.2C19.5 16.4 12 21 12 21z"/></svg>';

// ── What's favorited ──
// Every folder's favorites, with the open gallery's included. Single portrait photos are saved
// by their place in their pose, so their photos come from that pose's list.
function storeFavorites() {
  const data = currentCartData();
  const folders = [];
  Object.entries(data.folderCarts || {}).forEach(([key, fc]) => {
    const items = (fc.selectedPhotos || []).filter(p => p && p.path).map(p => ({kind: 'photo', path: p.path}));
    (fc.poses || []).forEach(pose => {
      const state = (fc.poseState || {})[pose.color || ''];
      const paths = pose.paths || [];
      if (state === 'pose') {
        items.push({kind: 'pose', color: pose.color || '', label: pose.label || 'Pose', paths});
      } else if (Array.isArray(state)) {
        state.forEach(i => { if (paths[i]) items.push({kind: 'single', color: pose.color || '', label: pose.label || 'Pose', path: paths[i]}); });
      }
    });
    const label = fc.location && fc.label && fc.label !== fc.location ? `${fc.location} · ${fc.label}` : (fc.label || fc.location || 'Photos');
    if (items.length) folders.push({key, label, date: key.split('||')[0], items});
  });
  return folders;
}

function storeCounts(folders = storeFavorites()) {
  let photos = 0, poses = 0;
  folders.forEach(f => f.items.forEach(it => { if (it.kind === 'pose') poses++; else photos++; }));
  return {photos, poses, total: photos + poses};
}

function storeCountText(c) {
  const parts = [];
  if (c.photos) parts.push(`${c.photos} photo${c.photos !== 1 ? 's' : ''}`);
  if (c.poses) parts.push(`${c.poses} whole pose${c.poses !== 1 ? 's' : ''}`);
  return parts.join(' + ');
}

// The gallery calls this whenever a heart changes
function storeFavoritesChanged() {
  saveCart();
  storeCodeFollow();
  storeRenderBar();
  storeUpdateLBButton();
  if (document.getElementById('store-favs')?.classList.contains('open')) storeRenderFavorites();
}

// ── Promo lines: what the counter offers (the numbers come from the server's settings) ──
function storePromoLines() {
  const off = Number((window.STORE_PROMO || {}).print_off) || 0;   // (built before the gallery script: no escHtml yet)
  const lines = [];
  if (off) lines.push(`<b>${off}% off prints</b> of the photos you get as digital files`);
  lines.push('Add the <b>Big Sky Resort logo</b> to your favorite photos!');
  return lines;
}

function storePromoHTML(cls) {
  return `<div class="${cls}">${storePromoLines().map(l => `<p>${l}</p>`).join('')}</div>`;
}

// ── Screen furniture: the favorites bar and the header buttons ──
function storeRenderBar() {
  const bar = document.getElementById('store-bar');
  if (!bar) return;
  const c = storeCounts();
  bar.querySelector('.store-bar-count').innerHTML = c.total
    ? `${STORE_HEART}<span>${storeCountText(c)} in your favorites</span>`
    : `${STORE_HEART}<span>Tap the heart on the photos you love</span>`;
  bar.querySelector('[data-act="favs"]').hidden = !c.total;
  bar.querySelector('[data-act="save"]').hidden = !c.total;
  bar.querySelector('[data-act="load"]').hidden = !!c.total;
  const send = bar.querySelector('[data-act="send"]');
  send.hidden = !c.total;
  send.innerHTML = _storeCode ? `Your code: <b>${escHtml(_storeCode)}</b>` : 'Send to sale';
  const n = document.getElementById('store-head-count');
  if (n) n.textContent = c.total ? `(${c.total})` : '';
}

function storeBuildUI() {
  // Header: the logo goes back to the first page; Favorites and Start over on the right
  const head = document.querySelector('.custom-header .header-inner');
  if (head) {
    const logo = head.querySelector('.logo');
    if (logo) {
      logo.setAttribute('role', 'button'); logo.tabIndex = 0;
      logo.setAttribute('aria-label', 'Back to all days');
      logo.addEventListener('click', () => navigate({view: 'days'}));
      logo.addEventListener('keydown', e => { if (e.key === 'Enter') navigate({view: 'days'}); });
    }
    const tools = document.createElement('div');
    tools.className = 'store-head-tools';
    // Eagle Mount families use their own page for now (the store's own Eagle Mount area comes later)
    tools.innerHTML = `<a class="store-head-btn" href="/eaglemount">Eagle Mount photos</a>
      <button type="button" class="store-head-btn" data-act="favs">${STORE_HEART} Favorites <span id="store-head-count"></span></button>
      <button type="button" class="store-head-btn" data-act="reset">Start over</button>`;
    // The promo lines sit in the header, between the logo and the buttons (it stays at the top)
    const promo = document.createElement('div');
    promo.innerHTML = storePromoHTML('store-promo');
    head.appendChild(promo.firstElementChild);
    head.appendChild(tools);
  }

  const bar = document.createElement('div');
  bar.id = 'store-bar';
  bar.className = 'store-bar';
  bar.setAttribute('role', 'region');
  bar.setAttribute('aria-label', 'Your favorites');
  bar.innerHTML = `<div class="store-bar-count" aria-live="polite"></div>
    <div class="store-bar-actions">
      <button type="button" class="btn-ghost btn-secondary" data-act="load">Load my saved photos</button>
      <button type="button" class="btn-ghost btn-secondary" data-act="save">Save for later</button>
      <button type="button" class="btn-ghost btn-secondary" data-act="favs">See my favorites</button>
      <button type="button" class="btn" data-act="send">Send to sale</button>
    </div>`;
  document.body.appendChild(bar);

  const favs = document.createElement('div');
  favs.id = 'store-favs';
  favs.className = 'store-overlay';
  favs.setAttribute('role', 'dialog');
  favs.setAttribute('aria-modal', 'true');
  favs.setAttribute('aria-labelledby', 'store-favs-title');
  favs.innerHTML = `<div class="store-modal">
      <div class="store-modal-head">
        <h3 id="store-favs-title">Your favorites</h3>
        <button type="button" class="store-close" data-act="close" aria-label="Close">Close</button>
      </div>
      <div id="store-favs-body"></div>
      ${storePromoHTML('store-promo-note')}
      <div class="store-modal-foot">
        <button type="button" class="store-clear" data-act="clear">Clear all favorites</button>
        <span class="store-foot-actions">
          <button type="button" class="btn-ghost btn-secondary" data-act="save">Save for later</button>
          <button type="button" class="btn" data-act="send">Send to sale</button>
        </span>
      </div>
    </div>`;
  document.body.appendChild(favs);

  const save = document.createElement('div');
  save.id = 'store-save';
  save.className = 'store-overlay';
  save.setAttribute('role', 'dialog');
  save.setAttribute('aria-modal', 'true');
  save.setAttribute('aria-labelledby', 'store-save-title');
  save.innerHTML = `<div class="store-modal store-modal-narrow">
      <div class="store-modal-head">
        <h3 id="store-save-title">Save for later</h3>
        <button type="button" class="store-close" data-act="close" aria-label="Close">Close</button>
      </div>
      <p class="store-note">Save your favorites with your email and pick them up anytime on bigskyphotos.com,
        or here in the store. Saved them on the website already? Load them below.</p>
      <div class="save-box" data-savebox></div>
    </div>`;
  document.body.appendChild(save);

  // Send to sale: the code to tell staff at the counter, then a thank-you when the sale is done
  const send = document.createElement('div');
  send.id = 'store-send';
  send.className = 'store-overlay';
  send.setAttribute('role', 'dialog');
  send.setAttribute('aria-modal', 'true');
  send.setAttribute('aria-labelledby', 'store-send-title');
  send.innerHTML = `<div class="store-modal store-modal-narrow store-send-box">
      <div class="store-modal-head">
        <h3 id="store-send-title">Store code</h3>
        <button type="button" class="store-close" data-act="close" aria-label="Close">Close</button>
      </div>
      <div id="store-send-body"></div>
    </div>`;
  document.body.appendChild(send);

  // A favorite, big: opens over the favorites list, flips through them all
  const view = document.createElement('div');
  view.id = 'store-view';
  view.className = 'store-overlay store-view';
  view.setAttribute('role', 'dialog');
  view.setAttribute('aria-modal', 'true');
  view.setAttribute('aria-label', 'Favorite photo');
  view.innerHTML = `<div class="store-view-top">
      <span id="store-view-where"></span>
      <button type="button" class="store-close" data-act="close" aria-label="Close">Close</button>
    </div>
    <div class="store-view-stage">
      <button type="button" class="store-view-nav" data-act="view-prev" aria-label="Previous photo">&#8249;</button>
      <img id="store-view-img" alt="">
      <button type="button" class="store-view-nav" data-act="view-next" aria-label="Next photo">&#8250;</button>
    </div>
    <div class="store-view-foot">
      <span id="store-view-count"></span>
      <button type="button" class="store-view-remove" data-act="view-unfav"></button>
    </div>`;
  document.body.appendChild(view);
  let touchX = null;
  view.addEventListener('touchstart', e => { touchX = e.touches[0].clientX; }, {passive: true});
  view.addEventListener('touchend', e => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(dx) > 50) storeViewStep(dx < 0 ? 1 : -1);
  });

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (b && (b.closest('#store-bar') || b.closest('.store-head-tools') || b.closest('.store-overlay'))) {
      const act = b.dataset.act;
      if (act === 'favs') storeOpen('store-favs');
      else if (act === 'save' || act === 'load') storeOpen('store-save');
      else if (act === 'reset') storeStartOver();
      else if (act === 'close') storeClose(b.closest('.store-overlay'));
      else if (act === 'unfav') storeUnfavorite(b.dataset);
      else if (act === 'clear') storeClearFavorites();
      else if (act === 'send') storeSendToSale();
      else if (act === 'view') storeView(+b.dataset.i);
      else if (act === 'view-prev') storeViewStep(-1);
      else if (act === 'view-next') storeViewStep(1);
      else if (act === 'view-unfav') storeViewUnfavorite();
      return;
    }
    if (e.target.classList?.contains('store-overlay') || e.target.classList?.contains('store-view-stage')) {
      storeClose(e.target.closest('.store-overlay'));                              // tap outside the box
    }
  });
  document.addEventListener('keydown', e => {
    const viewing = document.getElementById('store-view')?.classList.contains('open');
    if (viewing && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { storeViewStep(e.key === 'ArrowLeft' ? -1 : 1); return; }
    if (e.key !== 'Escape') return;
    // The big photo closes first, back to the favorites list
    if (viewing) storeClose(document.getElementById('store-view'));
    else document.querySelectorAll('.store-overlay.open').forEach(storeClose);
  });
}

function storeOpen(id) {
  document.querySelectorAll('.store-overlay.open').forEach(o => o.classList.remove('open'));
  const o = document.getElementById(id);
  if (id === 'store-favs') storeRenderFavorites();
  if (id === 'store-save') {
    renderSaveBoxes();
    o.querySelectorAll('details.save-details').forEach(d => { d.open = true; });
  }
  o.classList.add('open');
  document.body.style.overflow = 'hidden';
  o.querySelector('.store-close')?.focus();
}

function storeClose(o) {
  if (!o) return;
  o.classList.remove('open');
  if (!document.querySelector('.store-overlay.open')) document.body.style.overflow = '';
}

// ── The favorites list ──
// Each folder's photos, then each portrait pose on its own row: a whole pose shows all its
// photos under one heart, single photos each have their own. Any photo opens big.
let _storeViewList = [];   // every photo in the list, in order: {path, where, unfav: {...}, pose}

function storeRenderFavorites() {
  const body = document.getElementById('store-favs-body');
  const folders = storeFavorites();
  _storeViewList = [];
  document.querySelector('#store-favs [data-act="clear"]').hidden = !folders.length;
  if (!folders.length) {
    body.innerHTML = '<p class="store-note">No favorites yet. Tap the heart on any photo to add it here.</p>';
    return;
  }
  const unfavAttrs = u => Object.entries(u).map(([k, v]) => `data-${k}="${escA(v)}"`).join(' ');
  // A photo tile; whole-pose photos have no heart of their own (the pose's heart is on its row)
  const tile = (path, where, unfav, pose) => {
    const i = _storeViewList.push({path, where, unfav, pose}) - 1;
    return `<div class="store-fav-thumb">
        <button type="button" class="store-fav-open" data-act="view" data-i="${i}" aria-label="See this photo bigger">
          <img src="${escA(photoSrc(path, 'thumb'))}" alt="" loading="lazy">
          <span class="store-fav-name">${escHtml(path.split('/').pop())}</span>
        </button>
        ${pose ? '' : `<button type="button" class="store-unfav" data-act="unfav" ${unfavAttrs(unfav)}
          aria-label="Remove this photo from favorites">${STORE_HEART}</button>`}
      </div>`;
  };
  body.innerHTML = folders.map(f => {
    const base = {key: f.key};
    const photos = f.items.filter(it => it.kind === 'photo');
    const rows = [];
    if (photos.length) {
      rows.push(`<div class="store-fav-grid">${photos.map(it =>
        tile(it.path, f.label, {...base, kind: 'photo', path: it.path})).join('')}</div>`);
    }
    // Portrait poses, in the order they're picked: a whole pose, or its single photos
    const poseRows = new Map();
    f.items.forEach(it => {
      if (it.kind === 'photo') return;
      const k = `${it.kind === 'pose' ? 'p' : 's'}:${it.color}`;
      if (!poseRows.has(k)) poseRows.set(k, {kind: it.kind, color: it.color, label: it.label, paths: []});
      const row = poseRows.get(k);
      if (it.kind === 'pose') row.paths = it.paths; else row.paths.push(it.path);
    });
    poseRows.forEach(r => {
      const n = r.paths.length, nPhotos = `${n} photo${n !== 1 ? 's' : ''}`;
      const where = `${f.label} · ${r.label}`;
      let head, tiles;
      if (r.kind === 'pose') {
        const unfav = {...base, kind: 'pose', color: r.color};
        head = `<span><b>${escHtml(r.label)}</b> · whole pose, ${nPhotos}</span>
          <button type="button" class="store-fav-pose-remove" data-act="unfav" ${unfavAttrs(unfav)}
            aria-label="Remove ${escA(r.label)} from favorites">${STORE_HEART} Remove pose</button>`;
        tiles = r.paths.map(p => tile(p, `${where} (whole pose)`, unfav, r.label)).join('');
      } else {
        head = `<span><b>${escHtml(r.label)}</b> · ${nPhotos}</span>`;
        tiles = r.paths.map(p => tile(p, where, {...base, kind: 'single', color: r.color, path: p})).join('');
      }
      rows.push(`<div class="store-fav-pose-row"><div class="store-fav-pose-head">${head}</div>
        <div class="store-fav-grid">${tiles}</div></div>`);
    });
    return `<section class="store-fav-folder">
        <div class="store-fav-folder-title">${escHtml(f.label)}${f.date ? ` <span>${escHtml(formatDateLong(f.date))}</span>` : ''}</div>
        ${rows.join('')}
      </section>`;
  }).join('');
}

// Clear every favorite on this screen and stay where they are
function storeClearFavorites() {
  if (!confirm('Remove all favorites on this screen?')) return;
  clearCart();
  storeFavoritesChanged();
}

// ── A favorite, big ──
let _storeViewAt = 0;
function storeView(i) {
  if (!_storeViewList.length) { storeClose(document.getElementById('store-view')); return; }
  _storeViewAt = Math.max(0, Math.min(i, _storeViewList.length - 1));
  const it = _storeViewList[_storeViewAt];
  const v = document.getElementById('store-view');
  document.getElementById('store-view-img').src = photoSrc(it.path, 'medium');
  document.getElementById('store-view-where').textContent = `${it.where} · ${it.path.split('/').pop()}`;
  document.getElementById('store-view-count').textContent = `${_storeViewAt + 1} of ${_storeViewList.length}`;
  v.querySelector('.store-view-remove').innerHTML = `${STORE_HEART} ${it.pose ? `Remove whole ${escHtml(it.pose)}` : 'Remove from favorites'}`;
  v.querySelectorAll('.store-view-nav').forEach(b => { b.hidden = _storeViewList.length < 2; });
  // The next ones load while this one is looked at
  [1, -1].forEach(d => {
    const n = _storeViewList[(_storeViewAt + d + _storeViewList.length) % _storeViewList.length];
    if (n) new Image().src = photoSrc(n.path, 'medium');
  });
  if (!v.classList.contains('open')) {
    v.classList.add('open');
    document.body.style.overflow = 'hidden';
    v.querySelector('.store-close').focus();
  }
}

function storeViewStep(d) {
  const n = _storeViewList.length;
  if (n > 1) storeView((_storeViewAt + d + n) % n);
}

function storeViewUnfavorite() {
  const it = _storeViewList[_storeViewAt];
  if (!it) return;
  storeUnfavorite({...it.unfav});
  storeRenderFavorites();
  storeView(_storeViewAt);       // the next one slides into its place (closes when none are left)
}

// Un-heart from the favorites list. The open gallery changes through its own heart buttons (so
// its photos update too); other folders are changed where they're kept.
function storeUnfavorite({key, kind, color, path}) {
  if (key === currentFolderKey) {
    if (kind === 'pose') {
      const pi = (_poseData?.poses || []).findIndex(p => (p.color || '') === color);
      if (pi >= 0) togglePosePurchase(color, pi);
    } else {
      const i = allPhotos.findIndex(p => p.path === path);
      if (i < 0) return;
      if (kind === 'single') togglePoseSingle(i, color); else toggleSelect(i);
    }
    return;
  }
  const fc = folderCarts[key];
  if (!fc) return;
  if (kind === 'photo') {
    fc.selectedPaths = (fc.selectedPaths || []).filter(p => p !== path);
    fc.selectedPhotos = (fc.selectedPhotos || []).filter(p => p.path !== path);
  } else if (fc.poseState) {
    if (kind === 'pose') delete fc.poseState[color];
    else {
      const paths = (fc.poses || []).find(p => (p.color || '') === color)?.paths || [];
      const left = (fc.poseState[color] || []).filter(i => paths[i] !== path);
      if (left.length) fc.poseState[color] = left; else delete fc.poseState[color];
    }
  }
  storeFavoritesChanged();
}

// ── The lightbox heart ──
function storePoseColorOf(idx) {
  const p = allPhotos[idx];
  if (!p || !_poseData) return null;
  const fn = p.filename || p.path.split('/').pop();
  const pose = (_poseData.poses || []).find(x => (x.files || []).includes(fn));
  return pose ? (pose.color || '') : null;
}

function storeIsFavorite(idx) {
  if (_poseData) {
    const state = _poseState[storePoseColorOf(idx)];
    return state === 'pose' ? 'pose' : (state instanceof Set && state.has(idx));
  }
  return selectedPhotos.has(idx);
}

function storeUpdateLBButton() {
  const btn = document.getElementById('lb-select-btn');
  if (!btn) return;
  const fav = storeIsFavorite(cur);
  btn.innerHTML = `${STORE_HEART} ${fav === 'pose' ? 'Whole pose in favorites' : fav ? 'In your favorites' : 'Add to favorites'}`;
  btn.classList.toggle('selected-btn', !!fav);
  btn.setAttribute('aria-pressed', fav ? 'true' : 'false');
  const upsell = document.getElementById('lb-upsell');
  if (upsell) upsell.innerHTML = '';
}

function storeToggleFromLB() {
  if (_poseData) {
    const color = storePoseColorOf(cur);
    if (color === null) return;
    if (_poseState[color] === 'pose') { showToast('This whole pose is in your favorites'); return; }
    togglePoseSingle(cur, color);
  } else {
    toggleSelect(cur);
  }
  storeUpdateLBButton();
}

// ── Portrait poses: a heart for the whole pose instead of prices ──
function storePoseHeaderHTML(pose, pi) {
  const color = pose.color || '';
  const n = (pose.files || []).length;
  const fav = _poseState[color] === 'pose';
  const heart = color
    ? `<button type="button" class="pose-buy-btn store-pose-heart${fav ? ' selected' : ''}" aria-pressed="${fav}"
         onclick="togglePosePurchase('${escA(escQ(color))}',${pi})">${STORE_HEART} ${fav ? 'Whole pose in favorites' : 'Favorite the whole pose'}</button>`
    : '';
  return `<span class="pose-label">${escHtml(pose.label || `Pose ${pi + 1}`)}</span>
    <span class="pose-count">${n} photo${n !== 1 ? 's' : ''}</span>${heart}`;
}

function storePoseIntroHTML() {
  return '<div class="pose-intro">Tap the heart on any photo you love, or favorite a whole pose. Staff will help you with downloads and prints.</div>';
}

// ── Send to sale ──
// Staff, beside the customer, send the favorites and type the 4-letter code at the POS. While
// the code waits, new hearts go along with it. When the sale is done the screen thanks them and
// starts over for the next customer.
const STORE_SEND_KEY = 'ci_store_send_v1';
let _storeCode = (() => { try { return JSON.parse(localStorage.getItem(STORE_SEND_KEY) || 'null')?.code || ''; } catch (e) { return ''; } })();
let _storeCodeTimer = null, _storeCodeWatch = null, _storeThanked = false;

function storeSetCode(code) {
  _storeCode = code || '';
  try {
    if (_storeCode) localStorage.setItem(STORE_SEND_KEY, JSON.stringify({code: _storeCode}));
    else localStorage.removeItem(STORE_SEND_KEY);
  } catch (e) {}
  clearInterval(_storeCodeWatch);
  _storeCodeWatch = _storeCode ? setInterval(storeCheckCode, 5000) : null;
  storeRenderBar();
}

async function storePostFavorites() {
  const r = await fetch('/api/store/send', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cart: currentCartData(), code: _storeCode})});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || 'Could not send — try again');
  return d;
}

async function storeSendToSale() {
  if (!storeCounts().total) return;
  clearTimeout(_storeCodeTimer);
  const body = document.getElementById('store-send-body');
  body.innerHTML = '<p class="store-note">Sending your favorites…</p>';
  storeOpen('store-send');
  try {
    const d = await storePostFavorites();
    if (d.status === 'sold') { storeThankYou(); return; }
    storeSetCode(d.code);
    storeRenderCode();
  } catch (e) {
    body.innerHTML = `<p class="store-note store-err">${escHtml(e.message || 'Could not send — check the internet connection')}</p>`;
  }
}

function storeRenderCode() {
  // Staff send it from beside the customer: just the code to type at the POS
  const body = document.getElementById('store-send-body');
  if (!body || !_storeCode) return;
  body.innerHTML = `<div class="store-code" aria-label="Code ${escA(_storeCode.split('').join(' '))}">${escHtml(_storeCode)}</div>
    <div class="store-modal-foot"><button type="button" class="btn" data-act="close">Done</button></div>`;
}

// New hearts go along with the code (a moment after the last change)
function storeCodeFollow() {
  if (!_storeCode) return;
  clearTimeout(_storeCodeTimer);
  _storeCodeTimer = setTimeout(async () => {
    try {
      const d = await storePostFavorites();
      if (d.status === 'sold') storeThankYou();
      else if (d.status === 'cleared') storeSetCode('');
      else if (d.code !== _storeCode) storeSetCode(d.code);
    } catch (e) {}
  }, 1500);
}

async function storeCheckCode() {
  if (!_storeCode || _storeThanked) return;
  let d;
  try {
    const r = await fetch(`/api/store/send/${encodeURIComponent(_storeCode)}`);
    d = await r.json().catch(() => ({}));
    if (!r.ok) { storeSetCode(''); return; }       // gone (a new day, or this screen was removed)
  } catch (e) { return; }
  if (d.status === 'sold') { storeThankYou(); return; }
}

function storeThankYou() {
  _storeThanked = true;
  clearInterval(_storeCodeWatch);
  document.getElementById('store-send-title').textContent = 'Thank you!';
  document.getElementById('store-send-body').innerHTML = '<p class="store-thanks">Enjoy your photos.</p>';
  storeOpen('store-send');
  setTimeout(() => storeStartOver(false), 8000);
}

// ── Start over: clear this screen for the next customer ──
function storeStartOver(ask = true) {
  if (ask && storeCounts().total && !confirm('Clear all favorites on this screen and start over?')) return;
  try { localStorage.removeItem(CART_KEY); localStorage.removeItem(STORE_SEND_KEY); } catch (e) {}
  try { sessionStorage.clear(); } catch (e) {}
  location.replace('/store');
}

storeBuildUI();
document.addEventListener('DOMContentLoaded', () => {
  storeRenderBar();
  if (_storeCode) storeSetCode(_storeCode);           // keep watching a code sent before a reload
});
