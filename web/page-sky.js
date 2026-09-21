'use strict';
/* Satellites page: where the ISS and other satellites are now, a sky plot of what is overhead and the next passes.
   Positions are calculated on the server from public orbital data (no SDR needed); the SDR can listen to a downlink. */

const SK = { timer: 0, sel: null, map: null, markers: new Map(), tracks: null, foot: null, home: null };
const compass8 = az => (az == null ? '' : ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(az / 45) % 8]);
const fmtPassTime = ts => new Date(ts * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
const shortSat = n => n.replace(/ \(.*\)$/, '').replace(/ & .*/, '');

function skyRequest() { if (S.page === 'sky' && serviceAvailable('sats') !== false && ws?.readyState === 1) ws.send(JSON.stringify({ type: 'sats' })); }
function splitTrack(track) { // break a ground track where it wraps around the date line
  const segs = [[]];
  for (const p of track) {
    const last = segs[segs.length - 1].at(-1);
    if (last && Math.abs(p[1] - last[1]) > 180) segs.push([]);
    segs[segs.length - 1].push(p);
  }
  return segs;
}
function skyPlot(sats) {
  const size = 300, c = size / 2, R = 128;
  const svg = svgEl('svg', { viewBox: `0 0 ${size} ${size}`, class: 'skyplot', role: 'img', 'aria-label': 'Sky plot of satellites above the horizon' });
  for (const el_ of [0, 30, 60]) svgEl('circle', { cx: c, cy: c, r: R * (90 - el_) / 90, class: 'ring' }, svg);
  svgEl('line', { x1: c, y1: c - R, x2: c, y2: c + R, class: 'ring' }, svg);
  svgEl('line', { x1: c - R, y1: c, x2: c + R, y2: c, class: 'ring' }, svg);
  for (const [t, x, y] of [['N', c, c - R - 4], ['S', c, c + R + 11], ['E', c + R + 8, c + 3], ['W', c - R - 8, c + 3]]) svgEl('text', { x, y, 'text-anchor': 'middle' }, svg).textContent = t;
  for (const s of sats.filter(x => x.above)) {
    const r = R * (90 - s.el) / 90, th = (s.az * Math.PI) / 180;
    const x = c + r * Math.sin(th), y = c - r * Math.cos(th);
    svgEl('circle', { cx: x, cy: y, r: 6, fill: s.name === SK.sel ? '#ffd60a' : '#3fd17d', stroke: '#000', 'stroke-width': .8 }, svg);
    const t = svgEl('text', { x: x + 9, y: y + 3, fill: 'var(--text)', style: 'fill:var(--text);font-size:10px' }, svg);
    t.textContent = shortSat(s.name);
  }
  if (!sats.some(x => x.above)) svgEl('text', { x: c, y: c + 3, 'text-anchor': 'middle' }, svg).textContent = 'nothing above the horizon';
  return svg;
}

const skyPage = {
  build(sec) {
    const sh = pageShell('Satellites', el('span', { id: 'sk-loc', class: 'dim' }), el('span', { class: 'grow' }), el('span', { id: 'sk-age', class: 'dim' }),
      el('button', { class: 'btn small', id: 'sk-refresh', text: '↻ Refresh', onclick: skyRequest }));
    const mapEl = el('div', { class: 'dmap', id: 'sky-map' });
    const left = el('div', { class: 'pmap' }, mapEl);
    const right = el('div', { class: 'right' }, el('div', { id: 'sk-err' }),
      box('Overhead now', el('div', { id: 'sk-plot' })),
      box('Next passes (elevation ≥ 10°)', el('div', { class: 'scroll', style: 'max-height:340px' }, el('table', { class: 'tbl', id: 'sk-passes' }, el('tbody', { id: 'sk-pbody' })))),
      box('Satellites', el('div', { id: 'sk-list' })),
      el('div', { class: 'note' }, 'Calculated from public orbital data (CelesTrak), refreshed every 12 hours. Nothing is transmitted. The ISS voice/picture downlink (145.800 MHz) can be heard with the SDR during a pass; weather-satellite pictures need more software than is installed here.'));
    const wrap = el('div', { class: 'sky-page' }, left, right);
    sh.root.replaceChild(wrap, sh.body);
    sec.append(sh.root);
    skyPage.mapEl = mapEl;
  },
  enter() {
    if (!SK.map) {
      SK.map = makeMap(skyPage.mapEl, { center: [20, 0], zoom: 2, offline: true });
      SK.tracks = L.layerGroup().addTo(SK.map);
      SK.sats = L.layerGroup().addTo(SK.map);
    }
    setTimeout(() => SK.map.invalidateSize(), 60);
    skyRequest();
    clearInterval(SK.timer);
    SK.timer = setInterval(skyRequest, 5000);
  },
  leave() { clearInterval(SK.timer); SK.timer = 0; },
  render() {
    const D = S.sats;
    const unavailable = serviceAvailable('sats') === false;
    $('#sk-refresh').disabled = !S.connected || unavailable;
    if (!D || unavailable) {
      const msg = unavailable ? S.capabilityErrors.sats || 'Satellite tracking is not enabled on this server.' : !S.connected ? 'Waiting for the server connection…' : 'Loading orbital data… (needs the internet the first time)';
      $('#sk-list').replaceChildren(el('div', { class: 'empty-state', text: msg }));
      return;
    }
    $('#sk-err').replaceChildren(...(D.error ? [el('div', { class: 'err-note', role: 'alert', text: D.error })] : []));
    $('#sk-loc').textContent = D.location ? `Passes for ${D.location[0].toFixed(3)}, ${D.location[1].toFixed(3)}${S.location?.source === 'assumed' ? ' · assumed location' : ''}` : '';
    $('#sk-age').textContent = D.tle_age_h != null ? `orbital data ${D.tle_age_h < 1 ? 'fresh' : `${D.tle_age_h} h old`}${D.tle_stale ? ' · stale' : ''}` : '';
    const sats = D.sats || [];
    if (!sats.some(s => s.name === SK.sel)) SK.sel = (sats.find(s => s.name.startsWith('ISS')) || sats[0])?.name || null;
    // map
    SK.tracks.clearLayers();
    const seen = new Set();
    for (const s of sats) {
      seen.add(s.name);
      const isSel = s.name === SK.sel;
      if (isSel || s.above) for (const seg of splitTrack(s.track || [])) if (seg.length > 1) L.polyline(seg, { color: isSel ? '#ffd60a' : '#7aa2f7', weight: isSel ? 2 : 1, opacity: .7, dashArray: '4 5', interactive: false }).addTo(SK.tracks);
      if (isSel) {
        const rEarth = 6371, ang = Math.acos(rEarth / (rEarth + s.alt_km));
        L.circle([s.lat, s.lon], { radius: rEarth * ang * 1000, color: '#ffd60a', weight: 1, fillOpacity: .06, interactive: false }).addTo(SK.tracks);
      }
      let m = SK.markers.get(s.name);
      const icon = L.divIcon({ className: 'plane', html: el('span', { style: `font-size:22px;filter:${s.above ? 'drop-shadow(0 0 6px #3fd17d)' : 'none'}`, text: s.name.startsWith('ISS') || s.name.startsWith('CSS') ? '🛰' : '📡' }), iconSize: [24, 24] });
      if (!m) { m = L.marker([s.lat, s.lon], { icon, keyboard: false }).addTo(SK.sats); m.on('click', () => { SK.sel = s.name; skyPage.render(); }); SK.markers.set(s.name, m); }
      else { m.setLatLng([s.lat, s.lon]); m.setIcon(icon); }
      m.unbindTooltip();
      m.bindTooltip(el('span', { text: `${shortSat(s.name)} · ${s.alt_km} km${s.above ? ' · overhead' : ''}` }), { permanent: true, direction: 'right', offset: [12, 0], className: 'sat-label' });
    }
    for (const [n, m] of SK.markers) if (!seen.has(n)) { SK.sats.removeLayer(m); SK.markers.delete(n); }
    if (D.location) {
      if (!SK.home) SK.home = L.circleMarker(D.location, { radius: 6, className: 'home-marker' }).bindTooltip('You').addTo(SK.map);
      else SK.home.setLatLng(D.location);
    } else if (SK.home) { SK.map.removeLayer(SK.home); SK.home = null; }
    // sky plot
    $('#sk-plot').replaceChildren(skyPlot(sats));
    // passes
    const now = D.now || nowSec();
    const body = $('#sk-pbody');
    body.replaceChildren(el('tr', {}, ...['Satellite', 'Starts', 'In', 'Max', 'Path', ''].map(t => el('th', { text: t, style: 'cursor:default' }))));
    if (!(D.passes || []).length) body.append(el('tr', {}, el('td', { colspan: 6, class: 'empty', text: D.error ? '' : 'No passes in the next 24 hours.' })));
    for (const p of D.passes || []) {
      const live = p.rise <= now && p.set > now;
      body.append(el('tr', { class: 'row' + (p.name === SK.sel ? ' sel' : ''), onclick: () => { SK.sel = p.name; skyPage.render(); } },
        el('td', {}, el('b', { text: shortSat(p.name) })), el('td', { text: fmtPassTime(p.rise) }),
        el('td', { class: 'num' }, live ? el('span', { class: 'badge live', text: 'NOW' }) : fmtDur(p.rise - now)),
        el('td', {}, el('span', { class: 'elbar' }, el('b', { style: `width:${Math.min(100, (p.max_el / 90) * 100)}%` })), ` ${p.max_el}°`),
        el('td', { text: `${compass8(p.rise_az)} → ${compass8(p.set_az)}` }),
        el('td', {}, p.downlink ? el('button', { class: 'btn small', disabled: !sdrCapability('audio').available, title: sdrCapability('audio').available ? `${p.downlink.what}: ${p.downlink.mhz} MHz` : sdrCapability('audio').error, onclick: e => { e.stopPropagation(); setPage('radio'); tune(p.downlink.mhz); }, text: '▶ Listen' }) : null)));
    }
    // list
    const list = $('#sk-list');
    list.replaceChildren();
    if (!sats.length) list.append(el('div', { class: 'empty-state', text: D.error ? 'No orbital positions are available.' : 'No satellites in the current orbital data.' }));
    for (const s of sats) {
      list.append(el('div', { class: 'crow', onclick: () => { SK.sel = s.name; skyPage.render(); if (SK.map) SK.map.panTo([s.lat, s.lon]); } },
        el('b', { style: s.name === SK.sel ? 'color:#ffd60a' : '', text: shortSat(s.name) }),
        el('span', { class: 'x', text: `${s.alt_km} km up · ${s.above ? `overhead: el ${s.el}° az ${s.az}° (${compass8(s.az)})` : `${Math.round(s.range_km).toLocaleString()} km away`}${s.downlink ? ` · ${s.downlink.mhz} MHz ${s.downlink.what}` : ''}` }),
        el('span', { class: 't', text: s.above ? '● overhead' : '' })));
    }
  },
};
registerPage('sky', skyPage);
