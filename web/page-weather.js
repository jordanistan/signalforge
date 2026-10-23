'use strict';
/* Weather page: NWS observation, alerts, hourly and 7-day forecast, radar loop. The server fetches from
   api.weather.gov; everything is untrusted text and is only shown with textContent. */

const WP = { openAlert: null, radarKey: '', radarErr: false, radarStation: null };
const tempHue = f => Math.max(0, Math.min(260, 260 - ((f + 10) / 120) * 260)); // cold blue → hot red
const tempColor = f => (f == null ? 'var(--dim)' : `hsl(${tempHue(f)} 85% 60%)`);
const fmtHour = iso => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: 'numeric' }); };
function weatherSectionNote(data, key) {
  const status = data?.status?.[key];
  if (!status || status === 'current') return null;
  const names = { alerts: 'Weather alerts', obs: 'Current observation', forecast: 'Forecast', hourly: 'Hourly forecast' };
  return el('div', { class: 'note section-status', role: 'status', dataset: { section: key }, text: `${names[key]} ${key === 'alerts' ? 'are' : 'is'} ${status === 'stale' ? 'stale; showing the last available data.' : 'unavailable.'}${data.errors?.[key] ? ` ${data.errors[key]}` : ''}` });
}

// ---- radar pop-out: the same loop image, as large as the window allows -----------------------------------
function closeRadarModal() { WP.closeModal?.(); }
function openRadarModal() {
  const d = S.weather?.data, img = $('#wp-radar');
  if (!d?.radar || WP.radarErr || !img?.src || $('#wp-radar-modal')) return;
  const back = document.activeElement;
  const fetched = WP.radarAt ? ` · fetched ${new Date(WP.radarAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : '';
  const closeBtn = el('button', { class: 'btn modal-close', id: 'wp-radar-close', 'aria-label': 'Close the radar', title: 'Close (Esc)', text: '✕' });
  const dialog = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'wp-radar-title' },
    el('div', { class: 'modal-head' },
      el('div', {}, el('h2', { id: 'wp-radar-title', text: `NWS radar ${d.radar}` }), el('span', { class: 'dim', text: `Loop of the last hour · ${d.place}${fetched}` })),
      closeBtn),
    el('img', { class: 'modal-img', id: 'wp-radar-big', src: img.src, alt: `NWS radar loop for ${d.radar}, last hour`, referrerpolicy: 'no-referrer' }));
  const shade = el('div', { class: 'modal-backdrop', id: 'wp-radar-modal', onclick: e => { if (e.target === shade) close(); } }, dialog);
  const onKey = e => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'Tab') { e.preventDefault(); closeBtn.focus(); } // the close button is the only control: keep focus in the dialog
  };
  function close() {
    WP.closeModal = null;
    document.removeEventListener('keydown', onKey, true);
    shade.remove();
    if (back?.isConnected) back.focus();
  }
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', onKey, true);
  WP.closeModal = close;
  document.body.append(shade);
  closeBtn.focus();
}

// ---- getting weather on the handheld: live bridge / follow status and the steps, in plain words ------------
function renderHandheld() {
  const host = $('#wp-handheld');
  if (!host) return;
  const names = (S.allow || []).map(id => nodeName(id));
  const wx = (S.follows || []).filter(f => /weather/i.test(f.topic || ''));
  const status = S.bridge
    ? `Bridge: on · ${names.length ? `${names.length > 1 ? 'handhelds' : 'handheld'} ${names.join(', ')} can use it` : 'no handheld is allowed yet'}`
    : 'Bridge: off';
  const code = t => el('code', { text: t });
  host.replaceChildren(
    el('div', { class: 'hh-status' + (S.bridge ? ' on' : ''), id: 'wp-bridge', role: 'status' }, el('i'), el('b', { text: status })),
    ...wx.map(f => el('div', { class: 'crow hh-follow' }, el('b', { text: nodeName(f.node) }), el('span', { class: 'x', text: `following “${f.topic}”` }),
      el('span', { class: 't', text: `ends in ${fmtDur(Math.max(0, f.expires - nowSec()))}` }))),
    el('ol', { class: 'hh-steps' },
      el('li', {}, 'Start SignalForge with ', code("--bridge --allow '!<your handheld id>'"), ' and keep a Claude session running it (see the README).'),
      el('li', {}, 'From the handheld, text ', code('follow weather <place>'), ', for example ', code('follow weather austin'), '.'),
      el('li', {}, 'Claude checks about every 15 minutes and texts only when there is something new. Text ', code('following'), ' to list what you follow and ', code('unfollow'), ' to stop. A follow ends by itself after 6 hours (text it again to renew); up to 3 at a time.')),
    el('div', { class: 'dim hh-alerts', text: 'NWS alerts are shown at the top of this page. With the bridge on, new alerts are printed for the Claude session to relay; the server does not text them to the handheld by itself.' }));
}

const weatherPage = {
  build(sec) {
    const sh = pageShell('Weather', el('span', { id: 'wp-place', style: 'font-weight:600' }), el('span', { id: 'wp-upd', class: 'dim' }), el('span', { class: 'grow' }),
      el('button', { class: 'btn small', id: 'wp-refresh', onclick: () => { WP.radarErr = false; WP.radarKey = ''; weatherPage.render(); if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'weather' })); }, text: '↻ Refresh' }),
      el('button', { class: 'btn small', id: 'wp-nwr', title: 'Listen to NOAA Weather Radio (162.400 MHz) with the SDR', onclick: () => { setPage('radio'); tune(162.4); }, text: '📻 NOAA Weather Radio' }));
    sh.body.append(el('div', { id: 'wp-err' }), el('div', { id: 'wp-alerts' }),
      el('div', { class: 'grid' },
        box('Right now', el('div', { id: 'wp-now' }), { span: true }),
        box('Next 24 hours', el('div', {}, el('div', { id: 'wp-hourly-status' }), el('div', { class: 'hourly', id: 'wp-hourly' })), { span: true }),
        box('Forecast', el('div', {}, el('div', { id: 'wp-forecast-status' }), el('div', { class: 'days', id: 'wp-days' })), { span: true }),
        box('Radar', el('div', {}, el('div', { class: 'dim', id: 'wp-radar-cap', style: 'margin-bottom:6px' }),
          el('img', { class: 'radar-img', id: 'wp-radar', alt: 'Radar loop (click to enlarge)', title: 'Click to enlarge', role: 'button', tabindex: 0, referrerpolicy: 'no-referrer',
            onerror: () => { WP.radarErr = true; weatherPage.render(); }, onclick: openRadarModal,
            onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openRadarModal(); } } })),
          { action: el('button', { id: 'wp-radar-open', class: 'btn small', title: 'Show the radar loop full size', onclick: openRadarModal, text: '⤢ Enlarge' }) }),
        box('Nearby weather stations (433 MHz)', el('div', { id: 'wp-433' })),
        box('Weather on your handheld', el('div', { id: 'wp-handheld' }))));
    sec.append(sh.root);
  },
  tick() { weatherPage.render(); },
  enter() { if (ws?.readyState === 1 && serviceAvailable('weather') !== false && !S.weather?.data) ws.send(JSON.stringify({ type: 'weather' })); },
  render() {
    const W = S.weather, d = W?.data;
    const unavailable = serviceAvailable('weather') === false;
    const error = W?.error || (unavailable ? S.capabilityErrors.weather || 'Weather is not enabled on this server.' : '');
    $('#wp-refresh').disabled = !S.connected || unavailable;
    $('#wp-nwr').disabled = !sdrCapability('audio').available;
    $('#wp-nwr').title = sdrCapability('audio').available ? 'Listen to NOAA Weather Radio (162.400 MHz)' : sdrCapability('audio').error;
    // A service that is switched off is a setting, not a failure: say so calmly; real fetch errors stay red.
    $('#wp-err').replaceChildren(...(error ? [el('div', unavailable && !W?.error ? { class: 'note', role: 'status', style: 'margin-bottom:12px', text: error } : { class: 'err-note', role: 'alert', style: 'margin-bottom:12px', text: error })] : []));
    $('#wp-place').textContent = d ? `${d.place} · NWS ${d.office}${S.location?.source === 'assumed' ? ' · assumed location' : ''}` : unavailable ? 'Unavailable' : W?.error ? 'Weather unavailable' : 'Loading…';
    $('#wp-upd').textContent = W?.updated ? `updated ${ago(W.updated)} ago` : '';
    const alerts = $('#wp-alerts');
    alerts.replaceChildren();
    const alertNote = weatherSectionNote(d, 'alerts');
    if (alertNote) alerts.append(alertNote);
    for (const a of d?.alerts || []) {
      const open = WP.openAlert === a.id;
      const toggle = () => { WP.openAlert = open ? null : a.id; weatherPage.render(); };
      const box = el('div', { class: `alert sev-${a.severity}${open ? ' open' : ''}`, role: 'button', tabindex: 0, 'aria-expanded': String(open), dataset: { id: a.id }, onclick: toggle, onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } }, style: 'cursor:pointer' },
        el('h4', {}, `⚠ ${a.event} `, el('span', { class: 'tag', text: a.severity })),
        el('div', { class: 'dim', text: a.headline || a.area }),
        el('div', { class: 'txt', text: [a.description, a.instruction && `WHAT TO DO: ${a.instruction}`].filter(Boolean).join('\n\n') }));
      alerts.append(box);
    }
    if (d && !(d.alerts || []).length && !alertNote) alerts.append(el('div', { class: 'note', style: 'margin-bottom:12px' }, el('b', {}, '✓ No active weather alerts '), `for ${d.place}.`));
    // now
    const now = $('#wp-now');
    now.replaceChildren();
    const obsNote = weatherSectionNote(d, 'obs');
    if (obsNote) now.append(obsNote);
    const o = d?.obs;
    if (!d) now.append(el('div', { class: 'empty-state', text: error ? 'No weather data is available.' : 'Loading…' }));
    else {
      const p0 = d.forecast?.[0];
      const fact = (k, v) => el('div', {}, el('span', { text: k }), el('b', { text: v == null ? '–' : String(v) }));
      const temp = o?.temp_f ?? (p0 ? p0.temp : null);
      now.append(el('div', { class: 'wx-hero' },
        el('div', {}, el('div', { class: 'wx-temp', style: `color:${tempColor(temp)}` }, temp == null ? '–' : String(Math.round(temp)), el('small', { text: '°F' })),
          el('div', { class: 'wx-cond', text: o?.text || p0?.short || '' })),
        o ? el('div', { class: 'wx-facts' }, fact('Feels like', o.feels_f != null ? `${o.feels_f}°` : null), fact('Humidity', o.humidity != null ? `${o.humidity}%` : null),
          fact('Wind', o.wind_mph != null ? `${o.wind_dir || ''} ${o.wind_mph} mph${o.gust_mph ? ` (gust ${o.gust_mph})` : ''}` : null),
          fact('Pressure', o.pressure_inhg != null ? `${o.pressure_inhg} inHg` : null), fact('Dew point', o.dew_f != null ? `${o.dew_f}°` : null),
          fact('Visibility', o.visibility_mi != null ? `${o.visibility_mi} mi` : null)) : el('div', { class: 'dim', text: 'No current observation is available; showing the forecast.' }),
        o ? el('div', { class: 'dim', style: 'font-size:12px;flex-basis:100%', text: `${o.station}${o.time ? ` · ${new Date(o.time).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : ''}` }) : null));
    }
    // hourly
    const hr = $('#wp-hourly');
    $('#wp-hourly-status').replaceChildren(...[weatherSectionNote(d, 'hourly')].filter(Boolean));
    hr.replaceChildren();
    const hours = d?.hourly || [];
    const temps = hours.map(h => h.temp).filter(t => t != null);
    const lo = Math.min(...temps), hi = Math.max(...temps);
    for (const h of hours) {
      const pct = h.temp == null ? 0 : hi > lo ? 20 + ((h.temp - lo) / (hi - lo)) * 80 : 60;
      hr.append(el('div', { title: h.short }, el('div', { text: fmtHour(h.start) }), el('div', { class: 'bar' }, el('i', { style: `height:${pct}%;background:${tempColor(h.temp)}` })),
        el('div', { class: 't', text: h.temp == null ? '–' : `${Math.round(h.temp)}°` }), el('div', { class: 'dim', text: h.pop ? `💧${h.pop}%` : '' }), el('div', { class: 'dim', style: 'font-size:11px', text: (h.wind || '').split(' ').slice(0, 3).join(' ') })));
    }
    if (!hours.length) hr.append(el('div', { class: 'empty-state', text: 'No hourly forecast.' }));
    // days
    const days = $('#wp-days');
    $('#wp-forecast-status').replaceChildren(...[weatherSectionNote(d, 'forecast')].filter(Boolean));
    days.replaceChildren();
    for (const p of d?.forecast || []) {
      days.append(el('div', { class: 'day-card', title: p.detail },
        el('b', { text: p.name }), el('span', { class: p.day ? 'hi' : 'lo', style: `color:${tempColor(p.temp)}`, text: p.temp == null ? '–' : `${Math.round(p.temp)}°` }),
        el('div', { text: p.short }), el('div', { class: 'dim', text: `${p.pop ? `💧 ${p.pop}% · ` : ''}${p.wind}` })));
    }
    if (!days.children.length) days.append(el('div', { class: 'empty-state', text: 'No forecast available.' }));
    // radar
    const img = $('#wp-radar');
    if (WP.radarStation !== d?.radar) { WP.radarStation = d?.radar; WP.radarErr = false; WP.radarKey = ''; }
    if (d?.radar && !WP.radarErr) {
      const key = `${d.radar}:${Math.floor(Date.now() / 300000)}`; // refresh the loop every 5 minutes
      if (WP.radarKey !== key) { WP.radarKey = key; WP.radarAt = Date.now(); img.src = `https://radar.weather.gov/ridge/standard/${d.radar}_loop.gif?${key.split(':')[1]}`; }
      $('#wp-radar-cap').textContent = `NWS radar ${d.radar} (loop of the last hour)`;
      img.classList.remove('hidden');
      $('#wp-radar-open').disabled = false;
    } else {
      img.classList.add('hidden');
      $('#wp-radar-open').disabled = true;
      closeRadarModal();
      $('#wp-radar-cap').textContent = WP.radarErr ? 'The radar image could not be loaded (needs the internet).' : d ? 'No radar station for this location.' : 'No radar without weather data.';
    }
    renderHandheld();
    // 433 MHz
    const s = $('#wp-433');
    s.replaceChildren();
    const devs = S.job?.kind === 'sensors' ? S.job.devices || [] : [];
    if (!devs.length) s.append(el('div', { class: 'empty-state' }, 'None heard. Start the ', el('a', { href: '#/sensors', text: 'Sensors' }), ' page to pick up backyard weather stations with the SDR.'));
    for (const v of devs.slice(0, 6)) {
      const t = v.readings.temperature_C;
      s.append(el('div', { class: 'crow' }, el('b', { text: v.model }), el('span', { class: 'x', text: Object.values(v.readings).map(r => `${r.label} ${r.value}${r.unit}`).join(' · ') }), el('span', { class: 't', text: t ? `${Math.round(t.value * 9 / 5 + 32)}°F` : '' })));
    }
  },
};
registerPage('weather', weatherPage);
