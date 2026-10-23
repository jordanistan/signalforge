'use strict';
/* SignalForge radar overlay, shared by every map: satellite/street bases, range rings, a rotating sweep that is
   phase-locked to "ping" blips at each target, and the mesh / aircraft ring models. Loads after pages.js and
   before the page scripts; it only touches app.js globals (S, el, myPos ...) at run time. */

const KM_PER_MI = 1.609344, RADAR_PERIOD = 6; // seconds per sweep revolution
const RADAR_SCALE_CAP_KM = 300; // LoRa links beyond this are exceptional: they never set the ring scale
const SAT_TILES = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
const SAT_LABELS = 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}';
const ESRI_ATTR = 'Imagery © Esri, Maxar, Earthstar Geographics';

function bearingDeg(a, b) {
  const r = Math.PI / 180, dl = (b[1] - a[1]) * r;
  const y = Math.sin(dl) * Math.cos(b[0] * r);
  const x = Math.cos(a[0] * r) * Math.sin(b[0] * r) - Math.sin(a[0] * r) * Math.cos(b[0] * r) * Math.cos(dl);
  return (Math.atan2(y, x) / r + 360) % 360;
}
const fmtKm = km => (km < 10 ? km.toFixed(1) : km.toFixed(0));
const kmMi = km => `${fmtKm(km)} km · ${fmtKm(km / KM_PER_MI)} mi`;
function ringStep(maxKm) {
  for (const s of [0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500]) if (s * 4 >= maxKm) return s;
  return 1000;
}
// Every sweep and blip is phase-locked to the document timeline, so a node flashes exactly when the beam crosses
// it. CSS restarts an animation whenever its element is re-inserted or un-hidden (page switches, layer toggles,
// display:none), so radarLock() pins each animation's start to time 0 again: the phase then depends only on
// performance.now(). A blip's delay is the time the beam takes to reach its bearing.
const blipDelay = bearing => (bearing / 360 * RADAR_PERIOD).toFixed(3);
function radarLock() {
  for (const e of document.querySelectorAll('.nm-sweep, .nm-blip i, .radar .sweep')) {
    for (const a of e.getAnimations()) if (a.startTime !== 0) a.startTime = 0;
  }
}
let radarLockQueued = false;
function radarLockSoon() { // after the browser has created the animations for newly inserted elements
  if (radarLockQueued) return;
  radarLockQueued = true;
  requestAnimationFrame(() => { radarLockQueued = false; radarLock(); });
}
document.addEventListener('visibilitychange', radarLockSoon);
setInterval(radarLock, 1000); // catches anything un-hidden by a path that did not call radarLockSoon()

// A rotating conic gradient sized to the outermost ring. It lives in its own pane under the markers and is
// hidden during zoom animations, then re-placed (panning moves the whole pane, so it needs no update).
const RadarSweep = L.Layer.extend({
  onAdd(m) {
    this._el = el('div', { class: 'nm-radar hidden' }, el('div', { class: 'nm-sweep' }));
    if (!m.getPane('nmRadar')) { m.createPane('nmRadar').style.zIndex = 350; m.getPane('nmRadar').style.pointerEvents = 'none'; }
    m.getPane('nmRadar').append(this._el);
    m.on('zoomstart', this._hide, this).on('zoomend viewreset resize', this._update, this);
    this._update();
  },
  onRemove(m) {
    this._el.remove();
    m.off('zoomstart', this._hide, this).off('zoomend viewreset resize', this._update, this);
  },
  set(center, km) { this._c = center; this._km = km; if (this._map) this._update(); },
  _hide() { this._el?.classList.add('hidden'); },
  _update() {
    this.onUpdate?.();
    const m = this._map;
    if (!m || !this._c || !this._km) { this._hide(); return; }
    const p = m.latLngToLayerPoint(this._c), top = m.latLngToLayerPoint([this._c[0] + this._km / 111.32, this._c[1]]);
    const r = Math.max(1, p.y - top.y);
    Object.assign(this._el.style, { left: `${p.x - r}px`, top: `${p.y - r}px`, width: `${2 * r}px`, height: `${2 * r}px` });
    this._el.classList.toggle('wide', this._km > 500); // continent-sized (satellites): a fainter beam
    this._el.classList.remove('hidden');
    radarLockSoon();
  },
});

/* Satellite (default) + Streets (+ optional offline vector) bases, a place-label overlay and the radar pane,
   shared by the Nodes and Air maps. The chosen base is remembered per map under `key`. */
function addRadarBases(m, key, { offline = false } = {}) {
  const bases = {
    Satellite: L.tileLayer(SAT_TILES, { maxZoom: 19, maxNativeZoom: 18, attribution: ESRI_ATTR }),
    Streets: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', OSM_OPTS),
  };
  if (offline) bases['Offline vector'] = makeOfflineLayer(m);
  let start = 'Satellite';
  try { const saved = localStorage.getItem(key); if (bases[saved]) start = saved; } catch { /* ignore */ }
  bases[start].addTo(m);
  // Place names for the imagery; street and offline maps carry their own, so the overlay follows the base.
  const labels = L.tileLayer(SAT_LABELS, { maxZoom: 19, maxNativeZoom: 18, pane: 'overlayPane' });
  if (start === 'Satellite') labels.addTo(m);
  m.on('baselayerchange', e => {
    try { localStorage.setItem(key, e.name); } catch { /* ignore */ }
    if (e.name === 'Satellite') labels.addTo(m); else m.removeLayer(labels);
  });
  if (offline) m.on('zoomend moveend', () => bases['Offline vector'].refreshCities?.());
  m.on('overlayadd', radarLockSoon);
  return { bases, labels };
}
// Radar "pings": a ring that flashes at each point as the sweep beam crosses its bearing from `center`.
function radarBlips(layer, center, pts) {
  layer.clearLayers();
  for (const p of pts) {
    L.marker(p, { interactive: false, keyboard: false,
      icon: L.divIcon({ className: 'nm-blip', html: el('i', { style: `animation-delay:${blipDelay(bearingDeg(center, p))}s` }), iconSize: [0, 0] }) }).addTo(layer);
  }
  radarLockSoon();
}


// ---- mesh radar: rings sized to the nodes heard today, plus the observed direct reach ----------------
function meshRadarModel() {
  const me = myPos();
  if (!me) return null;
  const others = [...S.nodes.values()].filter(n => n.id !== S.me && hasPos(n));
  const recent = others.filter(n => clsOf(n) !== 'old');
  const direct = recent.filter(n => n.hops === 0).map(n => ({ n, d: haversineKm(me, [n.lat, n.lon]) }));
  const reach = direct.length ? Math.max(...direct.map(x => x.d)) : null;
  // Scale to the bulk of the nodes: one far-off (or bogus) position must not shrink everyone else to a dot.
  // Nodes beyond RADAR_SCALE_CAP_KM or 3x the 90th-percentile distance still show, just outside the rings.
  const ds = (recent.length ? recent : others).map(n => haversineKm(me, [n.lat, n.lon])).sort((a, b) => a - b);
  const p90 = ds.length ? ds[Math.floor(0.9 * (ds.length - 1))] : 0;
  const keep = ds.filter(d => d <= RADAR_SCALE_CAP_KM && d <= 3 * p90);
  const scaleKm = keep.length ? keep[keep.length - 1] : ds.length ? ds[0] : 0;
  const maxKm = Math.max(reach != null && reach <= RADAR_SCALE_CAP_KM ? reach : 0, scaleKm, 1);
  const step = ringStep(maxKm), count = Math.max(3, Math.ceil(maxKm / step)), outer = step * count;
  const inside = recent.map(n => [n.lat, n.lon]).filter(p => haversineKm(me, p) <= outer);
  const sig = [me.join(), step, count, reach, ...recent.map(n => `${n.id}:${n.lat}:${n.lon}:${n.hops}:${n.snr}`)].join('|');
  return { me, recent, direct, reach, step, count, outer, inside, sig };
}
function makeRadar() {
  const r = { rings: L.layerGroup(), blips: L.layerGroup(), sweep: new RadarSweep(), sig: '', labels: [] };
  r.group = L.layerGroup([r.rings, r.blips, r.sweep]);
  r.sweep.onUpdate = () => thinRingLabels(r);
  return r;
}
function clearRings(r) { r.rings.clearLayers(); r.labels = []; }
// A distance label on a ring of `km` around `center`; `below` puts it under the centre (the reach label).
function ringLabel(r, center, km, text, below = false) {
  const mk = L.marker([center[0] + (below ? -km : km) / 111.32, center[1]], { interactive: false, keyboard: false,
    icon: L.divIcon({ className: 'nm-ring-label' + (below ? ' reach' : ''), html: el('span', { text }), iconSize: [0, 0] }) }).addTo(r.rings);
  r.labels.push({ mk, km, center, below });
}
// Labels stack on the north side of their rings, so small or tightly spaced rings would print on top of each
// other: keep the outermost, then only rings at least RING_LABEL_GAP px further in, and none under RING_LABEL_MIN px.
const RING_LABEL_MIN = 40, RING_LABEL_GAP = 28;
function thinRingLabels(r) {
  const m = r.sweep._map;
  if (!m || !r.labels.length) return;
  // project() is unrounded: layer points are whole pixels, which would make 1 km read as 0 px when zoomed out
  const c = r.labels[0].center, z = m.getZoom(), pxPerKm = (m.project(c, z).y - m.project([c[0] + 100 / 111.32, c[1]], z).y) / 100;
  let last = Infinity;
  for (const x of [...r.labels].sort((a, b) => b.km - a.km)) {
    const px = x.km * pxPerKm;
    const show = x.below ? px >= RING_LABEL_GAP : px >= RING_LABEL_MIN && last - px >= RING_LABEL_GAP;
    if (show && !x.below) last = px;
    x.mk.setOpacity(show ? 1 : 0);
  }
}
// Draw the mesh rings/sweep/blips into radar `r`; returns the model (null without a position for you).
function syncMeshRadar(r, { labels = true } = {}) {
  const M = meshRadarModel();
  const sig = M ? M.sig : 'none';
  if (sig === r.sig) return M;
  r.sig = sig;
  clearRings(r); r.blips.clearLayers();
  if (!M) { r.sweep.set(null, 0); return M; }
  const { me, step, count, reach } = M;
  for (let i = 1; i <= count; i++) {
    const km = step * i;
    L.circle(me, { radius: km * 1000, interactive: false, className: 'nm-ring', fill: false }).addTo(r.rings);
    if (labels) ringLabel(r, me, km, kmMi(km));
  }
  if (reach) {
    L.circle(me, { radius: reach * 1000, interactive: false, className: 'nm-reach' }).addTo(r.rings);
    if (labels) ringLabel(r, me, reach, `direct reach ${kmMi(reach)}`, true);
  }
  if (!S.nodes.get(S.me) || !hasPos(S.nodes.get(S.me))) {
    const you = L.circleMarker(me, { radius: 7, interactive: false, className: 'home-marker' });
    if (labels) you.bindTooltip('You', { permanent: true, direction: 'right', offset: [8, 0], className: 'node-label' });
    you.addTo(r.rings);
  }
  radarBlips(r.blips, me, M.inside);
  r.sweep.set(me, M.outer);
  thinRingLabels(r);
  return M;
}

// ---- aircraft radar: fixed nautical-mile rings around the ADS-B reference -----------------------------
const AIR_RINGS_NM = [25, 50, 100, 150];
const airRef = () => S.air.reference || myPos(); // the ADS-B reference, else your --home / GPS position
function syncAirRadar(r, { labels = true } = {}) {
  const ref = airRef();
  const pts = ref ? S.air.aircraft.filter(a => a.lat != null && a.dist_nm != null && a.dist_nm <= AIR_RINGS_NM[AIR_RINGS_NM.length - 1]) : [];
  const sig = ref ? [ref.join(), ...pts.map(a => `${a.icao}:${a.lat.toFixed(2)}:${a.lon.toFixed(2)}`)].join('|') : 'none';
  if (sig === r.sig) return;
  const ringKey = ref ? ref.join() : '';
  if (r.ringKey !== ringKey) {
    r.ringKey = ringKey;
    clearRings(r);
    if (ref) {
      for (const nm of AIR_RINGS_NM) {
        L.circle(ref, { radius: nm * 1852, fill: false, interactive: false, className: 'nm-ring' }).addTo(r.rings);
        if (labels) ringLabel(r, ref, nm * 1.852, `${nm} NM · ${Math.round(nm * 1.15078)} mi`);
      }
      L.circleMarker(ref, { radius: 6, className: 'home-marker', interactive: false }).addTo(r.rings);
    }
    r.sweep.set(ref, ref ? AIR_RINGS_NM[AIR_RINGS_NM.length - 1] * 1.852 : 0);
    thinRingLabels(r);
  }
  r.sig = sig;
  if (ref) radarBlips(r.blips, ref, pts.map(a => [a.lat, a.lon])); else r.blips.clearLayers();
}

// ---- Command Center map widgets ---------------------------------------------------------------------
// Each widget's map element is created once and moved into every re-rendered card, so Leaflet keeps its state.
const CMDMAP = {};
function cmdMapHost(kind) {
  if (!CMDMAP[kind]) CMDMAP[kind] = { host: el('div', { class: 'cmd-map', id: `cmd-map-${kind}` }) };
  return CMDMAP[kind].host;
}
function cmdMapBuild(w, key) {
  const m = w.map = L.map(w.host, { zoomControl: false, doubleClickZoom: false, worldCopyJump: true, attributionControl: true }).setView(myPos() || [39, -98], 9);
  addRadarBases(m, key);
  w.radar = makeRadar();
  w.radar.group.addTo(m);
  w.layer = L.layerGroup().addTo(m);
  new ResizeObserver(() => { m.invalidateSize(); w.radar.sweep._update(); }).observe(w.host);
}
function syncCmdMaps() {
  const mesh = CMDMAP.mesh, air = CMDMAP.air;
  if (mesh?.host.isConnected && mesh.host.clientWidth) {
    if (!mesh.map) { cmdMapBuild(mesh, 'mesh.cmd.mesh.base'); mesh.markers = new Map(); }
    const M = syncMeshRadar(mesh.radar, { labels: false });
    const seen = new Set();
    for (const n of S.nodes.values()) {
      if (!hasPos(n) || (clsOf(n) === 'old' && n.id !== S.me)) continue;
      seen.add(n.id);
      const st = { ...markerStyle(n), radius: n.id === S.me ? 7 : 5 };
      let mk = mesh.markers.get(n.id);
      if (!mk) {
        mk = L.circleMarker([n.lat, n.lon], st).addTo(mesh.layer);
        mk.bindTooltip(el('span', { text: n.short }), { direction: 'right', offset: [6, 0], className: 'node-label' });
        mk.on('click', () => { setPage('nodes'); selectNode(n.id); });
        mesh.markers.set(n.id, mk);
      } else { mk.setLatLng([n.lat, n.lon]); mk.setStyle(st); }
    }
    for (const [id, mk] of mesh.markers) if (!seen.has(id)) { mesh.layer.removeLayer(mk); mesh.markers.delete(id); }
    if (M && mesh.fitKey !== M.outer) { mesh.fitKey = M.outer; mesh.map.fitBounds(L.latLng(M.me).toBounds(M.outer * 2000), { padding: [6, 6] }); }
  }
  if (air?.host.isConnected && air.host.clientWidth) {
    if (!air.map) { cmdMapBuild(air, 'mesh.cmd.air.base'); air.planes = new Map(); }
    syncAirRadar(air.radar, { labels: false });
    syncPlanes({ layer: air.layer, planes: air.planes, labels: false, onclick: icao => { S.selAir = icao; setPage('air'); } });
    const ref = airRef();
    if (ref && air.fitKey !== ref.join()) { air.fitKey = ref.join(); air.map.fitBounds(L.latLng(ref).toBounds(AIR_RINGS_NM[AIR_RINGS_NM.length - 1] * 1852 * 2), { padding: [6, 6] }); }
  }
}
