'use strict';
/* SignalForge page navigation. Dashboard is the command center overview; Map remains a separate focused view.
   Every other menu entry opens a page of its own. Pages register themselves here and are built on first use.
   This file loads before app.js: it only defines things, and touches app.js globals (S, el, $ ...) lazily. */

const PAGES = {};
const NAV = [['dashboard', '◈ Command Center'], ['nodes', 'Nodes'], ['chats', 'Chats'], ['air', '✈ Air'], ['radio', 'Radio'],
  ['weather', 'Weather'], ['pager', 'Pagers'], ['sensors', 'Sensors'], ['sky', 'Satellites']];
const pageEls = {}, pageBuilt = {};
let pageQueued = false;

function registerPage(name, spec) { PAGES[name] = spec; }

function buildNav() {
  const nav = $('#pagenav');
  nav.replaceChildren();
  for (const [name, label] of NAV) {
    nav.append(el('button', { id: `nav-${name}`, role: 'tab', 'aria-controls': name === 'dashboard' ? 'layout' : `page-${name}`, dataset: { page: name }, onclick: () => setPage(name, name === 'dashboard') }, label, el('span', { id: `nav-badge-${name}`, class: 'badge hidden' })));
  }
  nav.addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    const idx = NAV.findIndex(([n]) => n === S.page);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? NAV.length - 1 : (idx + (e.key === 'ArrowRight' ? 1 : -1) + NAV.length) % NAV.length;
    setPage(NAV[next][0], NAV[next][0] === 'dashboard');
    $(`#nav-${S.page}`).focus();
  });
}

function setPage(name, forceCommand = false) {
  if (name !== 'dashboard' && !PAGES[name]) name = 'dashboard';
  const prev = S.page;
  if (prev !== name && prev && prev !== 'dashboard') PAGES[prev].leave?.();
  S.page = name;
  document.body.classList.toggle('page-open', name !== 'dashboard');
  for (const [n] of NAV) {
    $(`#nav-${n}`)?.classList.toggle('active', n === name);
    $(`#nav-${n}`)?.setAttribute('aria-selected', String(n === name));
    $(`#nav-${n}`)?.setAttribute('tabindex', n === name ? '0' : '-1');
    pageEls[n]?.classList.toggle('hidden', n !== name);
  }
  if (name === 'dashboard') {
    // Clicking Command Center always returns to the overview. A saved map view remains
    // available on reload until the operator explicitly chooses Command Center.
    if (forceCommand) setView('command');
    scheduleCommand();
  } else {
    if (!pageBuilt[name]) {
      const sec = el('section', { id: `page-${name}`, class: 'page', role: 'tabpanel', 'aria-labelledby': `nav-${name}` });
      document.body.insertBefore(sec, $('#toasts'));
      pageEls[name] = sec;
      PAGES[name].build(sec);
      pageBuilt[name] = true;
    }
    pageEls[name].classList.remove('hidden');
    if (prev !== name) PAGES[name].enter?.();
    PAGES[name].render?.();
  }
  const want = name === 'dashboard' ? '' : `#/${name}`;
  if (location.hash !== want && (want || location.hash)) history.pushState(null, '', want || location.pathname + location.search);
  if (S.conv && viewingChat()) { markRead(S.conv); renderTop(); renderChats(); }
}
function pageFromHash() {
  const m = /^#\/([a-z]+)$/.exec(location.hash);
  return m && (PAGES[m[1]] || m[1] === 'dashboard') ? m[1] : 'dashboard';
}
function schedulePage() {
  if (S.page === 'dashboard' || pageQueued) return;
  pageQueued = true;
  requestAnimationFrame(() => { pageQueued = false; PAGES[S.page]?.render?.(); });
}
function renderNav() {
  const set = (id, text, cls = '') => { const b = $(`#nav-badge-${id}`); if (!b) return; b.classList.toggle('hidden', !text); b.textContent = text || ''; b.className = `badge ${cls}${text ? '' : ' hidden'}`; };
  set('chats', totalUnread() || '');
  set('air', S.air.enabled ? S.air.aircraft.length : '', 'live');
  const j = S.job || {};
  set('pager', j.enabled && j.kind === 'pager' ? '●' : '', 'live');
  set('sensors', j.enabled && j.kind === 'sensors' ? '●' : '', 'live');
  set('radio', S.radio && (S.radio.enabled || S.radio.listening) ? '●' : '', 'live');
  const alerts = S.weather?.data?.alerts?.length || 0;
  set('weather', alerts || '', 'hot');
}

// ---- shared bits ------------------------------------------------------------------------------------
function serviceAvailable(name) {
  return S.capabilities[name] ?? (name === 'weather' ? !!S.weather : null);
}
function sdrCapability(mode) {
  if (!S.connected) return { available: false, error: 'Reconnecting to the server…' };
  return S.radio?.capabilities?.[mode] || { available: !!S.radio?.available, error: S.radio ? 'Required SDR tools are not installed.' : 'SDR is not enabled on this server.' };
}
function frequencyValue(value) {
  const text = String(value).trim().replace(',', '.');
  if (!/^\d+(?:\.\d+)?$/.test(text)) return null;
  const n = Number(text);
  return n >= 24 && n <= 1766 ? n : null;
}
function pageShell(title, ...headItems) {
  const head = el('header', { class: 'pg-head' }, el('h1', { text: title }), ...headItems);
  const body = el('div', { class: 'pg-body' });
  return { root: el('div', { class: 'pg' }, head, body), head, body };
}
function box(title, body, { span = false, action = null } = {}) {
  return el('section', { class: 'box' + (span ? ' span' : '') }, el('h3', {}, el('span', { text: title }), action), body);
}
function sdrState() {
  const r = S.radio, j = S.job || {};
  if (!S.connected) return { on: false, err: true, text: 'server disconnected' };
  if (!r) return { on: false, err: false, text: 'SDR not available' };
  if (!r.available) return { on: false, err: true, text: 'SDR tools not installed' };
  if (r.listening) return { on: true, err: false, text: `dongle: listening ${r.listening} MHz` };
  if (j.enabled) return { on: true, err: false, text: `dongle: ${j.kind === 'pager' ? 'decoding pagers' : 'reading sensors'} ${j.mhz} MHz` };
  if (S.air.enabled) return { on: true, err: false, text: 'dongle: tracking aircraft' };
  if (r.enabled) return { on: true, err: false, text: 'dongle: surveying bands' };
  return { on: false, err: false, text: 'dongle: free' };
}
function dongleChip() {
  const chip = el('span', { class: 'dongle', title: 'The SDR dongle can only do one thing at a time. Starting something here stops whatever else is using it.' }, el('i'), el('span'));
  chip.update = () => { const s = sdrState(); chip.className = `dongle${s.on ? ' on' : ''}${s.err ? ' err' : ''}`; chip.lastChild.textContent = s.text; };
  chip.update();
  return chip;
}
function sortBy(rows, key, dir, get) {
  const f = get[key];
  if (!f) return rows;
  return [...rows].sort((a, b) => {
    const x = f(a), y = f(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1; // missing values always last
    if (y == null) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
  });
}
function makeMap(host, { center = [39, -98], zoom = 4, offline = false } = {}) {
  const m = L.map(host, { zoomControl: true, doubleClickZoom: false, worldCopyJump: true }).setView(center, zoom);
  if (offline && typeof makeOfflineLayer === 'function') makeOfflineLayer(m).addTo(m);
  else L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(m);
  new ResizeObserver(() => m.invalidateSize()).observe(host);
  return m;
}
function svgEl(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  parent?.append(e);
  return e;
}
function attachRight(host) { host.append($('#right')); }
function detachRight() { $('#layout').append($('#right')); }

// ---- Nodes page -------------------------------------------------------------------------------------
const NP = { sort: 'heard', dir: -1, filter: 'all', q: '' };
const NODE_COLS = [
  ['name', 'Node', n => (n.short || '').toLowerCase()], ['status', 'Status', n => ({ live: 0, day: 1, old: 2 })[clsOf(n)]],
  ['heard', 'Last heard', n => n.heard || 0], ['hops', 'Hops', n => n.hops], ['snr', 'Signal', n => n.snr],
  ['batt', 'Power', n => n.batt], ['dist', 'From you', n => distFromMe(n)], ['hw', 'Hardware', n => n.hw || null],
  ['near', 'Near', n => n.near || null], ['id', 'ID', n => n.id],
];
function distFromMe(n) {
  const mine = myPos();
  return mine && hasPos(n) ? haversineKm(mine, [n.lat, n.lon]) : null;
}
function nodesForPage() {
  const q = NP.q.trim().toLowerCase();
  const rows = [...S.nodes.values()].filter(n => {
    if (n.id === S.me) return false;
    const c = clsOf(n);
    if (NP.filter === 'live' && c !== 'live') return false;
    if (NP.filter === 'today' && c === 'old') return false;
    if (NP.filter === 'direct' && n.hops !== 0) return false;
    if (NP.filter === 'mapped' && !hasPos(n)) return false;
    return !q || `${n.short} ${n.long} ${n.id} ${n.hw || ''} ${n.near || ''}`.toLowerCase().includes(q);
  });
  const get = Object.fromEntries(NODE_COLS.map(([k, , f]) => [k, f]));
  return sortBy(rows, NP.sort, NP.dir, get);
}
const nodesPage = {
  build(sec) {
    const search = el('input', { class: 'field', type: 'search', placeholder: 'Search name, ID, hardware, place', style: 'width:280px', 'aria-label': 'Search nodes',
      oninput: e => { NP.q = e.target.value; nodesPage.render(); } });
    const chips = el('div', { class: 'chips', id: 'np-chips' });
    for (const [k, t] of [['all', 'All'], ['live', 'Live'], ['today', 'Today'], ['direct', 'Direct'], ['mapped', 'On map']]) {
      chips.append(el('button', { class: 'chip' + (k === NP.filter ? ' active' : ''), dataset: { f: k }, onclick: () => { NP.filter = k; nodesPage.render(); } }, t));
    }
    const sh = pageShell('Nodes', search, chips, el('span', { id: 'np-count', class: 'dim' }));
    const table = el('table', { class: 'tbl', id: 'np-table' });
    const thead = el('thead');
    const tr = el('tr');
    for (const [k, label] of NODE_COLS) {
      tr.append(el('th', { dataset: { k }, class: ['heard', 'hops', 'snr', 'batt', 'dist'].includes(k) ? 'num' : '', onclick: () => {
        if (NP.sort === k) NP.dir *= -1; else { NP.sort = k; NP.dir = ['heard', 'snr', 'batt'].includes(k) ? -1 : 1; }
        nodesPage.render();
      } }, label));
    }
    thead.append(tr);
    table.append(thead, el('tbody', { id: 'np-body' }));
    const kp = el('div', { class: 'kpi-row', id: 'np-kpis', style: 'margin-bottom:14px' });
    const main = el('div', { class: 'pg-main' }, el('div', { class: 'pg-body', style: 'padding-bottom:0' }, kp, el('div', { class: 'scroll box', style: 'padding:0' }, table)));
    main.firstChild.style.cssText = 'display:flex;flex-direction:column;min-height:0;overflow:hidden';
    main.firstChild.lastChild.style.cssText = 'padding:0;flex:1;min-height:0;overflow:auto';
    const host = el('div', { id: 'np-right', style: 'min-height:0;display:flex' });
    const split = el('div', { class: 'pg-split', style: 'min-height:0' }, main, host);
    sh.root.replaceChild(split, sh.body);
    sec.append(sh.root);
    nodesPage.host = host;
  },
  enter() { attachRight(nodesPage.host); renderDetail(); renderThread(); },
  leave() { detachRight(); },
  tick() { nodesPage.render(); },
  render() {
    for (const c of document.querySelectorAll('#np-chips .chip')) c.classList.toggle('active', c.dataset.f === NP.filter);
    for (const th of document.querySelectorAll('#np-table th')) { th.classList.toggle('sorted', th.dataset.k === NP.sort); th.classList.toggle('asc', th.dataset.k === NP.sort && NP.dir === 1); }
    const rows = nodesForPage();
    $('#np-count').textContent = `${rows.length} of ${S.nodes.size ? S.nodes.size - (meNode() ? 1 : 0) : 0}`;
    const all = [...S.nodes.values()].filter(n => n.id !== S.me);
    const live = all.filter(n => clsOf(n) === 'live'), snrs = all.filter(n => n.snr != null).map(n => n.snr);
    const far = all.map(distFromMe).filter(v => v != null);
    const kp = $('#np-kpis');
    kp.replaceChildren(...[[live.length, 'live now'], [all.filter(n => clsOf(n) !== 'old').length, 'heard today'], [all.length, 'remembered'],
      [all.filter(n => n.hops === 0).length, 'direct (0 hops)'], [snrs.length ? `${(snrs.reduce((a, b) => a + b, 0) / snrs.length).toFixed(1)} dB` : '–', 'average SNR'],
      [far.length ? `${Math.max(...far).toFixed(0)} km` : '–', 'farthest']].map(([v, t]) => el('div', {}, el('div', { class: 'big', text: String(v) }), el('span', { text: t }))));
    const convs = conversations();
    const body = $('#np-body');
    body.replaceChildren();
    if (!rows.length) body.append(el('tr', {}, el('td', { colspan: NODE_COLS.length, class: 'empty', text: 'No nodes match.' })));
    for (const n of rows) {
      const c = clsOf(n), d = distFromMe(n), unread = convs.get(n.id)?.unread || 0;
      const tr = el('tr', { class: 'row' + (n.id === S.sel ? ' sel' : ''), dataset: { id: n.id }, title: 'Click for details and to message. Double-click to open the chat.' },
        el('td', {}, el('span', { class: 'dot', style: `background:${colorOf(n.id)};display:inline-block;margin-right:8px` }), el('b', { text: n.short }),
          n.long && n.long !== n.short ? el('span', { class: 'dim', text: `  ${n.long}` }) : null, unread ? el('span', { class: 'badge', style: 'margin-left:6px', text: unread }) : null),
        el('td', {}, el('span', { class: `status-dot ${c}` }), c === 'live' ? 'live' : c === 'day' ? 'today' : 'remembered'),
        el('td', { class: 'num', text: n.heard ? `${ago(n.heard)} ago` : '–' }),
        el('td', { class: 'num', text: n.hops == null ? '–' : String(n.hops) }),
        el('td', { class: 'num' }, n.snr == null ? '–' : `${n.snr.toFixed(1)} dB `, sigBars(n.snr)),
        el('td', { class: 'num', text: n.batt == null ? '–' : n.batt > 100 ? '⚡ USB' : `${n.batt}%` }),
        el('td', { class: 'num', text: d == null ? '–' : `${d.toFixed(1)} km` }),
        el('td', { text: n.hw || '–' }), el('td', { text: n.near || (hasPos(n) ? '' : 'no position') }), el('td', { class: 'dim', text: n.id }));
      tr.addEventListener('click', () => selectNode(n.id));
      tr.addEventListener('dblclick', () => openChat(n.id));
      body.append(tr);
    }
  },
};
registerPage('nodes', nodesPage);

// ---- Chats page -------------------------------------------------------------------------------------
const chatsPage = {
  build(sec) {
    const pick = el('select', { class: 'field', id: 'cp-new', 'aria-label': 'Start a conversation', onchange: e => { if (e.target.value) openChat(e.target.value); e.target.value = ''; } });
    const sh = pageShell('Chats', pick, el('span', { class: 'dim', text: 'Each node has its own conversation. Direct messages show delivery.' }));
    const listHost = el('div', { class: 'chat-list-host', id: 'cp-list' });
    const right = el('div', { id: 'cp-right', style: 'min-height:0;display:flex;flex-direction:column' });
    const split = el('div', { class: 'pg-chats chats-page', style: 'min-height:0' }, listHost, right);
    sh.root.replaceChild(split, sh.body);
    sec.append(sh.root);
    chatsPage.list = listHost; chatsPage.right = right;
  },
  enter() {
    chatsPage.list.append($('#chat-list'));
    attachRight(chatsPage.right);
    $('#right').classList.add('chats-page');
    renderChats(); renderThread();
  },
  leave() { $('#pane-chats').append($('#chat-list')); $('#right').classList.remove('chats-page'); detachRight(); },
  render() {
    const pick = $('#cp-new');
    if (pick.dataset.n !== String(S.nodes.size)) {
      pick.dataset.n = String(S.nodes.size);
      pick.replaceChildren(el('option', { value: '', text: '+ New message to…' }),
        ...[...S.nodes.values()].filter(n => n.id !== S.me).sort((a, b) => (b.heard || 0) - (a.heard || 0)).map(n => el('option', { value: n.id, text: `${n.short} · ${n.long || n.id}` })));
    }
  },
};
registerPage('chats', chatsPage);
