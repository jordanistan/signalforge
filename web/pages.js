'use strict';
/* SignalForge page navigation. Dashboard is the command center overview; Map remains a separate focused view.
   Every other menu entry opens a page of its own. Pages register themselves here and are built on first use.
   This file loads before app.js: it only defines things, and touches app.js globals (S, el, $ ...) lazily. */

// OpenStreetMap's tile servers answer 403 "Access blocked" without a Referer, and the page sends none
// (Referrer-Policy: no-referrer keeps the ?token= URL private). Tiles send only the origin.
const OSM_OPTS = { maxZoom: 19, attribution: '© OpenStreetMap contributors', referrerPolicy: 'origin', className: 'tiles-street' };
const PAGES = {};
const NAV = [['dashboard', '◈ Command Center'], ['nodes', 'Nodes'], ['chats', 'Chats'], ['air', '✈ Air'], ['radio', 'Radio'],
  ['weather', 'Weather'], ['sensors', 'Sensors'], ['sky', 'Satellites']];
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
    renderSdrLost(name);
  }
  const want = name === 'dashboard' ? '' : `#/${name}`;
  if (location.hash !== want && (want || location.hash)) history.pushState(null, '', want || location.pathname + location.search);
  if (S.conv && viewingChat()) { markRead(S.conv); renderTop(); renderChats(); }
  radarLockSoon(); // shown again: re-pin the radar animations to the shared clock
}
function pageFromHash() {
  const m = /^#\/([a-z]+)$/.exec(location.hash);
  return m && (PAGES[m[1]] || m[1] === 'dashboard') ? m[1] : 'dashboard';
}
function schedulePage() {
  if (S.page === 'dashboard' || pageQueued) return;
  pageQueued = true;
  requestAnimationFrame(() => { pageQueued = false; PAGES[S.page]?.render?.(); renderSdrLost(S.page); });
}
function renderNav() {
  const set = (id, text, cls = '') => { const b = $(`#nav-badge-${id}`); if (!b) return; b.classList.toggle('hidden', !text); b.textContent = text || ''; b.className = `badge ${cls}${text ? '' : ' hidden'}`; };
  set('chats', totalUnread() || '');
  set('air', S.air.enabled ? S.air.aircraft.length : '', 'live');
  const j = S.job || {};
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
// ---- who has the dongle: one SDR program at a time, so starting one stops another -------------------------
const SDR_JOBS = { survey: 'the band survey', audio: 'radio listening', aircraft: 'aircraft tracking', sensors: 'sensor reading' };
const SDR_PAGE = { survey: 'radio', audio: 'radio', aircraft: 'air', sensors: 'sensors' };
function sdrOwner() {
  const r = S.radio, j = S.job;
  if (r?.listening) return 'audio';
  if (j?.enabled && SDR_JOBS[j.kind]) return j.kind;
  if (S.air?.enabled) return 'aircraft';
  if (r?.enabled) return 'survey';
  return null;
}
// Called after every server frame. When one program replaces another (directly, or through a short idle gap
// while the server swaps them), remember it so the page that lost the dongle can say so.
const SDR_SWAP_MS = 4000;
function trackSdrOwner() {
  const cur = sdrOwner(), prev = S.sdrOwner;
  if (cur === prev) return;
  S.sdrOwner = cur;
  S.sdrLost ||= {};
  if (cur) {
    delete S.sdrLost[cur];
    const gone = prev || (S.sdrStopped && Date.now() - S.sdrStopped.at < SDR_SWAP_MS ? S.sdrStopped.kind : null);
    if (gone && gone !== cur && S.booted) S.sdrLost[gone] = { by: cur, at: Date.now() };
    S.sdrStopped = null;
  } else if (prev) S.sdrStopped = { kind: prev, at: Date.now() };
  schedulePage();
}
// "Starting this stops …" for a start button, or '' when the dongle is free or already doing `kind`.
function sdrTakeoverHint(kind) {
  const cur = sdrOwner();
  return cur && cur !== kind && !(kind === 'survey' && cur === 'audio') ? `Starting this stops ${SDR_JOBS[cur]}.` : '';
}
const SDR_RESUME = {
  survey: () => radioSend('start'), aircraft: () => radioSend('aircraft_start'),
  sensors: () => $('#sensors-toggle')?.click(),
};
// The inline notice at the top of a page whose program lost the dongle to another one.
function renderSdrLost(page) {
  const head = pageEls[page]?.querySelector('.pg-head');
  if (!head) return;
  const kinds = Object.keys(S.sdrLost || {}).filter(k => SDR_PAGE[k] === page);
  let note = head.querySelector('.sdr-lost');
  if (!kinds.length) { note?.remove(); return; }
  const k = kinds[0], x = S.sdrLost[k];
  const at = new Date(x.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const fresh = el('div', { class: 'sdr-lost', role: 'status', id: `sdr-lost-${page}` },
    el('span', { text: `Stopped: the dongle was switched to ${SDR_JOBS[x.by]} at ${at}.` }),
    SDR_RESUME[k] ? el('button', { class: 'btn small primary', title: `Starting this stops ${SDR_JOBS[x.by]}.`, onclick: () => { delete S.sdrLost[k]; SDR_RESUME[k](); renderSdrLost(page); }, text: '▶ Resume' }) : null,
    el('button', { class: 'btn small', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => { delete S.sdrLost[k]; renderSdrLost(page); }, text: '✕' }));
  if (note) note.replaceWith(fresh); else head.append(fresh);
}
function sdrState() {
  const r = S.radio, j = S.job || {};
  if (!S.connected) return { on: false, err: true, text: 'server disconnected' };
  if (!r) return { on: false, err: false, text: 'SDR not available' };
  if (!r.available) return { on: false, err: true, text: 'SDR tools not installed' };
  if (r.listening) return { on: true, err: false, text: `dongle: listening ${r.listening} MHz` };
  if (j.enabled) return { on: true, err: false, text: `dongle: reading sensors ${fmtJobMhz(j.mhz)}` };
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
function svgEl(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  parent?.append(e);
  return e;
}
function attachRight(host) { host.append($('#right')); }
function detachRight() { $('#layout').append($('#right')); }

// ---- Nodes page -------------------------------------------------------------------------------------
// A side list of nodes next to a satellite map centred on you: range rings, a radar sweep that "pings" each
// node as it passes, and the observed direct reach (the farthest positioned node heard with 0 hops today).
const NP = { sort: 'dist', dir: 1, filter: 'all', q: '' };
const NODE_COLS = [
  ['name', 'Node', n => (n.short || '').toLowerCase()], ['dist', 'From you', n => distFromMe(n)],
  ['snr', 'Signal', n => n.snr], ['hops', 'Hops', n => n.hops], ['heard', 'Heard', n => n.heard || 0],
];

function distFromMe(n) {
  const mine = myPos();
  return mine && hasPos(n) ? haversineKm(mine, [n.lat, n.lon]) : null;
}
const snrClass = snr => (snr == null ? '' : snr >= 0 ? 'good' : snr >= -10 ? 'fair' : 'weak');

const NM = { map: null, markers: new Map(), sig: '', fitted: false, bounds: null };

function nodesMapInit(host) {
  const m = L.map(host, { zoomControl: true, doubleClickZoom: false, worldCopyJump: true }).setView([39, -98], 4);
  const { bases, labels } = addRadarBases(m, 'mesh.nodes.base');
  NM.labels = labels;
  NM.radarR = makeRadar();
  NM.sweep = NM.radarR.sweep;
  NM.links = L.layerGroup().addTo(m);
  NM.nodes = L.layerGroup().addTo(m);
  NM.radar = NM.radarR.group.addTo(m);
  L.control.layers(bases, { 'Place labels': NM.labels, 'Radar & rings': NM.radar, 'Direct links': NM.links, Nodes: NM.nodes }, { collapsed: true }).addTo(m);
  const Btns = L.Control.extend({
    onAdd() {
      const box = L.DomUtil.create('div');
      L.DomEvent.disableClickPropagation(box);
      box.append(el('button', { class: 'map-btn', id: 'np-fit', title: 'Zoom to the range rings', text: '◎ Range', onclick: nodesMapFit }));
      return box;
    },
  });
  new Btns({ position: 'topleft' }).addTo(m);
  const Legend = L.Control.extend({ onAdd() { const d = L.DomUtil.create('div', 'nm-legend'); d.id = 'np-legend'; return d; } });
  new Legend({ position: 'bottomleft' }).addTo(m);
  new ResizeObserver(() => { m.invalidateSize(); NM.sweep._update(); if (!NM.fitted) nodesMapFit(); }).observe(host);
  NM.map = m;
}
function nodesMapFit() {
  if (!NM.map || !NM.map.getSize().x) return;
  if (NM.bounds) { NM.map.fitBounds(NM.bounds, { padding: [20, 20] }); NM.fitted = true; }
}
function syncNodesMap() {
  if (!NM.map) return;
  const me = myPos();
  // node markers: same look as the main map, but their own set
  const seen = new Set();
  for (const n of S.nodes.values()) {
    if (!hasPos(n)) continue;
    seen.add(n.id);
    const st = markerStyle(n);
    let mk = NM.markers.get(n.id);
    if (!mk) {
      mk = L.circleMarker([n.lat, n.lon], st).addTo(NM.nodes);
      mk.on('click', () => selectNode(n.id));
      mk.on('dblclick', () => openChat(n.id));
      NM.markers.set(n.id, mk);
    } else { mk.setLatLng([n.lat, n.lon]); mk.setStyle(st); mk.setRadius(st.radius); }
    const elm = mk.getElement();
    if (elm) {
      elm.setAttribute('data-id', n.id);
      elm.classList.remove('cls-live', 'cls-day', 'cls-old');
      elm.classList.add(`cls-${clsOf(n)}`);
      elm.classList.toggle('selected', n.id === S.sel);
      elm.classList.toggle('me', n.id === S.me);
    }
    const d = me && n.id !== S.me ? haversineKm(me, [n.lat, n.lon]) : null;
    const label = clsOf(n) !== 'old' || n.id === S.sel || n.id === S.me;
    const key = `${n.short}|${label}|${d == null ? '' : fmtKm(d)}`;
    if (mk._tk !== key) {
      mk._tk = key;
      mk.unbindTooltip();
      mk.bindTooltip(el('span', {}, el('b', { text: n.short }), d == null ? '' : `  ${fmtKm(d)} km`), { permanent: label, direction: 'right', offset: [8, 0], className: 'node-label' });
    }
    if (n.id === S.sel) mk.bringToFront();
  }
  for (const [id, mk] of NM.markers) if (!seen.has(id)) { NM.nodes.removeLayer(mk); NM.markers.delete(id); }

  // rings, sweep, blips (shared mesh radar) and direct links: only rebuilt when something they depend on changes
  const M = syncMeshRadar(NM.radarR);
  const legend = $('#np-legend');
  const sig = M ? M.sig : 'none';
  if (sig === NM.sig) return;
  NM.sig = sig;
  NM.links.clearLayers();
  if (!M) {
    NM.bounds = null;
    legend?.replaceChildren(el('b', { text: 'No position for you' }), el('div', { class: 'dim', text: 'Start with --home LAT,LON or give the radio a GPS fix to see distances.' }));
    return;
  }
  const { step, reach } = M;
  for (const { n, d } of M.direct) {
    L.polyline([me, [n.lat, n.lon]], { interactive: true, className: `nm-link ${snrClass(n.snr)}` })
      .bindTooltip(el('span', { text: `${n.short}: ${fmtKm(d)} km direct${n.snr == null ? '' : `, SNR ${n.snr.toFixed(1)} dB`}` }), { sticky: true })
      .addTo(NM.links);
  }
  NM.bounds = L.latLng(me).toBounds(M.outer * 2000);
  if (!NM.fitted) nodesMapFit();
  legend?.replaceChildren(
    el('div', {}, el('i', { class: 'lg-ring' }), `Rings every ${kmMi(step)}`),
    el('div', {}, el('i', { class: 'lg-reach' }), reach ? `Direct reach ${kmMi(reach)}` : 'No direct (0-hop) node with a position today'),
    el('div', {}, el('i', { class: 'lg-link' }), 'Direct links, coloured by signal'),
    el('div', { class: 'dim', text: 'Reach = farthest node your radio heard directly in the last 24 h. Real coverage depends on terrain, height and antennas.' }));
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
    const search = el('input', { class: 'field', type: 'search', placeholder: 'Search name, ID, hardware, place', style: 'width:260px', 'aria-label': 'Search nodes',
      oninput: e => { NP.q = e.target.value; nodesPage.render(); } });
    const chips = el('div', { class: 'chips', id: 'np-chips' });
    for (const [k, t] of [['all', 'All'], ['live', 'Live'], ['today', 'Today'], ['direct', 'Direct'], ['mapped', 'On map']]) {
      chips.append(el('button', { class: 'chip' + (k === NP.filter ? ' active' : ''), dataset: { f: k }, onclick: () => { NP.filter = k; nodesPage.render(); } }, t));
    }
    const sh = pageShell('Nodes', search, chips, el('span', { id: 'np-count', class: 'dim' }));
    const table = el('table', { class: 'tbl', id: 'np-table' });
    const tr = el('tr');
    for (const [k, label] of NODE_COLS) {
      tr.append(el('th', { dataset: { k }, class: k === 'name' ? '' : 'num', onclick: () => {
        if (NP.sort === k) NP.dir *= -1; else { NP.sort = k; NP.dir = ['heard', 'snr'].includes(k) ? -1 : 1; }
        nodesPage.render();
      } }, label));
    }
    table.append(el('thead', {}, tr), el('tbody', { id: 'np-body' }));
    const list = el('div', { class: 'np-list' }, el('div', { class: 'kpi-row np-kpis', id: 'np-kpis' }), el('div', { class: 'scroll' }, table));
    const mapHost = el('div', { id: 'np-map', class: 'dmap' });
    const host = el('div', { id: 'np-right', style: 'min-height:0;display:flex' });
    const split = el('div', { class: 'np-split' }, list, el('div', { class: 'np-mapwrap' }, mapHost), host);
    sh.root.replaceChild(split, sh.body);
    sec.append(sh.root);
    nodesPage.host = host;
    nodesMapInit(mapHost);
  },
  enter() { attachRight(nodesPage.host); renderDetail(); renderThread(); NM.map?.invalidateSize(); },
  leave() { detachRight(); },
  tick() { nodesPage.render(); },
  render() {
    for (const c of document.querySelectorAll('#np-chips .chip')) c.classList.toggle('active', c.dataset.f === NP.filter);
    for (const th of document.querySelectorAll('#np-table th')) { th.classList.toggle('sorted', th.dataset.k === NP.sort); th.classList.toggle('asc', th.dataset.k === NP.sort && NP.dir === 1); }
    const rows = nodesForPage();
    $('#np-count').textContent = `${rows.length} of ${S.nodes.size ? S.nodes.size - (meNode() ? 1 : 0) : 0}`;
    const all = [...S.nodes.values()].filter(n => n.id !== S.me);
    const live = all.filter(n => clsOf(n) === 'live');
    const near = all.map(distFromMe).filter(v => v != null);
    $('#np-kpis').replaceChildren(...[[live.length, 'live now'], [all.filter(n => clsOf(n) !== 'old').length, 'heard today'],
      [all.filter(n => n.hops === 0).length, 'direct'], [near.length ? `${fmtKm(Math.min(...near))} km` : '–', 'closest']]
      .map(([v, t]) => el('div', {}, el('div', { class: 'big', text: String(v) }), el('span', { text: t }))));
    const convs = conversations();
    const body = $('#np-body');
    body.replaceChildren();
    if (!rows.length) body.append(el('tr', {}, el('td', { colspan: NODE_COLS.length, class: 'empty', text: 'No nodes match.' })));
    for (const n of rows) {
      const c = clsOf(n), d = distFromMe(n), unread = convs.get(n.id)?.unread || 0;
      const tr = el('tr', { class: 'row' + (n.id === S.sel ? ' sel' : ''), dataset: { id: n.id }, title: `${n.long || n.short} · ${n.id}\nClick to show on the map. Double-click to open the chat.` },
        el('td', {}, el('span', { class: `status-dot ${c}` }), el('span', { class: 'dot', style: `background:${colorOf(n.id)};display:inline-block;margin-right:6px` }),
          el('b', { text: n.short }), unread ? el('span', { class: 'badge', style: 'margin-left:6px', text: unread }) : null),
        el('td', { class: 'num', text: d == null ? '–' : `${fmtKm(d)} km` }),
        el('td', { class: 'num' }, n.snr == null ? '–' : `${n.snr.toFixed(0)} `, sigBars(n.snr)),
        el('td', { class: 'num', text: n.hops == null ? '–' : String(n.hops) }),
        el('td', { class: 'num', text: n.heard ? ago(n.heard) : '–' }));
      tr.addEventListener('click', () => { selectNode(n.id); if (hasPos(n)) NM.map?.panTo([n.lat, n.lon]); });
      tr.addEventListener('dblclick', () => openChat(n.id));
      body.append(tr);
    }
    syncNodesMap();
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
