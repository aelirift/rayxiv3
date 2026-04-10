/* RayXI Game Gallery */
'use strict';

const gameGrid    = document.getElementById('game-grid');
const emptyMsg    = document.getElementById('gallery-empty');
const countEl     = document.getElementById('gallery-count');
const refreshBtn  = document.getElementById('refresh-btn');
const launchToast = document.getElementById('launch-toast');

// Code modal
const codeModal   = document.getElementById('code-modal');
const codeTitle   = document.getElementById('code-modal-title');
const codeMeta    = document.getElementById('code-modal-meta');
const codeContent = document.getElementById('code-modal-content');
const codeCopyBtn = document.getElementById('code-copy-btn');
const codeChatBtn = document.getElementById('code-chat-btn');
const codeCloseBtn= document.getElementById('code-close-btn');

let currentGame = null;
let toastTimer  = null;

// ── Init ───────────────────────────────────────────────────────────────────
(async function init() {
  await loadGallery();
  refreshBtn.addEventListener('click', loadGallery);

  codeCloseBtn.addEventListener('click', closeCode);
  codeCopyBtn.addEventListener('click', onCopy);
  codeChatBtn.addEventListener('click', openInChat);
  codeModal.addEventListener('click', e => { if (e.target === codeModal) closeCode(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCode(); });
})();

// ── Load gallery ───────────────────────────────────────────────────────────
async function loadGallery() {
  countEl.textContent = 'loading…';
  try {
    const res  = await fetch('/api/projects');
    const data = await res.json();
    await renderGallery(data.projects || []);
  } catch (e) {
    countEl.textContent = 'error loading games';
    console.error('Gallery load failed', e);
  }
}

async function renderGallery(names) {
  gameGrid.innerHTML = '';
  if (names.length === 0) {
    emptyMsg.style.display = '';
    countEl.textContent = '';
    return;
  }
  emptyMsg.style.display = 'none';
  countEl.textContent = `${names.length} game${names.length !== 1 ? 's' : ''}`;

  // Build all cards in parallel but append in original sorted order.
  // Promise.all preserves index order even when individual fetches resolve at
  // different speeds, so games never jump position when one gets updated.
  const cards = await Promise.all(names.map(name => buildCard(name)));
  cards.forEach(card => gameGrid.appendChild(card));
}

async function buildCard(name) {
  let lineCount = '?';
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/source`);
    const data = await res.json();
    if (data.source) lineCount = data.source.split('\n').length;
  } catch (_) {}

  const displayName = name.replace(/_/g, ' ');

  const card = document.createElement('div');
  card.className = 'game-card';
  card.innerHTML = `
    <div class="card-header">
      <div class="card-name">${esc(displayName)}</div>
      <span class="card-badge unknown" id="badge-${esc(name)}">…</span>
    </div>
    <div class="card-meta">
      <span>${lineCount} lines</span>
      <span>games/${esc(name)}/game.py</span>
    </div>
    <div class="card-actions">
      <button class="btn-play" id="play-${esc(name)}">▶ Play</button>
      <button class="btn-code" id="code-${esc(name)}">&lt;/&gt; Code</button>
    </div>
  `;

  card.querySelector(`#play-${CSS.escape(name)}`).addEventListener('click', () => {
    window.open(`/play/${encodeURIComponent(name)}`, '_blank');
  });
  card.querySelector(`#code-${CSS.escape(name)}`).addEventListener('click', () => showCode(name));

  // Async badge: check verify status
  checkVerify(name);

  return card;
}

// ── Verify badge (fire-and-forget per card) ────────────────────────────────
async function checkVerify(name) {
  const badge = document.getElementById(`badge-${CSS.escape(name)}`);
  if (!badge) return;
  try {
    const res  = await fetch(`/api/games/${encodeURIComponent(name)}/verify`);
    const data = await res.json();
    if (data.passed) {
      badge.textContent = '✓ playable';
      badge.className = 'card-badge pass';
    } else {
      badge.textContent = `✗ ${data.error_type || 'error'}`;
      badge.className = 'card-badge fail';
    }
  } catch (_) {
    badge.textContent = 'unknown';
    badge.className = 'card-badge unknown';
  }
}


// ── Code viewer ────────────────────────────────────────────────────────────
async function showCode(name) {
  currentGame = name;
  codeTitle.textContent = name.replace(/_/g, ' ');
  codeMeta.textContent  = 'Loading…';
  codeContent.textContent = '';
  codeModal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/source`);
    const data = await res.json();
    const src  = data.source || '';
    codeContent.textContent = src;
    codeMeta.textContent = `${src.split('\n').length} lines  •  games/${name}/game.py  •  run: python game.py`;
  } catch (_) {
    codeMeta.textContent = 'Failed to load source';
  }
}

function closeCode() {
  codeModal.style.display = 'none';
  document.body.style.overflow = '';
  currentGame = null;
}

function openInChat() {
  if (!currentGame) return;
  window.location.href = `/chat?project=${encodeURIComponent(currentGame)}`;
}

function onCopy() {
  const src = codeContent.textContent.trim();
  if (!src) return;
  navigator.clipboard.writeText(src).then(() => {
    codeCopyBtn.textContent = '✓ Copied';
    setTimeout(() => { codeCopyBtn.textContent = '⧉ Copy'; }, 1500);
  });
}


// ── Util ───────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
