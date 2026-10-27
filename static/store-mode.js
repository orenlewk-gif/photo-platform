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
        state.forEach(i => { if (paths[i]) items.push({kind: 'single', color: pose.color || '', path: paths[i]}); });
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
  storeRenderBar();
  storeUpdateLBButton();
  if (document.getElementById('store-favs')?.classList.contains('open')) storeRenderFavorites();
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
      <button type="button" class="btn" data-act="favs">See my favorites</button>
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
      <div class="store-modal-foot">
        <button type="button" class="btn-ghost btn-secondary" data-act="save">Save for later</button>
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

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (b && (b.closest('#store-bar') || b.closest('.store-head-tools') || b.closest('.store-overlay'))) {
      const act = b.dataset.act;
      if (act === 'favs') storeOpen('store-favs');
      else if (act === 'save' || act === 'load') storeOpen('store-save');
      else if (act === 'reset') storeStartOver();
      else if (act === 'close') storeClose(b.closest('.store-overlay'));
      else if (act === 'unfav') storeUnfavorite(b.dataset);
      return;
    }
    if (e.target.classList?.contains('store-overlay')) storeClose(e.target);   // tap outside the box
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.store-overlay.open').forEach(storeClose);
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
function storeRenderFavorites() {
  const body = document.getElementById('store-favs-body');
  const folders = storeFavorites();
  if (!folders.length) {
    body.innerHTML = '<p class="store-note">No favorites yet. Tap the heart on any photo to add it here.</p>';
    return;
  }
  const thumb = (path, data, label) => `<div class="store-fav-thumb">
      <img src="${escA(photoSrc(path, 'thumb'))}" alt="" loading="lazy">
      <button type="button" class="store-unfav" data-act="unfav" ${data} aria-label="${escA(label)}">${STORE_HEART}</button>
    </div>`;
  body.innerHTML = folders.map(f => {
    const cards = f.items.map(it => {
      const k = `data-key="${escA(f.key)}"`;
      if (it.kind === 'pose') {
        const shown = it.paths.slice(0, 4).map(p => `<img src="${escA(photoSrc(p, 'thumb'))}" alt="" loading="lazy">`).join('');
        return `<div class="store-fav-pose">
            <div class="store-fav-pose-imgs">${shown}</div>
            <div class="store-fav-pose-label">${escHtml(it.label)}: whole pose, ${it.paths.length} photo${it.paths.length !== 1 ? 's' : ''}</div>
            <button type="button" class="store-unfav" data-act="unfav" ${k} data-kind="pose" data-color="${escA(it.color)}"
              aria-label="Remove ${escA(it.label)} from favorites">${STORE_HEART}</button>
          </div>`;
      }
      return thumb(it.path, `${k} data-kind="${it.kind}" data-color="${escA(it.color || '')}" data-path="${escA(it.path)}"`,
                   'Remove this photo from favorites');
    }).join('');
    return `<section class="store-fav-folder">
        <div class="store-fav-folder-title">${escHtml(f.label)}${f.date ? ` <span>${escHtml(formatDateLong(f.date))}</span>` : ''}</div>
        <div class="store-fav-grid">${cards}</div>
      </section>`;
  }).join('');
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

// ── Start over: clear this screen for the next customer ──
function storeStartOver(ask = true) {
  if (ask && storeCounts().total && !confirm('Clear all favorites on this screen and start over?')) return;
  try { localStorage.removeItem(CART_KEY); } catch (e) {}
  try { sessionStorage.clear(); } catch (e) {}
  location.replace('/store');
}

storeBuildUI();
document.addEventListener('DOMContentLoaded', storeRenderBar);
