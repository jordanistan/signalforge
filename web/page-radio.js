'use strict';
/* Radio page: survey results as spectrum charts, a tuner with a live audio scope, presets and bookmarks.
   Receive only. The dongle does one job at a time (survey, listen, aircraft, pagers, sensors). */

const RADIO_PRESETS = [
  ['NOAA Weather', 162.4], ['NOAA WX 2', 162.55], ['2 m calling', 146.52], ['146.94 repeater', 146.94], ['MURS 1', 151.82], ['MURS 3', 151.94],
  ['FRS/GMRS 1', 462.5625], ['FRS/GMRS 3', 462.6125], ['70 cm calling', 446.0], ['ISS voice', 145.8],
];
const RP = { band: 'all', hideDigital: false, sort: 'duty', dir: -1, squelch: 0, audioCtx: null, analyser: null, raf: 0, bookmarks: [] };
try { RP.bookmarks = JSON.parse(localStorage.getItem('mesh.bookmarks') || '[]').filter(b => b && isFinite(b.mhz)).slice(0, 40); } catch { /* ignore */ }
const saveBookmarks = () => { try { localStorage.setItem('mesh.bookmarks', JSON.stringify(RP.bookmarks)); } catch { /* ignore */ } };

function tune(mhz) {
  const v = frequencyValue(mhz), cap = sdrCapability('audio');
  if (!cap.available) { toast('Radio unavailable', cap.error, null, true); return; }
  if (v == null) { toast('Frequency', 'Enter a frequency from 24 to 1766 MHz.', null, true); return; }
  if ($('#rp-mhz')) $('#rp-mhz').value = v;
  radioPage.ensureAnalyser();
  const a = $('#radio-audio');
  a.src = `/api/radio/audio?mhz=${encodeURIComponent(v)}&squelch=${RP.squelch}&t=${Date.now()}`;
  a.play().catch(() => { /* press play */ });
  radioPage.startScope();
}
function stopTune() { stopAudioElement(); radioSend('stop_audio'); }

const radioPage = {
  build(sec) {
    const toggle = el('button', { id: 'rp-toggle', class: 'btn primary', onclick: () => radioSend(S.radio?.enabled ? 'stop' : 'start') });
    radioPage.dongle = dongleChip();
    const sh = pageShell('Radio', toggle, radioPage.dongle, el('span', { id: 'rp-status', class: 'dim' }));
    const scope = el('canvas', { class: 'scope', id: 'rp-scope', width: 640, height: 180, 'aria-label': 'Audio spectrum' });
    const mhz = el('input', { class: 'field', id: 'rp-mhz', type: 'text', inputmode: 'decimal', placeholder: 'MHz e.g. 146.520', style: 'flex:1', autocomplete: 'off' });
    const form = el('form', { class: 'row-flex', onsubmit: e => { e.preventDefault(); tune($('#rp-mhz').value.replace(',', '.')); } }, mhz, el('button', { class: 'btn primary', id: 'rp-listen', type: 'submit', text: '▶ Listen' }),
      el('button', { class: 'btn', type: 'button', id: 'rp-stop', onclick: stopTune, text: '■ Stop' }));
    const presets = el('div', { class: 'presets', id: 'rp-presets' }, ...RADIO_PRESETS.map(([n, f]) => el('button', { class: 'chip', title: `${f} MHz`, onclick: () => { $('#rp-mhz').value = f; tune(f); }, text: n })));
    const sq = el('input', { type: 'range', min: 0, max: 300, step: 10, value: 0, id: 'rp-sq', oninput: e => { RP.squelch = +e.target.value; $('#rp-sqv').textContent = RP.squelch ? String(RP.squelch) : 'off'; } });
    const vol = el('input', { type: 'range', min: 0, max: 100, step: 5, value: 100, id: 'rp-vol', oninput: e => { $('#radio-audio').volume = e.target.value / 100; } });
    const player = box('Tuner', el('div', { class: 'stack' },
      el('div', { class: 'now-playing' }, el('div', { class: 'freq-big', id: 'rp-freq', text: '— — —' }), el('div', { class: 'dim', id: 'rp-now' })),
      scope, form, presets,
      el('div', { class: 'row-flex' }, el('label', { class: 'dim' }, 'Squelch ', sq, ' ', el('span', { id: 'rp-sqv', text: 'off' })), el('label', { class: 'dim' }, 'Volume ', vol)),
      el('div', { class: 'note' }, el('b', {}, 'Receive only. '), 'Analog FM only (narrow). Digital voice and encrypted channels sound like noise and are not decoded here.')));
    const bm = box('Bookmarks', el('div', { class: 'stack' }, el('div', { class: 'row-flex' },
      el('input', { class: 'field', id: 'rp-bmname', placeholder: 'Name for the current frequency', style: 'flex:1', maxlength: 32 }),
      el('button', { class: 'btn small', onclick: () => radioPage.addBookmark(), text: '★ Save' })), el('div', { id: 'rp-bms', class: 'stack' })));
    const spectra = box('Spectrum survey', el('div', { id: 'rp-spectra' }), { span: false, action: el('span', { id: 'rp-updated', class: 'dim', style: 'text-transform:none;letter-spacing:0' }) });
    const filters = el('div', { class: 'row-flex', style: 'margin-bottom:8px' },
      el('select', { class: 'field', id: 'rp-band', onchange: e => { RP.band = e.target.value; radioPage.render(); } }),
      el('label', { class: 'dim' }, el('input', { type: 'checkbox', id: 'rp-hd', onchange: e => { RP.hideDigital = e.target.checked; radioPage.render(); } }), ' hide digital'));
    const chan = box('Active channels', el('div', {}, filters, el('div', { class: 'scroll', style: 'max-height:420px' }, el('table', { class: 'tbl', id: 'rp-table' },
      el('thead', {}, el('tr', {}, ...[['mhz', 'Frequency'], ['label', 'What'], ['duty', 'On air'], ['peak', 'Peak'], ['', '']].map(([k, t]) => el('th', { dataset: { k }, class: k === 'duty' || k === 'peak' ? 'num' : '', onclick: () => {
        if (!k) return; if (RP.sort === k) RP.dir *= -1; else { RP.sort = k; RP.dir = k === 'mhz' || k === 'label' ? 1 : -1; } radioPage.render();
      } }, t)))), el('tbody', { id: 'rp-body' })))));
    const errBox = el('div', { id: 'rp-err' });
    const wrap = el('div', { class: 'radio-page' }, el('div', { class: 'stack' }, errBox, spectra, chan), el('div', { class: 'stack' }, player, bm));
    sh.root.replaceChild(wrap, sh.body);
    sh.root.lastChild.style.overflow = 'auto';
    sec.append(sh.root);
    $('#rp-scope').getContext('2d');
  },
  enter() { radioPage.startScope(); },
  leave() { cancelAnimationFrame(RP.raf); RP.raf = 0; },
  ensureAnalyser() {
    if (RP.analyser) { RP.audioCtx.resume?.(); return; }
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      RP.audioCtx = new Ctx();
      const src = RP.audioCtx.createMediaElementSource($('#radio-audio'));
      RP.analyser = RP.audioCtx.createAnalyser();
      RP.analyser.fftSize = 256;
      src.connect(RP.analyser);
      RP.analyser.connect(RP.audioCtx.destination);
    } catch { RP.analyser = null; } // no Web Audio: the audio element still plays on its own
  },
  startScope() {
    if (RP.raf || S.page !== 'radio') return;
    const draw = () => {
      RP.raf = 0;
      if (S.page !== 'radio') return;
      const cv = $('#rp-scope');
      if (!cv) return;
      const g = cv.getContext('2d'), w = cv.width, h = cv.height;
      g.fillStyle = '#0a0f14'; g.fillRect(0, 0, w, h);
      const playing = S.radio?.listening != null;
      if (RP.analyser && playing) {
        const data = new Uint8Array(RP.analyser.frequencyBinCount);
        RP.analyser.getByteFrequencyData(data);
        const bars = 64, bw = w / bars;
        for (let i = 0; i < bars; i++) {
          const v = data[Math.floor(i * data.length * 0.6 / bars)] / 255;
          const bh = Math.max(2, v * (h - 6));
          const hue = 150 - v * 110;
          g.fillStyle = `hsl(${hue} 85% ${45 + v * 15}%)`;
          g.fillRect(i * bw + 1, h - bh, bw - 2, bh);
        }
      } else {
        g.strokeStyle = '#1f2a36'; g.lineWidth = 1;
        for (let y = 20; y < h; y += 40) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
        g.fillStyle = '#5c6b7a'; g.font = '14px system-ui'; g.textAlign = 'center';
        g.fillText(playing ? 'Waiting for audio…' : 'Pick a channel to listen', w / 2, h / 2);
      }
      RP.raf = requestAnimationFrame(draw);
    };
    RP.raf = requestAnimationFrame(draw);
  },
  addBookmark() {
    const f = frequencyValue($('#rp-mhz').value);
    if (f == null) { toast('Bookmark', 'Enter a frequency from 24 to 1766 MHz first.', null, true); return; }
    const name = $('#rp-bmname').value.trim() || `${f} MHz`;
    if (!RP.bookmarks.some(b => Math.abs(b.mhz - f) < 0.0001)) RP.bookmarks.unshift({ name: name.slice(0, 32), mhz: f });
    saveBookmarks(); $('#rp-bmname').value = ''; radioPage.render();
  },
  render() {
    const r = S.radio;
    radioPage.dongle.update();
    const btn = $('#rp-toggle');
    const survey = sdrCapability('survey'), audio = sdrCapability('audio');
    btn.disabled = !S.connected || (!r?.enabled && !survey.available);
    btn.title = survey.available ? '' : survey.error;
    $('#rp-listen').disabled = !audio.available;
    $('#rp-listen').title = audio.available ? '' : audio.error;
    for (const b of document.querySelectorAll('#rp-presets button')) b.disabled = !audio.available;
    btn.textContent = r?.enabled ? '■ Stop survey' : '▶ Start survey';
    btn.classList.toggle('danger', !!r?.enabled); btn.classList.toggle('primary', !r?.enabled);
    $('#rp-status').textContent = !S.connected ? 'server disconnected' : !survey.available ? survey.error
      : r.listening ? `listening · ${r.listening} MHz` : r.enabled ? (r.updated ? `surveying · updated ${ago(r.updated)} ago` : 'surveying…') : 'idle';
    $('#rp-err').replaceChildren(...(r?.error ? [el('div', { class: 'err-note', role: 'alert', text: r.error })] : []));
    $('#rp-freq').textContent = r?.listening ? r.listening.toFixed(4) : '— — —';
    $('#rp-now').textContent = r?.listening ? `MHz · streaming (the survey is paused while you listen)` : audio.available ? 'MHz' : audio.error;
    $('#rp-stop').disabled = !r?.listening || !S.connected;
    $('#rp-updated').textContent = r?.updated ? `updated ${ago(r.updated)} ago` : '';
    // bookmarks
    const bms = $('#rp-bms');
    bms.replaceChildren();
    if (!RP.bookmarks.length) bms.append(el('div', { class: 'empty-state', text: 'No bookmarks yet. Type a frequency above, name it and press Save.' }));
    for (const b of RP.bookmarks) {
      bms.append(el('div', { class: 'row-flex' }, el('button', { class: 'chip', disabled: !audio.available, style: 'flex:1;text-align:left', onclick: () => { $('#rp-mhz').value = b.mhz; tune(b.mhz); }, text: `★ ${b.name} · ${b.mhz}` }),
        el('button', { class: 'btn small', title: 'Remove', onclick: () => { RP.bookmarks = RP.bookmarks.filter(x => x !== b); saveBookmarks(); radioPage.render(); }, text: '✕' })));
    }
    // spectrum charts
    const spec = $('#rp-spectra');
    const chans = r?.channels || [];
    const sel = $('#rp-band');
    const bandNames = (r?.bands || []).map(b => b.name);
    if (sel.dataset.b !== bandNames.join('|')) {
      sel.dataset.b = bandNames.join('|');
      sel.replaceChildren(el('option', { value: 'all', text: 'All bands' }), ...bandNames.map(n => el('option', { value: n, text: n })));
      sel.value = RP.band;
    }
    spec.replaceChildren();
    if (!r?.bands?.length) spec.append(el('div', { class: 'empty-state', text: 'Radio not available.' }));
    else if (!chans.length) spec.append(el('div', { class: 'empty-state', text: r.enabled ? 'Scanning the bands… charts fill in as each band completes.' : 'Press ▶ Start survey to scan the bands for activity.' }));
    for (const b of chans.length ? r.bands : []) {
      const inBand = chans.filter(c => c.band === b.name);
      if (!inBand.length) continue;
      const W = 640, H = 120, span = b.hi - b.lo;
      const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'spectrum', role: 'img', 'aria-label': `${b.name} activity` });
      const on = r.listening;
      svgEl('line', { x1: 0, y1: H - 18, x2: W, y2: H - 18, stroke: '#1f2a36' }, svg);
      const maxDb = Math.max(20, ...inBand.map(c => c.peak_db));
      for (const c of inBand) {
        const x = ((c.mhz - b.lo) / span) * W, bh = Math.max(4, (c.peak_db / maxDb) * (H - 34));
        const bar = svgEl('rect', { x: x - 2, y: H - 18 - bh, width: 4, height: bh, rx: 1, fill: c.digital ? '#8b98a5' : c.duty > .9 ? '#bb9af7' : '#3fd17d', opacity: Math.max(.45, c.duty), style: 'cursor:pointer' }, svg);
        svgEl('title', {}, bar).textContent = `${c.mhz.toFixed(4)} MHz · ${c.label || b.name} · on ${Math.round(c.duty * 100)}% · +${c.peak_db} dB`;
        bar.addEventListener('click', () => { if (!c.digital) { $('#rp-mhz').value = c.mhz; tune(c.mhz); } else toast('Digital channel', `${c.mhz} MHz carries digital data: it would sound like noise.`); });
        if (on != null && Math.abs(on - c.mhz) < 0.0006) svgEl('rect', { x: x - 5, y: 2, width: 10, height: H - 22, fill: 'none', stroke: '#ffd60a', 'stroke-width': 1.5, rx: 3 }, svg);
      }
      for (let i = 0; i <= 4; i++) {
        const t = svgEl('text', { x: (i / 4) * W, y: H - 5, fill: '#8b98a5', 'font-size': 10, 'text-anchor': i === 0 ? 'start' : i === 4 ? 'end' : 'middle' }, svg);
        t.textContent = (b.lo + (span * i) / 4).toFixed(span < 5 ? 1 : 0);
      }
      spec.append(el('div', { class: 'band-title' }, el('b', { text: b.name }), el('span', { text: `${b.lo}–${b.hi} MHz · ${inBand.length} active` })), svg);
    }
    // table
    let rows = chans.filter(c => (RP.band === 'all' || c.band === RP.band) && !(RP.hideDigital && c.digital));
    rows = sortBy(rows, RP.sort, RP.dir, { mhz: c => c.mhz, label: c => (c.label || c.band).toLowerCase(), duty: c => c.duty, peak: c => c.peak_db });
    for (const th of document.querySelectorAll('#rp-table th')) { th.classList.toggle('sorted', th.dataset.k === RP.sort); th.classList.toggle('asc', th.dataset.k === RP.sort && RP.dir === 1); }
    const body = $('#rp-body');
    body.replaceChildren();
    if (!rows.length) body.append(el('tr', {}, el('td', { colspan: 5, class: 'empty', text: 'No channels yet.' })));
    for (const c of rows) {
      const on = r.listening != null && Math.abs(r.listening - c.mhz) < 0.0005;
      body.append(el('tr', { class: 'row' + (on ? ' sel' : '') },
        el('td', {}, el('b', { text: `${c.mhz.toFixed(4)} MHz` })), el('td', { text: `${c.label || c.band}${c.digital ? ' · digital' : ''}` }),
        el('td', { class: 'num' }, el('span', { class: 'duty' }, el('b', { style: `width:${Math.round(c.duty * 100)}%` })), ` ${Math.round(c.duty * 100)}%`),
        el('td', { class: 'num', text: `+${c.peak_db} dB` }),
        el('td', {}, on ? el('button', { class: 'btn small', onclick: stopTune, text: '■ Stop' })
          : el('button', { class: 'btn small', disabled: c.digital || !audio.available, title: c.digital ? 'digital: not audible as analog FM' : 'Listen', onclick: () => { $('#rp-mhz').value = c.mhz; tune(c.mhz); }, text: '▶ Listen' }))));
    }
  },
};
registerPage('radio', radioPage);
