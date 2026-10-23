'use strict';
/* Air page: a large live map of aircraft heard on 1090 MHz (ADS-B), with trails, range rings and a details panel.
   Also home of the plane-marker code that the Dashboard map shares. */

const ALT_COLORS = [[5000, '#ffb454'], [20000, '#9ece6a'], [35000, '#7aa2f7'], [Infinity, '#bb9af7']];
const altColor = ft => (ft == null ? '#8b98a5' : ALT_COLORS.find(([lim]) => ft < lim)[1]);
const fmtAlt = ft => (ft == null ? '–' : ft >= 18000 ? `FL${Math.round(ft / 100)}` : `${ft.toLocaleString()} ft`);
const acName = a => a.callsign || a.icao;
const trend = v => (v == null ? '' : v > 300 ? '↑' : v < -300 ? '↓' : '→');
const SVGNS = 'http://www.w3.org/2000/svg';

function planeSvg(track, color) {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '26'); svg.setAttribute('height', '26');
  svg.style.transform = `rotate(${Number(track) || 0}deg)`;
  const p = document.createElementNS(SVGNS, 'path');
  p.setAttribute('d', 'M12 2 L14 9 L22 13 L22 15 L14 13 L14 19 L17 21 L17 22 L12 21 L7 22 L7 21 L10 19 L10 13 L2 15 L2 13 L10 9 Z');
  p.setAttribute('fill', color); p.setAttribute('stroke', '#000'); p.setAttribute('stroke-width', '0.8');
  svg.append(p);
  return svg;
}

/* Keep a set of Leaflet plane markers in step with the aircraft list. `t` = { layer, planes: Map, labels?: bool,
   onclick(icao) }. Used by the Dashboard map and the Air page map. */
function syncPlanes(t) {
  if (!t.layer) return;
  const seen = new Set();
  for (const a of S.air.aircraft) {
    if (a.lat == null || a.lon == null) continue;
    seen.add(a.icao);
    const color = altColor(a.alt_ft);
    let m = t.planes.get(a.icao);
    if (!m) {
      const svg = planeSvg(a.track, color);
      m = L.marker([a.lat, a.lon], { icon: L.divIcon({ className: 'plane', html: svg, iconSize: [26, 26] }), zIndexOffset: 500, keyboard: false });
      m._svg = svg;
      m.on('click', () => t.onclick(a.icao));
      m.addTo(t.layer);
      t.planes.set(a.icao, m);
    } else {
      m.setLatLng([a.lat, a.lon]);
      m._svg.style.transform = `rotate(${Number(a.track) || 0}deg)`;
      m._svg.firstChild.setAttribute('fill', color);
    }
    const elm = m.getElement();
    elm?.setAttribute('data-icao', a.icao);
    elm?.classList.toggle('sel', a.icao === S.selAir);
    const showLabel = t.labels !== false;
    const tip = `${acName(a)} · ${fmtAlt(a.alt_ft)}${a.gs_kt != null ? ` · ${a.gs_kt} kt` : ''}`;
    if (m._tip !== tip || m._lab !== showLabel) {
      m._tip = tip; m._lab = showLabel;
      m.unbindTooltip();
      m.bindTooltip(el('span', { text: tip }), { permanent: showLabel, direction: 'right', offset: [12, 0], className: 'plane-label' });
    }
  }
  for (const [id, m] of t.planes) if (!seen.has(id)) { t.layer.removeLayer(m); t.planes.delete(id); }
}

// client-side history per aircraft: trail of positions and altitude samples
const acHist = new Map();
function airIngest(list) {
  const now = nowSec();
  const live = new Set();
  for (const a of list) {
    live.add(a.icao);
    let h = acHist.get(a.icao);
    if (!h) { h = { trail: [], alt: [], gs: [], first: now }; acHist.set(a.icao, h); }
    if (a.lat != null) {
      const last = h.trail[h.trail.length - 1];
      if (!last || last[0] !== a.lat || last[1] !== a.lon) { h.trail.push([a.lat, a.lon]); if (h.trail.length > 60) h.trail.shift(); }
    }
    if (a.alt_ft != null) { h.alt.push(a.alt_ft); if (h.alt.length > 90) h.alt.shift(); }
    if (a.gs_kt != null) { h.gs.push(a.gs_kt); if (h.gs.length > 90) h.gs.shift(); }
  }
  for (const k of acHist.keys()) if (!live.has(k)) acHist.delete(k);
}

const AP = { sort: 'dist', dir: 1, q: '', posOnly: false, trails: true, labels: true, follow: false, ringsFor: null };
const AIR_COLS = [
  ['name', 'Flight', a => acName(a).toLowerCase()], ['alt', 'Altitude', a => a.alt_ft], ['gs', 'Speed', a => a.gs_kt],
  ['dist', 'Distance', a => a.dist_nm], ['brg', 'Bearing', a => a.bearing], ['vr', 'Climb', a => a.vrate], ['age', 'Heard', a => a.age_s],
];
const airPage = {
  planes: new Map(), trails: new Map(),
  build(sec) {
    const toggle = el('button', { id: 'ap-toggle', class: 'btn primary', onclick: () => radioSend(S.air.enabled ? 'aircraft_stop' : 'aircraft_start') });
    airPage.dongle = dongleChip();
    const sh = pageShell('Air traffic', toggle, airPage.dongle, el('span', { id: 'ap-status', class: 'dim' }), el('span', { class: 'grow' }),
      el('label', { class: 'dim' }, el('input', { type: 'checkbox', id: 'ap-trails', checked: true, onchange: e => { AP.trails = e.target.checked; airPage.render(); } }), ' trails '),
      el('label', { class: 'dim' }, el('input', { type: 'checkbox', id: 'ap-labels', checked: true, onchange: e => { AP.labels = e.target.checked; airPage.render(); } }), ' labels '),
      el('label', { class: 'dim' }, el('input', { type: 'checkbox', id: 'ap-follow', onchange: e => { AP.follow = e.target.checked; airPage.render(); } }), ' follow selected '),
      el('button', { class: 'btn small', id: 'ap-fit', onclick: () => airPage.fit(), text: '⤢ Fit' }));
    const mapEl = el('div', { class: 'dmap', id: 'air-map' });
    const legend = el('div', { class: 'legend' }, ...[['< 5,000 ft', ALT_COLORS[0][1]], ['< 20,000', ALT_COLORS[1][1]], ['< 35,000', ALT_COLORS[2][1]], ['higher', ALT_COLORS[3][1]]]
      .map(([t, c]) => el('span', {}, el('i', { style: `background:${c}` }), t)));
    const left = el('div', { class: 'left' }, el('div', { class: 'pmap' }, mapEl, legend), el('div', { class: 'box', id: 'ap-kpis' }));
    const right = el('div', { class: 'right' },
      el('div', { id: 'ap-err' }),
      box('Selected aircraft', el('div', { id: 'ap-detail', class: 'ac-detail' })),
      box('All aircraft', el('div', {}, el('div', { class: 'row-flex', style: 'margin-bottom:8px' },
        el('input', { class: 'field', type: 'search', placeholder: 'Filter flight / ICAO', id: 'ap-q', style: 'flex:1', oninput: e => { AP.q = e.target.value; airPage.render(); } }),
        el('label', { class: 'dim' }, el('input', { type: 'checkbox', id: 'ap-pos', onchange: e => { AP.posOnly = e.target.checked; airPage.render(); } }), ' on map only')),
      el('div', { class: 'scroll', style: 'max-height:340px' }, el('table', { class: 'tbl', id: 'ap-table' }, el('thead', {}, el('tr', {}, ...AIR_COLS.map(([k, label]) => el('th', { dataset: { k }, class: k === 'name' ? '' : 'num', onclick: () => {
        if (AP.sort === k) AP.dir *= -1; else { AP.sort = k; AP.dir = 1; }
        airPage.render();
      } }, label)))), el('tbody', { id: 'ap-body' })))), {}));
    const wrap = el('div', { class: 'air-page' }, left, right);
    sh.root.replaceChild(wrap, sh.body);
    sec.append(sh.root);
    airPage.mapEl = mapEl;
  },
  enter() {
    if (!airPage.map) {
      const h = S.air.reference || myPos() || [39, -98];
      const m = airPage.map = L.map(airPage.mapEl, { zoomControl: true, doubleClickZoom: false, worldCopyJump: true }).setView(h, 8);
      const { bases, labels } = addRadarBases(m, 'mesh.air.base', { offline: true });
      airPage.layer = L.layerGroup().addTo(m);
      airPage.trailLayer = L.layerGroup().addTo(m);
      airPage.radar = makeRadar();
      airPage.sweep = airPage.radar.sweep;
      const radar = airPage.radar.group.addTo(m);
      L.control.layers(bases, { 'Place labels': labels, 'Radar & rings': radar, Aircraft: airPage.layer, Trails: airPage.trailLayer }, { collapsed: true }).addTo(m);
      new ResizeObserver(() => { m.invalidateSize(); airPage.sweep._update(); }).observe(airPage.mapEl);
      airPage.home = null;
    }
    setTimeout(() => { airPage.map.invalidateSize(); airPage.drawRings(); }, 60);
  },
  drawRings() { if (airPage.radar) syncAirRadar(airPage.radar); },
  fit() {
    const pts = S.air.aircraft.filter(a => a.lat != null).map(a => [a.lat, a.lon]);
    if (S.air.reference) pts.push(S.air.reference);
    if (pts.length > 1) airPage.map.fitBounds(pts, { padding: [40, 40], maxZoom: 11 });
    else if (S.air.reference) airPage.map.setView(S.air.reference, 9);
  },
  select(icao) { S.selAir = icao; airPage.render(); const a = S.air.aircraft.find(x => x.icao === icao); if (a?.lat != null) airPage.map.panTo([a.lat, a.lon]); },
  render() {
    const A = S.air, r = S.radio;
    airPage.dongle.update();
    const btn = $('#ap-toggle');
    const cap = sdrCapability('aircraft');
    btn.disabled = !S.connected || (!A.enabled && !cap.available);
    btn.title = cap.available ? (A.enabled ? '' : sdrTakeoverHint('aircraft')) : cap.error;
    btn.textContent = A.enabled ? '■ Stop tracking' : '▶ Start tracking';
    btn.classList.toggle('danger', A.enabled); btn.classList.toggle('primary', !A.enabled);
    const n = A.aircraft.length, withPos = A.aircraft.filter(a => a.lat != null).length;
    const rx = A.receiver ? ` · via ${A.receiver}` : ''; // which decoder program (rtl_adsb / readsb / dump1090)
    $('#ap-status').textContent = !S.connected ? 'server disconnected' : A.enabled ? `${n} aircraft · ${withPos} on the map${rx}` : cap.available ? `idle${rx}` : cap.error;
    const err = $('#ap-err');
    err.replaceChildren(...(r && r.error && !A.enabled ? [el('div', { class: 'err-note', role: 'alert', text: r.error })] : []));
    airPage.drawRings();
    // map
    syncPlanes({ layer: airPage.layer, planes: airPage.planes, labels: AP.labels, onclick: i => airPage.select(i) });
    airPage.trailLayer.clearLayers();
    if (AP.trails) {
      for (const a of A.aircraft) {
        const h = acHist.get(a.icao);
        if (h && h.trail.length > 1) L.polyline(h.trail, { color: altColor(a.alt_ft), weight: a.icao === S.selAir ? 3 : 1.6, opacity: a.icao === S.selAir ? .95 : .55, className: 'trail', interactive: false }).addTo(airPage.trailLayer);
      }
    }
    const sel = A.aircraft.find(a => a.icao === S.selAir);
    if (AP.follow && sel?.lat != null) airPage.map.panTo([sel.lat, sel.lon], { animate: true });
    // KPIs
    const pos = A.aircraft.filter(a => a.dist_nm != null);
    const closest = pos[0], high = [...A.aircraft].filter(a => a.alt_ft != null).sort((a, b) => b.alt_ft - a.alt_ft)[0];
    const fast = [...A.aircraft].filter(a => a.gs_kt != null).sort((a, b) => b.gs_kt - a.gs_kt)[0];
    const far = [...pos].sort((a, b) => b.dist_nm - a.dist_nm)[0];
    const k = (big, small) => el('div', {}, el('div', { class: 'big', style: 'font-size:22px', text: big }), el('span', { text: small }));
    $('#ap-kpis').replaceChildren(el('div', { class: 'kpi-row' },
      k(String(n), 'aircraft now'), k(closest ? `${acName(closest)} · ${closest.dist_nm} NM` : '–', 'closest'),
      k(high ? `${acName(high)} · ${fmtAlt(high.alt_ft)}` : '–', 'highest'), k(fast ? `${acName(fast)} · ${fast.gs_kt} kt` : '–', 'fastest'),
      k(far ? `${far.dist_nm} NM ${far.bearing}°` : '–', 'farthest'), k(A.stats ? `${A.stats.accepted}/${A.stats.frames}` : '–', 'valid frames'),
    ), ...((A.stats?.frames ?? 0) > 50 && A.stats?.accepted === 0
      ? [el('div', { class: 'noise-hint', id: 'ap-noise', role: 'status', text: 'Hearing only noise so far — no valid aircraft messages. Put the antenna near a window or outside; 1090 MHz needs a short antenna (about 6.9 cm per element).' })]
      : []),
    el('div', { class: 'dim', style: 'margin-top:8px;font-size:12px', text: A.reference ? `Distances from ${A.reference_assumed ? 'downtown Austin (assumed: start the server with --home LAT,LON)' : 'your position'} · ${A.reference[0].toFixed(3)}, ${A.reference[1].toFixed(3)}` : '' }));
    // detail
    const d = $('#ap-detail');
    d.replaceChildren();
    if (!sel) d.append(el('div', { class: 'empty-state', text: A.enabled ? 'Click a plane on the map or a row in the table.' : 'Start tracking, then click a plane.' }));
    else {
      const h = acHist.get(sel.icao);
      const kv = el('dl', { class: 'kv' });
      const row = (kk, ...v) => kv.append(el('dt', { text: kk }), el('dd', {}, ...v));
      row('Altitude', `${fmtAlt(sel.alt_ft)} `, el('span', { class: 'dim', text: sel.alt_ft != null ? `(${Math.round(sel.alt_ft * 0.3048).toLocaleString()} m)` : '' }));
      row('Speed', sel.gs_kt != null ? `${sel.gs_kt} kt · ${Math.round(sel.gs_kt * 1.15078)} mph` : '–');
      row('Heading', sel.track != null ? `${Math.round(sel.track)}°` : '–');
      row('Climb', sel.vrate != null ? `${trend(sel.vrate)} ${Math.abs(sel.vrate)} ft/min` : '–');
      row('From you', sel.dist_nm != null ? `${sel.dist_nm} NM (${(sel.dist_nm * 1.852).toFixed(1)} km) · bearing ${sel.bearing}°` : 'no position yet');
      row('Position', sel.lat != null ? `${sel.lat.toFixed(4)}, ${sel.lon.toFixed(4)}` : '–');
      row('ICAO address', sel.icao);
      row('Messages', String(sel.msgs), el('span', { class: 'dim', text: `  last ${Math.round(sel.age_s)}s ago` }));
      if (h && h.alt.length > 2) row('Altitude history', spark(h.alt));
      if (h && h.gs.length > 2) row('Speed history', spark(h.gs));
      d.append(el('h2', {}, acName(sel), el('span', { class: 'tag alt', style: `background:${altColor(sel.alt_ft)}`, text: fmtAlt(sel.alt_ft) })), kv,
        el('div', { class: 'actions' }, el('button', { class: 'btn small', disabled: sel.lat == null, onclick: () => sel.lat != null && airPage.map.setView([sel.lat, sel.lon], Math.max(airPage.map.getZoom(), 11)), text: '⌖ Centre' }),
          el('button', { class: 'btn small', onclick: () => { S.selAir = null; airPage.render(); }, text: 'Clear' })));
    }
    // table
    const q = AP.q.trim().toLowerCase();
    let rows = A.aircraft.filter(a => (!AP.posOnly || a.lat != null) && (!q || `${a.callsign || ''} ${a.icao}`.toLowerCase().includes(q)));
    rows = sortBy(rows, AP.sort, AP.dir, Object.fromEntries(AIR_COLS.map(([kk, , f]) => [kk, f])));
    for (const th of document.querySelectorAll('#ap-table th')) { th.classList.toggle('sorted', th.dataset.k === AP.sort); th.classList.toggle('asc', th.dataset.k === AP.sort && AP.dir === 1); }
    const body = $('#ap-body');
    body.replaceChildren();
    if (!rows.length) body.append(el('tr', {}, el('td', { colspan: AIR_COLS.length, class: 'empty', text: A.enabled ? 'Listening on 1090 MHz…' : 'Nothing yet.' })));
    for (const a of rows) {
      const tr = el('tr', { class: 'row' + (a.icao === S.selAir ? ' sel' : ''), dataset: { icao: a.icao } },
        el('td', {}, el('b', { style: `color:${altColor(a.alt_ft)}`, text: acName(a) })), el('td', { class: 'num', text: fmtAlt(a.alt_ft) }),
        el('td', { class: 'num', text: a.gs_kt != null ? `${a.gs_kt} kt` : '–' }), el('td', { class: 'num', text: a.dist_nm != null ? `${a.dist_nm} NM` : '–' }),
        el('td', { class: 'num', text: a.bearing != null ? `${a.bearing}°` : '–' }), el('td', { class: 'num', text: a.vrate != null ? `${trend(a.vrate)} ${Math.abs(a.vrate)}` : '–' }),
        el('td', { class: 'num', text: `${Math.round(a.age_s)}s` }));
      tr.addEventListener('click', () => airPage.select(a.icao));
      body.append(tr);
    }
  },
};
registerPage('air', airPage);
