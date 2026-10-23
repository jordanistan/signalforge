'use strict';
/* SignalForge browser console. Talks to tools/meshweb.py over /ws. Everything that comes from the mesh (node names,
   message text) is untrusted: it is only ever put into the page with textContent / text nodes, never innerHTML. */

const BROADCAST = '^all';
const PALETTE = ['#7aa2f7', '#bb9af7', '#9ece6a', '#e0af68', '#f7768e', '#2ac3de', '#ff9e64', '#73daca'];
const LIVE_SECS = 2 * 3600, DAY_SECS = 86400, MAX_BYTES = 200;

const S = {
  me: null, error: null, home: null, allow: [], capabilities: {}, capabilityErrors: {}, demo: false, location: null,
  nodes: new Map(), msgs: [], acks: new Map(), follows: [], radio: null,
  air: { enabled: false, aircraft: [], reference: null, reference_assumed: true, stats: null }, selAir: null,
  job: null, weather: null, sats: null, page: 'dashboard',
  events: [], view: 'command', booted: false, radioMode: 'idle',
  sel: null, conv: BROADCAST, tab: 'nodes', filter: 'all', q: '',
  lastRead: {}, connected: false,
};

// ---- helpers ---------------------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const nowSec = () => Date.now() / 1000;

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'style') e.style.cssText = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c); // strings become text nodes
  return e;
}

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t;
})();
function crc32(str) {
  let c = 0xFFFFFFFF;
  for (const b of new TextEncoder().encode(str)) c = CRC[(c ^ b) & 255] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}
const colorOf = id => PALETTE[crc32(String(id)) % PALETTE.length]; // same colours as the terminal UI

function ago(ts, now = nowSec()) {
  if (!ts) return '–';
  const s = Math.max(0, Math.floor(now - ts));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : s < DAY_SECS ? `${Math.floor(s / 3600)}h` : `${Math.floor(s / DAY_SECS)}d`;
}
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const fmtDay = ts => new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
const fmtFull = ts => new Date(ts * 1000).toLocaleString();
function fmtDur(s) {
  s = Math.floor(s);
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : s < DAY_SECS ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : `${Math.floor(s / DAY_SECS)}d`;
}

function haversineKm(a, b) {
  const r = Math.PI / 180, dLat = (b[0] - a[0]) * r, dLon = (b[1] - a[1]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLon / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

const clsOf = n => {
  if (!n.heard) return 'old';
  const a = nowSec() - n.heard;
  return a < LIVE_SECS ? 'live' : a < DAY_SECS ? 'day' : 'old';
};
const isBroadcast = to => to == null || to === BROADCAST || to === '!ffffffff' || to === 4294967295;
const convOf = m => (m.dir === 'out' ? (isBroadcast(m.to) ? BROADCAST : m.to) : (isBroadcast(m.to) ? BROADCAST : m.from));
const nodeName = id => (id === BROADCAST ? '# Channel' : (S.nodes.get(id)?.short && S.nodes.get(id).short !== '?' ? S.nodes.get(id).short : id));
const hasPos = n => n.lat != null && n.lon != null;
const meNode = () => S.nodes.get(S.me);
const myPos = () => { const n = meNode(); return n && hasPos(n) ? [n.lat, n.lon] : S.home; };

function sigBars(snr) {
  const n = snr == null ? 0 : snr >= 5 ? 4 : snr >= 0 ? 3 : snr >= -5 ? 2 : snr >= -10 ? 1 : 0;
  return el('span', { class: `bars b${n}`, title: snr == null ? 'no signal data' : `SNR ${snr.toFixed(1)} dB` }, el('i'), el('i'), el('i'), el('i'));
}
function gauge(pct, title) {
  return el('span', { class: 'gauge', title }, el('b', { style: `width:${Math.max(0, Math.min(100, pct))}%` }));
}
function spark(vals) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'spark');
  svg.setAttribute('viewBox', '0 0 100 30');
  svg.setAttribute('preserveAspectRatio', 'none');
  if (vals.length < 2) return svg;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * 100},${28 - ((v - lo) / span) * 26}`).join(' ');
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', pts);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--accent)');
  line.setAttribute('stroke-width', '1.5');
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.append(line);
  return svg;
}

// ---- conversations & unread ------------------------------------------------------------------------
function loadLastRead() {
  try { return JSON.parse(localStorage.getItem('mesh.lastRead') || 'null'); } catch { return null; }
}
function saveLastRead() {
  try { localStorage.setItem('mesh.lastRead', JSON.stringify(S.lastRead)); } catch { /* private mode */ }
}
function conversations() {
  const map = new Map([[BROADCAST, { key: BROADCAST, msgs: [], unread: 0, last: 0 }]]);
  for (const m of S.msgs) {
    const key = convOf(m);
    if (!map.has(key)) map.set(key, { key, msgs: [], unread: 0, last: 0 });
    const c = map.get(key);
    c.msgs.push(m);
    c.last = Math.max(c.last, m.ts);
    if (m.dir === 'in' && m.ts > (S.lastRead[key] || 0)) c.unread++;
  }
  return map;
}
function markRead(key) {
  const last = Math.max(0, ...S.msgs.filter(m => convOf(m) === key).map(m => m.ts));
  if (last > (S.lastRead[key] || 0)) { S.lastRead[key] = last; saveLastRead(); }
}
const viewingChat = () => {
  const r = $('#chat')?.getBoundingClientRect();
  return document.visibilityState === 'visible' && r && r.width > 0 && r.height > 0 && r.left < innerWidth && r.top < innerHeight;
};

// ---- websocket -------------------------------------------------------------------------------------
let ws = null, retry = 0;
function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { retry = 0; S.connected = true; renderAll(); };
  ws.onclose = () => { S.connected = false; renderAll(); setTimeout(connect, Math.min(10000, 500 * 2 ** retry++)); };
  ws.onmessage = ev => { let d; try { d = JSON.parse(ev.data); } catch { return; } handle(d); };
}
function handle(d) {
  handleFrame(d);
  trackSdrOwner();
}
function handleFrame(d) {
  if (d.type === 'state') {
    S.me = d.me; S.error = d.error; S.home = d.home; S.allow = d.allow || []; S.follows = d.follows || []; S.radio = d.radio || null;
    S.capabilities = d.capabilities || {}; S.capabilityErrors = d.capability_errors || {}; S.demo = !!d.demo; S.bridge = !!d.bridge; S.debugUi = !!d.debug_ui; S.location = d.location || null;
    if (d.aircraft) { S.air = d.aircraft; airIngest(S.air.aircraft); }
    S.job = d.job || null; S.weather = d.weather || null;
    S.nodes = new Map(d.nodes.map(n => [n.id, n]));
    S.msgs = d.messages;
    if (!loadLastRead()) { // first visit: existing history counts as read
      S.lastRead = {};
      for (const [k, c] of conversations()) S.lastRead[k] = c.last;
      saveLastRead();
    } else S.lastRead = loadLastRead();
    if (S.conv && viewingChat()) markRead(S.conv);
    firstMapView();
    renderAll();
    if (S.page === 'sky') skyRequest();
  } else if (d.type === 'nodes') {
    const prev = S.nodes;
    S.nodes = new Map(d.nodes.map(n => [n.id, n]));
    for (const n of S.nodes.values()) {
      const was = prev.get(n.id);
      if (n.id !== S.me && clsOf(n) === 'live' && (!was || clsOf(was) !== 'live') && S.booted) pushEvent('node', `${n.short} is live (${n.hops == null ? '?' : n.hops} hops)`);
    }
    renderNodesUI();
    scheduleCommand(); schedulePage();
  } else if (d.type === 'message') {
    S.msgs.push(d.msg);
    pushEvent('msg', (d.msg.dir === 'in' ? `${nodeName(d.msg.from)}: ` : `you → ${nodeName(convOf(d.msg))}: `) + d.msg.text.slice(0, 90));
    const key = convOf(d.msg);
    const open = key === S.conv && viewingChat();
    if (d.msg.dir === 'in') {
      if (open) markRead(key);
      else toast(nodeName(d.msg.from) + (key === BROADCAST ? ' → channel' : ''), d.msg.text, () => openChat(key));
    }
    renderChats(); renderThread(); renderTop(); renderNodeList(); schedulePage();
  } else if (d.type === 'radio') {
    S.radio = d.radio;
    const mode = radioMode();
    if (mode !== S.radioMode) { S.radioMode = mode; pushEvent('radio', `SDR: ${mode}`); }
    renderRadio(); renderAir(); scheduleCommand(); renderNav(); schedulePage();
  } else if (d.type === 'aircraft') {
    const known = new Set(S.air.aircraft.map(a => a.icao));
    S.air = { enabled: d.enabled, aircraft: d.aircraft || [], reference: d.reference, reference_assumed: d.reference_assumed, stats: d.stats, receiver: d.receiver };
    for (const a of S.air.aircraft) if (a.lat != null && !known.has(a.icao) && S.air.aircraft.length < 60) pushEvent('air', `${a.callsign || a.icao} · ${fmtAlt(a.alt_ft)} · ${a.dist_nm} NM`);
    airIngest(S.air.aircraft);
    syncAircraft(); renderAir(); scheduleCommand(); renderNav(); schedulePage();
  } else if (d.type === 'job') {
    const was = S.job;
    S.job = d.job;
    if (S.booted && d.job && (!was || was.enabled !== d.job.enabled) && d.job.kind) pushEvent('radio', `Sensor scan ${d.job.enabled ? `started on ${fmtJobMhz(d.job.mhz)}` : 'stopped'}`);
    renderNav(); scheduleCommand(); schedulePage();
  } else if (d.type === 'weather') {
    const before = new Set((S.weather?.data?.alerts || []).map(a => a.id));
    const first = !S.weather?.data;
    S.weather = d.weather;
    for (const a of d.weather?.data?.alerts || []) if (!before.has(a.id) && !first) {
      pushEvent('news', `Weather alert: ${a.event}`);
      toast(`⚠ ${a.event}`, a.headline || a.area, () => setPage('weather'), a.severity === 'Severe' || a.severity === 'Extreme');
    }
    renderNav(); scheduleCommand(); schedulePage();
  } else if (d.type === 'sats') {
    S.sats = d;
    schedulePage();
  } else if (d.type === 'follows') {
    const prev = new Map(S.follows.map(f => [f.id, f]));
    S.follows = d.follows || [];
    for (const f of S.follows) {
      if (!prev.has(f.id)) pushEvent('news', `Following “${f.topic}” (${f.id})`);
      else if (prev.get(f.id).last_report !== f.last_report && f.last_report) pushEvent('news', `Update recorded for “${f.topic}”`);
    }
    for (const f of prev.values()) if (!S.follows.some(x => x.id === f.id)) pushEvent('news', `Stopped following “${f.topic}”`);
    renderTop(); scheduleCommand();
  } else if (d.type === 'ack') {
    S.acks.set(d.id, { ok: d.ok, reason: d.reason });
    renderThread();
  } else if (d.type === 'error') {
    toast('Not sent', d.message, null, true);
  }
}

// ---- toasts ----------------------------------------------------------------------------------------
function toast(title, text, onclick, isError = false) {
  const t = el('div', { class: 'toast' + (isError ? ' error' : ''), role: 'status' }, el('b', { text: title }), el('span', { text }));
  if (onclick) t.addEventListener('click', () => { onclick(); t.remove(); });
  else t.addEventListener('click', () => t.remove());
  $('#toasts').prepend(t);
  while ($('#toasts').children.length > 4) $('#toasts').lastChild.remove();
  setTimeout(() => t.remove(), isError ? 9000 : 7000);
}

// ---- rendering: top bar ---------------------------------------------------------------------------
function totalUnread() { let n = 0; for (const c of conversations().values()) n += c.unread; return n; }
function renderTop() {
  const st = $('#status');
  st.textContent = S.connected ? (S.error ? 'radio offline' : S.demo ? 'demo' : 'connected') : 'reconnecting…';
  st.className = 'pill ' + (S.connected && !S.error ? 'status-on' : 'status-off');
  const me = meNode();
  const meEl = $('#me');
  meEl.replaceChildren();
  if (me) {
    meEl.append(el('b', { text: me.short }), ' ', el('span', { class: 'dim', text: S.me }));
    if (me.batt != null) meEl.append(' · ', me.batt > 100 ? '⚡ USB' : `${me.batt}%`);
  } else meEl.append('no radio');
  let live = 0, known = 0;
  for (const n of S.nodes.values()) if (n.id !== S.me) { known++; if (clsOf(n) === 'live') live++; }
  $('#counts').textContent = `${live} live · ${known} known`;
  const un = totalUnread();
  const b = $('#btn-unread');
  b.classList.toggle('hidden', !un);
  b.textContent = `✉ ${un} unread`;
  const badge = $('#chats-badge');
  badge.classList.toggle('hidden', !un);
  badge.textContent = un;
  document.title = un ? `(${un}) SignalForge` : 'SignalForge';
  const fol = $('#follows');
  fol.classList.toggle('hidden', !S.follows.length);
  fol.textContent = `◉ following ${S.follows.length}`;
  fol.title = S.follows.map(f => `${f.id}: ${f.topic} (ends in ${fmtDur(Math.max(0, f.expires - nowSec()))})`).join('\n');
  const banner = $('#banner');
  banner.classList.toggle('hidden', !S.error && !S.demo);
  banner.classList.toggle('demo-banner', S.demo && !S.error);
  banner.textContent = S.error || (S.demo ? 'Demo mode · sample nodes · messages are simulated and never transmitted.' : '');
  renderNav();
}

// ---- rendering: node list -------------------------------------------------------------------------
function visibleNodes() {
  const q = S.q.trim().toLowerCase();
  return [...S.nodes.values()].filter(n => {
    if (n.id === S.me) return false;
    const c = clsOf(n);
    if (S.filter === 'live' && c !== 'live') return false;
    if (S.filter === 'direct' && n.hops !== 0) return false;
    if (S.filter === 'mapped' && !hasPos(n)) return false;
    if (q && !(`${n.short} ${n.long} ${n.id}`.toLowerCase().includes(q))) return false;
    return true;
  }).sort((a, b) => b.heard - a.heard);
}
function renderNodeList() {
  const rows = visibleNodes();
  const convs = conversations();
  $('#nodes-count').textContent = `(${rows.length})`;
  const list = $('#node-list');
  list.replaceChildren();
  if (!rows.length) list.append(el('li', { class: 'empty' }, 'No nodes match.'));
  for (const n of rows) {
    const c = clsOf(n);
    const unread = convs.get(n.id)?.unread || 0;
    const sub = [n.long && n.long !== n.short ? n.long : null, n.near || (hasPos(n) ? null : 'no position')].filter(Boolean).join(' · ');
    const li = el('li', { class: (n.id === S.sel ? 'sel ' : '') + (c === 'old' ? 'stale' : ''), dataset: { id: n.id }, title: `${n.long || n.short} (${n.id})` },
      el('span', { class: 'dot', style: `background:${colorOf(n.id)}` }),
      el('span', { class: 'name' }, n.short, unread ? el('span', { class: 'badge', style: 'margin-left:6px', text: unread }) : null),
      el('span', { class: 'meta' }, el('span', { text: c === 'live' ? `live · ${ago(n.heard)}` : `${ago(n.heard)} ago` }),
        el('span', {}, n.hops != null ? el('span', { class: 'dim', text: `${n.hops} hop${n.hops === 1 ? '' : 's'} ` }) : null, sigBars(n.snr))),
      el('span', { class: 'sub', text: sub }));
    li.addEventListener('click', () => selectNode(n.id, { pan: true }));
    li.addEventListener('dblclick', () => openChat(n.id));
    list.append(li);
  }
}

// ---- rendering: chats list & thread ---------------------------------------------------------------
function renderChats() {
  const list = $('#chat-list');
  list.replaceChildren();
  const convs = [...conversations().values()].sort((a, b) => (a.key === BROADCAST ? -1 : b.key === BROADCAST ? 1 : b.last - a.last));
  for (const c of convs) {
    const last = c.msgs[c.msgs.length - 1];
    const preview = last ? `${last.dir === 'out' ? (last.src === 'claude' ? 'Claude: ' : last.src === 'follow' ? 'Follow: ' : 'you: ') : ''}${last.text}` : 'no messages yet';
    const li = el('li', { class: c.key === S.conv ? 'sel' : '', dataset: { conv: c.key } },
      el('span', { class: 'dot', style: `background:${c.key === BROADCAST ? 'var(--accent)' : colorOf(c.key)}` }),
      el('span', { class: 'name', text: nodeName(c.key) }),
      el('span', { class: 'meta' }, c.last ? el('span', { text: ago(c.last) }) : null, c.unread ? el('span', { class: 'badge', text: c.unread }) : null),
      el('span', { class: 'sub', text: preview }));
    li.addEventListener('click', () => openChat(c.key));
    list.append(li);
  }
}

function renderThread() {
  const head = $('#chat-head'), thread = $('#thread');
  const key = S.conv;
  head.replaceChildren(...[el('span', { text: key ? nodeName(key) : 'No conversation' }),
    key && key !== BROADCAST ? el('span', { class: 'dim', text: key }) : null,
    key === BROADCAST ? el('span', { class: 'dim', text: 'primary channel, everyone in range' }) : null].filter(Boolean)); // replaceChildren(null) would print "null"
  const stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40;
  thread.replaceChildren();
  const msgs = S.msgs.filter(m => convOf(m) === key);
  if (!msgs.length) thread.append(el('div', { class: 'empty' }, 'No messages yet.'));
  let day = '';
  for (const m of msgs) {
    const d = fmtDay(m.ts);
    if (d !== day) { day = d; thread.append(el('div', { class: 'day', text: d })); }
    const out = m.dir === 'out';
    const foot = el('span', { class: 'foot' }, el('span', { title: fmtFull(m.ts), text: fmtTime(m.ts) }));
    if (out && (m.demo || m.delivery === 'simulated')) foot.append(el('span', { text: 'simulated · not transmitted' }));
    else if (out && (!isBroadcast(m.to) || m.delivery === 'failed' || S.acks.get(m.id)?.ok === false)) {
      const a = S.acks.get(m.id);
      if (a) foot.append(a.ok ? el('span', { class: 'good', text: '✓ delivered' }) : el('span', { class: 'bad', text: `✗ ${a.reason}` }));
      else if (m.delivery === 'delivered') foot.append(el('span', { class: 'good', text: '✓ delivered' }));
      else if (m.delivery === 'failed') foot.append(el('span', { class: 'bad', text: `✗ ${m.reason || 'delivery failed'}` }));
      else if (nowSec() - m.ts < 90) foot.append(el('span', { text: '… sent' }));
    }
    const bot = m.src === 'claude' || m.src === 'follow';
    const bubble = el('div', { class: `msg ${out ? 'out' : 'in'}${bot ? ' claude' : ''}`, dataset: { id: m.id || '' } });
    if (!out && key === BROADCAST) bubble.append(el('span', { class: 'who', style: `color:${colorOf(m.from)}`, text: nodeName(m.from) }));
    if (bot) bubble.append(el('span', { class: 'who', text: m.src === 'follow' ? 'Follow mode' : 'Claude' }));
    bubble.append(el('span', { class: 'text', text: m.text }), foot);
    thread.append(bubble);
  }
  if (stick) thread.scrollTop = thread.scrollHeight;
  updateComposer();
}

function updateComposer() {
  const input = $('#msg-input'), btn = $('#btn-send');
  const ready = !!S.conv && S.connected && !S.error;
  input.disabled = !ready;
  input.placeholder = !S.conv ? 'Pick a conversation' : S.error ? 'Radio offline' : `Message ${nodeName(S.conv)}…`;
  const bytes = new TextEncoder().encode(input.value).length;
  const over = bytes > MAX_BYTES;
  const cnt = $('#byte-count');
  cnt.textContent = input.value ? `${bytes}/${MAX_BYTES} bytes` : '';
  cnt.className = over ? 'over' : 'dim';
  btn.disabled = !ready || !input.value.trim() || over;
}

// ---- rendering: detail card -----------------------------------------------------------------------
function renderDetail() {
  const box = $('#detail');
  box.replaceChildren();
  const n = S.nodes.get(S.sel);
  if (!n) { box.append(el('div', { class: 'dim', text: 'Click a node on the map or in the list for details. Double-click to message it.' })); return; }
  const c = clsOf(n);
  const pos = hasPos(n) ? [n.lat, n.lon] : null, mine = myPos();
  const kv = el('dl', { class: 'kv' });
  const row = (k, ...v) => kv.append(el('dt', { text: k }), el('dd', {}, ...v));
  row('Last heard', `${ago(n.heard)} ago`, el('span', { class: 'dim', text: n.heard ? `  (${fmtFull(n.heard)})` : '' }));
  row('Signal', n.snr == null ? '–' : `${n.snr.toFixed(1)} dB `, sigBars(n.snr), n.hops != null ? el('span', { class: 'dim', text: `  ${n.hops} hop${n.hops === 1 ? '' : 's'} away` }) : null);
  if (n.hist && n.hist.length > 1) row('SNR history', spark(n.hist));
  row('Hardware', n.hw || '–');
  if (n.batt != null) row('Power', n.batt > 100 ? '⚡ USB powered' : [`${n.batt}% `, gauge(n.batt, 'battery')], n.volt ? el('span', { class: 'dim', text: `  ${n.volt.toFixed(2)} V` }) : null);
  if (n.chutil != null) row('Channel use', `${n.chutil.toFixed(1)}%`, el('span', { class: 'dim', text: n.airtx != null ? `  · airtime ${n.airtx.toFixed(1)}%` : '' }));
  if (n.uptime) row('Uptime', fmtDur(n.uptime));
  if (pos) {
    row('Position', `${n.lat.toFixed(4)}, ${n.lon.toFixed(4)}`);
    if (n.near) row('Near', n.near);
    if (mine) row('From you', `${haversineKm(mine, pos).toFixed(1)} km`);
  } else row('Position', el('span', { class: 'dim', text: 'not reported (no GPS, or not shared)' }));
  box.append(
    el('h2', {}, el('span', { class: 'dot', style: `background:${colorOf(n.id)}` }), n.long || n.short,
      el('span', { class: `tag ${c === 'live' ? 'live' : ''}`, text: c === 'live' ? 'live' : c === 'day' ? 'today' : 'remembered' })),
    el('div', { class: 'id', text: `${n.short} · ${n.id}` }), kv,
    el('div', { class: 'actions' },
      el('button', { id: 'btn-message', class: 'primary', onclick: () => openChat(n.id), text: '✉ Message' }),
      el('button', { id: 'btn-center', onclick: () => centerOn(n.id), disabled: !pos, title: pos ? 'Centre the map on this node' : 'No position', text: '⌖ Centre' }),
      el('button', { id: 'btn-copy', onclick: () => navigator.clipboard?.writeText(n.id), text: 'Copy ID' })));
}

function renderNodesUI() {
  renderTop(); renderNodeList(); renderChats(); renderDetail(); syncMarkers();
}
function renderAll() {
  renderNodesUI(); renderThread(); renderRadio(); renderAir(); syncAircraft(); scheduleCommand(); schedulePage();
  S.booted = true;
}

// ---- rendering: radio (SDR) panel -----------------------------------------------------------------
function radioSend(cmd) { if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'radio', cmd })); }
function stopAudioElement() {
  const a = $('#radio-audio');
  a.pause();
  a.removeAttribute('src');
  a.load(); // closing the stream lets the server release the dongle
}
function listen(mhz) {
  tune(mhz);
}
function renderRadio() {
  const r = S.radio;
  const list = $('#radio-list'), status = $('#radio-status'), err = $('#radio-error'), btn = $('#btn-radio');
  list.replaceChildren();
  $('#radio-live').classList.toggle('hidden', !(r && (r.enabled || r.listening)));
  $('#btn-listen').disabled = !sdrCapability('audio').available;
  if (!r) {
    status.textContent = 'not available';
    btn.disabled = true;
    list.append(el('li', { class: 'empty' }, 'The SDR panel is not enabled on this server.'));
    return;
  }
  btn.disabled = !S.connected || (!r.enabled && !sdrCapability('survey').available);
  btn.textContent = r.enabled ? 'Stop survey' : 'Start survey';
  btn.title = r.enabled ? '' : sdrTakeoverHint('survey');
  err.classList.toggle('hidden', !r.error);
  err.textContent = r.error || '';
  status.textContent = !r.available ? 'SDR tools not installed'
    : r.listening ? `listening · ${r.listening} MHz`
      : r.enabled ? (r.updated ? `surveying · updated ${ago(r.updated)} ago` : 'surveying…') : 'idle (dongle free)';
  $('#radio-now').textContent = r.listening ? `Streaming ${r.listening} MHz. The survey is paused while you listen.` : '';
  if (!r.channels.length) {
    list.append(el('li', { class: 'empty' }, r.enabled ? 'Scanning the bands… channels appear here as they are found.' : 'Press Start survey to look for active channels.'));
  }
  for (const c of r.channels) {
    const on = r.listening != null && Math.abs(r.listening - c.mhz) < 0.0005;
    const li = el('li', { class: 'radio-row' + (on ? ' on' : ''), dataset: { mhz: c.mhz } },
      el('span', { class: 'dot', style: `background:${c.duty > 0.9 ? 'var(--dim)' : 'var(--good)'}` }),
      el('span', { class: 'name' }, `${c.mhz.toFixed(4)} MHz`, el('span', { class: 'tag', text: c.label || c.band }),
        c.digital ? el('span', { class: 'tag', title: 'Digital signal: it would sound like noise', text: 'digital' }) : null),
      el('span', { class: 'meta' },
        el('span', {}, el('span', { class: 'duty', title: `on ${Math.round(c.duty * 100)}% of the time` }, el('b', { style: `width:${Math.round(c.duty * 100)}%` })),
          el('span', { class: 'dim', text: ` ${Math.round(c.duty * 100)}%  +${c.peak_db} dB` })),
        on ? el('button', { onclick: () => { stopAudioElement(); radioSend('stop_audio'); }, text: '■ Stop' })
          : el('button', { onclick: () => listen(c.mhz), disabled: c.digital || !sdrCapability('audio').available, title: c.digital ? 'digital: not audible as analog FM' : 'Listen (narrow FM)', text: '▶ Listen' })));
    list.append(li);
  }
}

// ---- selection / navigation -----------------------------------------------------------------------
function selectNode(id, { pan = false, popup = false } = {}) {
  S.sel = id;
  renderNodeList(); renderDetail(); syncMarkers(); schedulePage();
  document.body.classList.add('show-right');
  const n = S.nodes.get(id), m = markers.get(id);
  if (pan && n && hasPos(n)) map.panTo([n.lat, n.lon]);
  if (popup && m) m.openPopup();
}
function openChat(key) {
  S.conv = key;
  markRead(key);
  if (key !== BROADCAST && S.nodes.has(key)) S.sel = key;
  if (!['dashboard', 'nodes', 'chats'].includes(S.page)) setPage('chats'); // from another page, the chat opens on the Chats page
  document.body.classList.add('show-right');
  renderTop(); renderNodeList(); renderChats(); renderDetail(); renderThread(); syncMarkers(); schedulePage();
  $('#msg-input').focus();
}
function centerOn(id) {
  if (S.page !== 'dashboard') { setPage('dashboard'); setView('map'); }
  const n = S.nodes.get(id);
  if (n && hasPos(n)) map.setView([n.lat, n.lon], Math.max(map.getZoom(), 13));
}

// ---- map -------------------------------------------------------------------------------------------
let map, nodeLayer, offlineLayer, homeMarker, airLayer, mainRadar;
const markers = new Map();

function initMap() {
  map = L.map('map', { zoomControl: true, doubleClickZoom: false, attributionControl: true, worldCopyJump: true }).setView([39, -98], 4);
  const { bases, labels } = addRadarBases(map, 'mesh.base', { offline: true });
  offlineLayer = bases['Offline vector'];
  syncMapBg(Object.keys(bases).find(k => map.hasLayer(bases[k])));
  nodeLayer = L.layerGroup().addTo(map);
  airLayer = L.layerGroup().addTo(map);
  mainRadar = makeRadar();
  mainRadar.group.addTo(map);
  L.control.layers(bases, { 'Place labels': labels, 'Radar & rings': mainRadar.group, Nodes: nodeLayer, Aircraft: airLayer }, { collapsed: true }).addTo(map);
  map.on('baselayerchange', e => syncMapBg(e.name));
  const Btns = L.Control.extend({
    onAdd() {
      const box = L.DomUtil.create('div');
      const fit = el('button', { id: 'btn-fit', class: 'map-btn', title: 'Zoom to fit the nodes', text: '⤢ Fit nodes', onclick: fitNodes });
      const home = el('button', { id: 'btn-home', class: 'map-btn', title: 'Go to your position', text: '⌂ Me', onclick: goHome });
      L.DomEvent.disableClickPropagation(box);
      box.append(fit, home);
      return box;
    },
  });
  new Btns({ position: 'topleft' }).addTo(map);
  new ResizeObserver(() => { map.invalidateSize(); mainRadar.sweep._update(); }).observe($('#map')); // the grid settles after first paint
}
function syncMapBg(name) { $('#map').classList.toggle('offline', name === 'Offline vector'); }

function makeOfflineLayer(targetMap = map) {
  const group = L.layerGroup();
  const linePane = 'basemap';
  const cityGroup = L.layerGroup();
  let data = null, loading = false;
  const styles = { coast: ['#4aa3c9', 1.2], rivers: ['#3b6fb3', 1], borders: ['#6b7686', 1], roads: ['#c9a23a', 1.2] };
  group.onAdd = function (m) {
    L.LayerGroup.prototype.onAdd.call(this, m);
    if (!m.getPane(linePane)) { m.createPane(linePane).style.zIndex = 250; }
    cityGroup.addTo(m);
    if (data) group.refreshCities();
    else if (!loading) {
      loading = true;
      fetch('/api/basemap').then(r => r.json()).then(d => {
        data = d;
        const renderer = L.canvas({ pane: linePane, padding: 0.3 });
        for (const [name, [color, weight]] of Object.entries(styles)) {
          for (const flat of d[name] || []) {
            const pts = [];
            for (let i = 0; i < flat.length; i += 2) pts.push([flat[i], flat[i + 1]]);
            L.polyline(pts, { color, weight, renderer, interactive: false, smoothFactor: 1.5 }).addTo(group);
          }
        }
        group.refreshCities();
      }).catch(() => toast('Offline map', 'Could not load /api/basemap (run tools/build_basemap.py).', null, true));
    }
  };
  group.onRemove = function (m) { cityGroup.clearLayers(); m.removeLayer(cityGroup); L.LayerGroup.prototype.onRemove.call(this, m); };
  group.refreshCities = () => {
    if (!data || !targetMap.hasLayer(group)) return;
    cityGroup.clearLayers();
    const z = targetMap.getZoom(), minPop = z <= 5 ? 1e6 : z <= 6 ? 4e5 : z <= 7 ? 2e5 : z <= 8 ? 8e4 : z <= 9 ? 4e4 : z <= 10 ? 15e3 : 5e3;
    const b = targetMap.getBounds().pad(0.05);
    let shown = 0;
    for (const [name, , lat, lon, pop] of data.places) { // sorted by population, biggest first
      if (pop < minPop) break;
      if (!b.contains([lat, lon])) continue;
      const big = pop >= 5e5;
      L.marker([lat, lon], { interactive: false, keyboard: false, icon: L.divIcon({ className: 'city' + (big ? ' big' : ''), html: el('span', { text: `${big ? '◆' : '◇'} ${name}` }), iconSize: [0, 0] }) }).addTo(cityGroup);
      if (++shown >= 70) break;
    }
  };
  return group;
}

function markerStyle(n) {
  const c = clsOf(n), color = colorOf(n.id);
  const sel = n.id === S.sel, me = n.id === S.me;
  return { radius: me ? 10 : sel ? 11 : c === 'live' ? 9 : c === 'day' ? 7 : 5, fillColor: color, fillOpacity: c === 'old' ? 0.5 : 0.95,
    color: '#fff', weight: 1.5, className: `node-marker cls-${c}${sel ? ' selected' : ''}${me ? ' me' : ''}` };
}
function popupEl(id) {
  const n = S.nodes.get(id);
  if (!n) return el('div');
  return el('div', { class: 'pop' }, el('h3', { text: n.long || n.short }),
    el('div', { class: 'dim', text: `${n.short} · ${ago(n.heard)} ago${n.near ? ' · ' + n.near : ''}` }),
    id === S.me ? el('div', { class: 'dim', text: 'This is the connected radio' }) : el('button', { text: '✉ Message', onclick: () => { map.closePopup(); openChat(id); } }));
}
function syncMarkers() {
  if (!map) return;
  const seen = new Set();
  for (const n of S.nodes.values()) {
    if (!hasPos(n)) continue;
    seen.add(n.id);
    const label = clsOf(n) !== 'old' || n.id === S.sel || n.id === S.me;
    let m = markers.get(n.id);
    if (!m) {
      m = L.circleMarker([n.lat, n.lon], markerStyle(n));
      m.on('click', () => selectNode(n.id));
      m.on('dblclick', () => openChat(n.id));
      m.bindPopup(() => popupEl(n.id));
      m.addTo(nodeLayer);
      markers.set(n.id, m);
    } else {
      m.setLatLng([n.lat, n.lon]);
      m.setStyle(markerStyle(n));
      m.setRadius(markerStyle(n).radius);
    }
    const elm = m.getElement();
    if (elm) { // Leaflet only applies className when a path is created, so keep the state classes in sync by hand
      elm.setAttribute('data-id', n.id);
      elm.classList.remove('cls-live', 'cls-day', 'cls-old');
      elm.classList.add(`cls-${clsOf(n)}`);
      elm.classList.toggle('selected', n.id === S.sel);
      elm.classList.toggle('me', n.id === S.me);
    }
    const key = `${n.short}|${label}`;
    if (m._tk !== key) {
      m._tk = key;
      m.unbindTooltip();
      m.bindTooltip(el('span', { text: n.short }), { permanent: label, direction: 'right', offset: [8, 0], className: 'node-label' });
    }
    if (n.id === S.sel) m.bringToFront();
  }
  for (const [id, m] of markers) if (!seen.has(id)) { nodeLayer.removeLayer(m); markers.delete(id); }
  const noPos = [...S.nodes.values()].filter(n => n.id !== S.me && !hasPos(n) && clsOf(n) !== 'old');
  const note = $('#mapnote');
  note.classList.toggle('hidden', !noPos.length);
  note.replaceChildren();
  if (noPos.length) {
    note.append(el('span', { class: 'dim', text: 'Heard, no position: ' }));
    noPos.slice(0, 6).forEach((n, i) => {
      note.append(i ? ', ' : '', el('a', { href: '#', dataset: { id: n.id }, style: `color:${colorOf(n.id)};font-weight:600`, text: n.short,
        onclick: e => { e.preventDefault(); selectNode(n.id); } }));
    });
    if (noPos.length > 6) note.append(` +${noPos.length - 6} more`);
  }
  syncMeshRadar(mainRadar);
  if (S.home) {
    if (!homeMarker) { homeMarker = L.circleMarker(S.home, { radius: 6, className: 'home-marker' }).bindTooltip('Home'); homeMarker.addTo(map); }
    else homeMarker.setLatLng(S.home);
  }
}
function fitNodes() {
  const pts = [...S.nodes.values()].filter(hasPos);
  const live = pts.filter(n => clsOf(n) !== 'old');
  const use = live.length >= 2 ? live : pts;
  if (!use.length) { const h = myPos(); if (h) map.setView(h, 11); return; }
  if (use === pts && pts.length > 10) { // ignore far-flung outliers: fit the central 90%
    const lats = pts.map(n => n.lat).sort((a, b) => a - b), lons = pts.map(n => n.lon).sort((a, b) => a - b);
    const lo = Math.floor(pts.length * 0.05), hi = Math.ceil(pts.length * 0.95) - 1;
    map.fitBounds([[lats[lo], lons[lo]], [lats[hi], lons[hi]]], { padding: [30, 30] });
  } else map.fitBounds(use.map(n => [n.lat, n.lon]), { padding: [40, 40], maxZoom: 14 });
}
function goHome() { const h = myPos(); if (h) map.setView(h, Math.max(map.getZoom(), 12)); else toast('No position', 'The radio has no GPS fix and no --home was given.', null, true); }
let viewSet = false;
function firstMapView() {
  if (viewSet || !map) return;
  viewSet = true;
  const h = myPos();
  if (h) map.setView(h, 11); else fitNodes();
}

// ---- wiring ----------------------------------------------------------------------------------------
function setTab(tab) {
  S.tab = tab;
  for (const b of document.querySelectorAll('.tabs button')) b.classList.toggle('active', b.dataset.tab === tab);
  $('#pane-nodes').classList.toggle('hidden', tab !== 'nodes');
  $('#pane-chats').classList.toggle('hidden', tab !== 'chats');
  $('#pane-radio').classList.toggle('hidden', tab !== 'radio');
  $('#pane-air').classList.toggle('hidden', tab !== 'air');
}
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  $('#btn-theme').textContent = t === 'dark' ? '☾' : '☀';
  try { localStorage.setItem('mesh.theme', t); } catch { /* ignore */ }
}
function sendCurrent() {
  const input = $('#msg-input');
  const text = input.value.trim();
  if (!text || !S.conv || ws?.readyState !== 1) return;
  ws.send(JSON.stringify({ type: 'send', to: S.conv, text }));
  input.value = '';
  updateComposer();
}


// ---- activity feed ---------------------------------------------------------------------------------
function pushEvent(kind, text) {
  S.events.unshift({ ts: nowSec(), kind, text });
  if (S.events.length > 120) S.events.length = 120;
  scheduleCommand();
}
function radioMode() {
  const r = S.radio;
  if (!r) return 'unavailable';
  return r.listening ? `listening ${r.listening} MHz` : r.job ? 'reading sensors' : S.air.enabled ? 'tracking aircraft' : r.enabled ? 'surveying' : 'idle';
}

// ---- aircraft --------------------------------------------------------------------------------------
const planes = new Map();
function syncAircraft() { syncPlanes({ layer: map && airLayer, planes, onclick: selectAircraft }); } // marker code is in page-air.js
function selectAircraft(icao) {
  S.selAir = icao;
  setTab('air');
  renderAir();
  const a = S.air.aircraft.find(x => x.icao === icao);
  if (a && a.lat != null && map) map.panTo([a.lat, a.lon]);
}
function renderAir() {
  const A = S.air, r = S.radio;
  const list = $('#air-list');
  list.replaceChildren();
  const n = A.aircraft.length, withPos = A.aircraft.filter(a => a.lat != null).length;
  $('#air-count').classList.toggle('hidden', !A.enabled);
  $('#air-count').textContent = n;
  const btn = $('#btn-air');
  btn.disabled = !S.connected || (!A.enabled && !sdrCapability('aircraft').available);
  btn.textContent = A.enabled ? 'Stop tracking' : 'Start tracking';
  btn.title = A.enabled ? '' : sdrTakeoverHint('aircraft');
  const err = $('#air-error');
  err.classList.toggle('hidden', !(r && r.error));
  err.textContent = (r && r.error) || '';
  $('#air-status').textContent = !r ? 'SDR not available' : !r.available ? 'SDR tools not installed'
    : A.enabled ? `tracking · ${n} aircraft (${withPos} on the map)` : 'idle (dongle free)';
  const ref = A.reference ? `${A.reference[0].toFixed(3)}, ${A.reference[1].toFixed(3)}` : '';
  $('#air-ref').textContent = A.reference ? `Distances from ${A.reference_assumed ? 'downtown Austin (assumed; start the server with --home LAT,LON)' : 'your position'} · ${ref}` : '';
  if (!n) list.append(el('li', { class: 'empty' }, A.enabled ? 'Listening for aircraft on 1090 MHz… planes appear within seconds.' : 'Press Start tracking to see aircraft near you. This uses the SDR dongle.'));
  for (const a of A.aircraft) {
    const color = altColor(a.alt_ft);
    const sub = [a.gs_kt != null ? `${a.gs_kt} kt` : null, a.track != null ? `hdg ${Math.round(a.track)}°` : null, a.vrate != null ? `${trend(a.vrate)} ${Math.abs(a.vrate)} fpm` : null].filter(Boolean).join(' · ');
    const li = el('li', { class: 'air-row' + (a.icao === S.selAir ? ' sel' : '') + (a.lat == null ? ' stale' : ''), dataset: { icao: a.icao } },
      el('span', { class: 'glyph', style: `color:${color}`, text: '✈' }),
      el('span', { class: 'name' }, acName(a), el('span', { class: 'tag alt', style: `background:${color}`, text: fmtAlt(a.alt_ft) })),
      el('span', { class: 'meta' }, el('span', { text: a.dist_nm != null ? `${a.dist_nm} NM ${a.bearing}°` : 'no position yet' }), el('span', { class: 'dim', text: `${Math.round(a.age_s)}s ago` })),
      el('span', { class: 'sub', text: sub || a.icao }));
    li.addEventListener('click', () => { S.selAir = a.icao; renderAir(); if (a.lat != null && map) { setView('map'); map.panTo([a.lat, a.lon]); } });
    list.append(li);
  }
}

// ---- command center -------------------------------------------------------------------------------
let cmdQueued = false;
function scheduleCommand() {
  if (S.view !== 'command' || cmdQueued) return;
  cmdQueued = true;
  requestAnimationFrame(() => { cmdQueued = false; renderCommand(); });
}
function setView(v) {
  S.view = v;
  document.body.classList.toggle('view-command', v === 'command');
  $('#view-cmd').classList.toggle('active', v === 'command');
  $('#view-map').classList.toggle('active', v === 'map');
  try { localStorage.setItem('mesh.view', v); } catch { /* ignore */ }
  if (v === 'command') scheduleCommand();
  else if (map) setTimeout(() => map.invalidateSize(), 60);
  radarLockSoon();
}
function stat(cls, big, small) {
  return el('div', { class: `stat ${cls}` }, el('i', { class: 'pulse' }), el('div', {}, el('b', { text: big }), el('span', { text: ` ${small}` })));
}
const CMDCARD = {};
function ccard(title, body, { wide = false, action = null, id = null } = {}) {
  return el('section', { class: 'ccard' + (wide ? ' wide' : ''), id },
    el('h3', {}, el('span', { text: title }), action), body);
}
function renderCommand() {
  const box = $('#command');
  if (!box || S.view !== 'command') return;
  const keep = box.scrollTop;
  const now = new Date();
  const me = meNode();
  const convs = conversations();
  const unread = totalUnread();
  const liveNodes = [...S.nodes.values()].filter(n => n.id !== S.me && clsOf(n) === 'live');
  const known = S.nodes.size ? S.nodes.size - (me ? 1 : 0) : 0;
  const r = S.radio, A = S.air;

  const strip = el('div', { class: 'cmd-strip' },
    el('div', { class: 'cmd-clock' }, now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      el('small', { text: `${now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })} · ${now.toISOString().slice(11, 16)} UTC` })),
    stat(S.connected && !S.error ? 'ok' : 'bad', me ? me.short : 'no radio', S.connected ? (S.error ? 'radio offline' : 'mesh radio') : 'reconnecting'),
    stat(liveNodes.length ? 'ok' : 'warn', String(liveNodes.length), `live · ${known} known`),
    stat(unread ? 'warn' : 'ok', String(unread), 'unread'),
    stat(S.follows.length ? 'ok' : 'warn', String(S.follows.length), 'news watches'),
    stat(!r ? 'bad' : (r.enabled || A.enabled || r.listening) ? 'ok' : 'warn', r ? radioMode() : 'no SDR', 'SDR'),
    stat(A.enabled ? 'ok' : 'warn', String(A.aircraft.length), 'aircraft'));

  // news watch
  const news = el('div', {});
  if (!S.follows.length) news.append(el('div', { class: 'empty-state', text: 'Nothing is being followed. Text “follow <topic>” from your handheld and it appears here with the latest verified report.' }));
  for (const f of S.follows) {
    news.append(el('div', { class: 'news' },
      el('div', { class: 'h' }, el('b', { text: `${f.id} · ${f.topic}` }), el('span', { text: f.last_check ? `checked ${ago(f.last_check)} ago · ends in ${fmtDur(Math.max(0, f.expires - nowSec()))}` : `ends in ${fmtDur(Math.max(0, f.expires - nowSec()))}` })),
      el('div', { text: f.last_report || 'No report yet: the first check is pending.' })));
  }

  // messages
  const msgs = el('div', {});
  const recent = [...S.msgs].slice(-40).reverse();
  const seenConv = new Set();
  for (const m of recent) {
    const k = convOf(m);
    if (seenConv.has(k)) continue;
    seenConv.add(k);
    const u = convs.get(k)?.unread || 0;
    msgs.append(el('div', { class: 'crow', onclick: () => openChat(k) },
      el('b', { text: nodeName(k) }), el('span', { class: 'x', text: `${m.dir === 'out' ? (m.src === 'claude' ? 'Claude: ' : m.src === 'follow' ? 'Follow: ' : 'you: ') : ''}${m.text}` }),
      el('span', { class: 't' }, u ? el('span', { class: 'badge', text: u }) : null, ` ${ago(m.ts)}`)));
    if (seenConv.size >= 6) break;
  }
  if (!seenConv.size) msgs.append(el('div', { class: 'empty-state', text: 'No messages yet.' }));

  // mesh
  const mesh = el('div', {}, el('div', { class: 'kpis' },
    el('div', {}, el('div', { class: 'big', text: String(liveNodes.length) }), el('span', { text: 'live now' })),
    el('div', {}, el('div', { class: 'big', text: String(known) }), el('span', { text: 'remembered' })),
    el('div', {}, el('div', { class: 'big', text: me && me.batt != null ? (me.batt > 100 ? 'USB' : `${me.batt}%`) : '–' }), el('span', { text: 'this radio' }))));
  for (const n of liveNodes.sort((a, b) => b.heard - a.heard).slice(0, 5)) {
    mesh.append(el('div', { class: 'crow', onclick: () => { setView('map'); selectNode(n.id, { pan: true }); } },
      el('b', { style: `color:${colorOf(n.id)}`, text: n.short }), el('span', { class: 'x', text: n.near || n.long || n.id }),
      el('span', { class: 't' }, sigBars(n.snr), ` ${ago(n.heard)}`)));
  }
  if (!liveNodes.length) mesh.append(el('div', { class: 'empty-state', text: 'No other nodes heard in the last two hours.' }));

  // airspace: the shared satellite radar map (built once, never re-inserted) plus a list refreshed each tick
  const at = el('div', { class: 'cmd-air-list' });
  for (const a of A.aircraft.filter(x => x.dist_nm != null).slice(0, 6)) {
    at.append(el('div', { class: 'crow', onclick: () => selectAircraft(a.icao) },
      el('b', { style: `color:${altColor(a.alt_ft)}`, text: acName(a) }), el('span', { class: 'x', text: `${fmtAlt(a.alt_ft)}${a.gs_kt != null ? ` · ${a.gs_kt} kt` : ''}` }),
      el('span', { class: 't', text: `${a.dist_nm} NM ${a.bearing}°` })));
  }
  if (!A.aircraft.length) at.append(el('div', { class: 'empty-state', text: A.enabled ? 'Listening on 1090 MHz…' : 'Aircraft tracking is off. Start it to see planes overhead on the radar map.' }));

  // radio
  const radio = el('div', {});
  if (!r) radio.append(el('div', { class: 'empty-state', text: 'The SDR panel is not enabled on this server.' }));
  else {
    radio.append(el('div', { class: 'kpis' }, el('div', {}, el('div', { class: 'big', text: String(r.channels.length) }), el('span', { text: 'active channels' })),
      el('div', {}, el('div', { class: 'big', style: 'font-size:18px', text: radioMode() }), el('span', { text: 'dongle' }))));
    if (r.error) radio.append(el('div', { class: 'radio-error', text: r.error }));
    for (const c of r.channels.slice(0, 5)) {
      radio.append(el('div', { class: 'crow', onclick: () => { if (!c.digital) listen(c.mhz); } },
        el('b', { text: `${c.mhz.toFixed(3)}` }), el('span', { class: 'x', text: `${c.label || c.band}${c.digital ? ' · digital' : ''}` }), el('span', { class: 't', text: `${Math.round(c.duty * 100)}% · ${c.c_peak || c.peak_db} dB` })));
    }
    if (!r.channels.length) radio.append(el('div', { class: 'empty-state', text: 'No survey results yet. Start a survey to find active channels.' }));
  }

  // sensors: environment sensors heard by the current / last sensor scan
  const sj = S.job?.kind === 'sensors' ? S.job : null, sensing = !!sj?.enabled;
  const sdevs = sj?.devices || [];
  const tempF = v => { const t = v.readings.temperature_C ?? v.readings.temperature_1_C; return t ? t.value * 9 / 5 + 32 : null; };
  const temps = sdevs.map(tempF).filter(t => t != null);
  const sens = el('div', {}, el('div', { class: 'kpis' },
    el('div', {}, el('div', { class: 'big', text: String(sdevs.length) }), el('span', { text: sdevs.length === 1 ? 'sensor heard' : 'sensors heard' })),
    el('div', {}, el('div', { class: 'big', style: 'font-size:24px', text: temps.length ? (Math.round(Math.min(...temps)) === Math.round(Math.max(...temps)) ? `${Math.round(temps[0])}°F` : `${Math.round(Math.min(...temps))}–${Math.round(Math.max(...temps))}°F`) : '–' }), el('span', { text: 'outdoor temps' }))));
  for (const v of sdevs.slice(0, 4)) {
    const f = tempF(v);
    sens.append(el('div', { class: 'crow', onclick: () => setPage('sensors') }, el('b', { text: v.model }),
      el('span', { class: 'x', text: f != null ? `${Math.round(f)}°F${v.readings.humidity ? ` · ${v.readings.humidity.value}%` : ''}` : `${v.count} reports` }),
      el('span', { class: 't', text: `${ago(v.last_seen)} ago` })));
  }
  if (!sdevs.length) sens.append(el('div', { class: 'empty-state', text: !sdrCapability('sensors').available ? sdrCapability('sensors').error : sensing ? `Scanning ${fmtJobMhz(sj.mhz)}… sensors report every 30–90 seconds.` : 'Sensor scan is off. Start it to find weather stations and thermometers near you.' }));
  if (sj?.ignored) sens.append(el('div', { class: 'dim', style: 'font-size:12px', text: `${sj.ignored} other transmissions ignored (cars, remotes, alarms: counted, never listed).` }));

  // weather
  const wx = el('div', {});
  const wd = S.weather?.data;
  if (!wd) wx.append(el('div', { class: 'empty-state', text: serviceAvailable('weather') === false ? S.capabilityErrors.weather || 'Weather is not enabled on this server.' : S.weather?.error ? 'Weather service unavailable.' : 'Loading the forecast…' }));
  else {
    const o = wd.obs, p0 = wd.forecast?.[0], alerts = wd.alerts || [], staleAlerts = wd.status?.alerts && wd.status.alerts !== 'current';
    wx.append(el('div', { class: 'kpis' }, el('div', {}, el('div', { class: 'big', style: `color:${tempColor(o?.temp_f ?? p0?.temp)}`, text: (o?.temp_f ?? p0?.temp) == null ? '–' : `${Math.round(o?.temp_f ?? p0.temp)}°` }), el('span', { text: o?.text || p0?.short || '' })),
      el('div', {}, el('div', { class: 'big', style: `font-size:24px;${alerts.length ? 'color:var(--bad)' : ''}`, text: staleAlerts ? '–' : String(alerts.length) }), el('span', { text: staleAlerts ? 'alerts unavailable' : alerts.length === 1 ? 'alert' : 'alerts' }))));
    if (S.weather?.error) wx.append(el('div', { class: 'note', text: S.weather.error }));
    for (const a of alerts.slice(0, 2)) wx.append(el('div', { class: 'crow', onclick: () => setPage('weather') }, el('b', { style: 'color:var(--bad)', text: a.event }), el('span', { class: 'x', text: a.headline }), el('span', { class: 't', text: a.severity })));
    if (p0) wx.append(el('div', { class: 'dim', style: 'font-size:12.5px', text: `${p0.name}: ${p0.short}${p0.pop ? `, ${p0.pop}% rain` : ''}. ${wd.place}` }));
  }

  // activity feed
  const feed = el('div', { class: 'feed' });
  for (const e of S.events.slice(0, 60)) feed.append(el('div', {}, el('time', { text: new Date(e.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }), el('span', { class: `k-${e.kind}`, text: e.text })));
  if (!S.events.length) feed.append(el('div', { class: 'empty-state', text: 'Live events appear here as they happen: messages, nodes, aircraft, the SDR and news updates.' }));

  const openTab = t => () => setPage(t);
  // The two map cards are built once and never re-inserted: moving them would restart the radar animations
  // and interrupt a drag. Every other card is swapped in place each tick.
  CMDCARD.meshmap ||= ccard('Mesh radar', cmdMapHost('mesh'), { id: 'card-meshmap', action: el('button', { onclick: openTab('nodes'), text: 'open map' }) });
  if (!CMDCARD.air) {
    const btn = el('button', { id: 'cmd-air-btn', onclick: () => radioSend(S.air.enabled ? 'aircraft_stop' : 'aircraft_start') });
    const body = el('div', { class: 'cmd-air-body' });
    CMDCARD.air = ccard('Airspace', el('div', {}, cmdMapHost('air'), body), { id: 'card-air',
      action: el('span', { class: 'row-flex' }, el('button', { onclick: openTab('air'), text: 'open map' }), btn) });
    CMDCARD.air.btn = btn; CMDCARD.air.body = body;
  }
  const airBtn = CMDCARD.air.btn;
  airBtn.textContent = A.enabled ? 'stop tracking' : 'start tracking';
  airBtn.disabled = !S.connected || (!A.enabled && !sdrCapability('aircraft').available);
  if (A.enabled) airBtn.removeAttribute('title'); else { const hint = sdrTakeoverHint('aircraft'); if (hint) airBtn.title = hint; else airBtn.removeAttribute('title'); }
  CMDCARD.air.body.replaceChildren(at);
  const cards = [
    ccard('Live news watch', news, { wide: true, id: 'card-news' }),
    ccard('Messages', msgs, { id: 'card-msgs', action: el('button', { onclick: openTab('chats'), text: 'open chats' }) }),
    ccard('Mesh', mesh, { id: 'card-mesh', action: el('button', { onclick: openTab('nodes'), text: 'all nodes' }) }),
    CMDCARD.meshmap,
    CMDCARD.air,
    ccard('Radio (SDR)', radio, { id: 'card-radio', action: el('button', { id: 'cmd-radio-btn', title: r?.enabled ? null : sdrTakeoverHint('survey') || null, onclick: () => radioSend(r && r.enabled ? 'stop' : 'start'), disabled: !S.connected || (!r?.enabled && !sdrCapability('survey').available), text: r && r.enabled ? 'stop survey' : 'start survey' }) }),
    ccard('Sensors', sens, { id: 'card-sensors', action: el('button', { id: 'cmd-sensors-btn', title: sensing ? null : sdrTakeoverHint('sensors') || null,
      onclick: () => { if (sensing) radioSend('job_stop'); else if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'radio', cmd: 'job_start', kind: 'sensors', mhz: AUTO_MHZ })); },
      disabled: !S.connected || (!sensing && !sdrCapability('sensors').available), text: sensing ? 'stop scan' : 'start scan' }) }),
    ccard('Weather', wx, { id: 'card-wx', action: el('button', { onclick: () => setPage('weather'), text: 'forecast & radar' }) }),
    ccard('Live activity', feed, { id: 'card-feed' })];
  const grid = box.querySelector(':scope > .cmd-grid');
  if (grid && grid.children.length === cards.length && cards.every((c, i) => grid.children[i].id === c.id)) {
    box.querySelector(':scope > .cmd-strip').replaceWith(strip);
    cards.forEach((c, i) => { if (grid.children[i] !== c) grid.children[i].replaceWith(c); });
  } else box.replaceChildren(strip, el('div', { class: 'cmd-grid' }, ...cards));
  box.scrollTop = keep;
  syncCmdMaps();
}

function init() {
  let theme = null;
  try { theme = localStorage.getItem('mesh.theme'); } catch { /* ignore */ }
  setTheme(theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
  $('#btn-theme').addEventListener('click', () => { setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'); });
  for (const b of document.querySelectorAll('.tabs button')) b.addEventListener('click', () => setTab(b.dataset.tab));
  for (const c of document.querySelectorAll('.chip')) c.addEventListener('click', () => {
    S.filter = c.dataset.filter;
    for (const x of document.querySelectorAll('.chip')) x.classList.toggle('active', x === c);
    renderNodeList();
  });
  $('#btn-air').addEventListener('click', () => radioSend(S.air.enabled ? 'aircraft_stop' : 'aircraft_start'));
  $('#view-cmd').addEventListener('click', () => setView('command'));
  $('#view-map').addEventListener('click', () => setView('map'));
  $('#btn-radio').addEventListener('click', () => radioSend(S.radio?.enabled ? 'stop' : 'start'));
  $('#radio-form').addEventListener('submit', e => { e.preventDefault(); listen($('#radio-mhz').value.replace(',', '.')); });
  $('#radio-audio').addEventListener('ended', () => radioSend('stop_audio'));
  $('#search').addEventListener('input', e => { S.q = e.target.value; renderNodeList(); });
  $('#composer').addEventListener('submit', e => { e.preventDefault(); sendCurrent(); });
  $('#msg-input').addEventListener('input', updateComposer);
  $('#msg-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendCurrent(); } });
  $('#btn-unread').addEventListener('click', () => {
    const c = [...conversations().values()].filter(x => x.unread).sort((a, b) => b.last - a.last)[0];
    if (c) { setTab('chats'); openChat(c.key); }
  });
  document.addEventListener('visibilitychange', () => { if (viewingChat() && S.conv) { markRead(S.conv); renderTop(); renderChats(); renderNodeList(); } });
  initMap();
  buildNav();
  window.addEventListener('hashchange', () => setPage(pageFromHash()));
  window.addEventListener('popstate', () => setPage(pageFromHash()));
  let savedView = null;
  try { savedView = localStorage.getItem('mesh.view'); } catch { /* ignore */ }
  setView(savedView === 'map' ? 'map' : 'command');
  setInterval(scheduleCommand, 1000); // the clock and the "x seconds ago" texts
  setInterval(() => { renderTop(); renderNodeList(); renderDetail(); syncMarkers(); }, 15000); // ages and live/remembered colours drift
  renderAll();
  setPage(pageFromHash());
  setInterval(() => PAGES[S.page]?.tick?.(), 10000);
  connect();
}
init();
