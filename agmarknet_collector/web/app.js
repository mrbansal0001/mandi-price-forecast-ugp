'use strict';
const $ = (id) => document.getElementById(id);
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} },
};
const asked = new URLSearchParams(location.search).get('lang');   // ?lang=hi gives a shareable Hindi link
let lang = [asked, store.get('lang', 'en')].find((l) => l in I18N) || 'en';
const t = (key, vars = {}) => (I18N[lang][key] ?? I18N.en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
const stateName = (n) => (I18N[lang].states || I18N.en.states)[n] || n;
const locale = () => lang === 'hi' ? 'hi-IN' : 'en-IN';
const rupee = (n) => '₹' + Math.round(n).toLocaleString('en-IN');
const longDate = (iso) => new Date(iso + 'T00:00:00Z').toLocaleDateString(locale(), {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'});
const shortDate = (iso) => new Date(iso + 'T00:00:00Z').toLocaleDateString(locale(), {day: 'numeric', month: 'short', timeZone: 'UTC'});
const stamp = (iso) => iso ? new Date(iso).toLocaleString(locale(), {day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit'}) : t('never');
const dayNumber = (iso) => Math.round(Date.parse(iso + 'T00:00:00Z') / 86400000);

// Source names come from a public feed, so they are only ever inserted as text.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v; else if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const svgEl = (tag, attrs) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

const asRegion = new URLSearchParams(location.search).get('state');   // ?state=Rajasthan gives a shareable link
const state = {region: asRegion || store.get('region', null), loaded: null, series: [], meta: {}, sid: null, token: 0, range: store.get('range', 90), only: store.get('only', true), lastUpdate: undefined, last: null};
const ORDER = ['group', 'commodity', 'variety', 'grade', 'district', 'market'];
const picked = () => Object.fromEntries(ORDER.map((k) => [k, $(k).value]));

const byName = (a, b) => a.label.localeCompare(b.label);
// Options ordered by how much data they carry, so the default is the best-reported choice.
function options(list, key, preferred) {
  const weight = new Map();
  for (const s of list) weight.set(s[key], (weight.get(s[key]) || 0) + s.count);
  const items = [...weight].map(([value, w]) => ({value, label: value, w}));
  return {items, preferred, best: items.slice().sort((a, b) => b.w - a.w || byName(a, b))[0]?.value};
}
function fill(select, spec, sortAlpha) {
  const items = sortAlpha ? spec.items.slice().sort(byName) : spec.items.slice().sort((a, b) => b.w - a.w || byName(a, b));
  select.replaceChildren(...items.map((i) => h('option', {value: i.value}, i.label)));
  select.value = items.some((i) => i.value === spec.preferred) ? spec.preferred : (spec.best ?? '');
  return items.length;
}

// With the filter on, only series that have a forecast are offered (falling back to everything if none do yet).
function visible() {
  const withForecast = state.series.filter((s) => s.f);
  return state.only && withForecast.length ? withForecast : state.series;
}

function buildPickers(prefer) {
  let list = visible();
  fill($('group'), options(list, 'group', prefer.group), true);
  list = list.filter((s) => s.group === $('group').value);
  fill($('commodity'), options(list, 'commodity', prefer.commodity), true);
  list = list.filter((s) => s.commodity === $('commodity').value);
  const nVariety = fill($('variety'), options(list, 'variety', prefer.variety), false);
  list = list.filter((s) => s.variety === $('variety').value);
  const nGrade = fill($('grade'), options(list, 'grade', prefer.grade), false);
  list = list.filter((s) => s.grade === $('grade').value);
  fill($('district'), options(list, 'district', prefer.district), true);
  list = list.filter((s) => s.district === $('district').value);
  fill($('market'), options(list, 'market', prefer.market), true);
  list = list.filter((s) => s.market === $('market').value);
  $('variety-box').hidden = nVariety < 2; $('grade-box').hidden = nGrade < 2;
  state.sid = list[0] ? list[0].id : null;
  const n = state.series.filter((s) => s.f).length;
  $('l-only').textContent = t('only', {a: n.toLocaleString('en-IN'), b: state.series.length.toLocaleString('en-IN')});
  $('only').checked = state.only;
}

async function getJSON(url) {
  const r = await fetch(url, {cache: 'no-store'});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

function banner(kind, text) { $('banner').replaceChildren(...(text ? [h('div', {class: 'banner ' + kind}, text)] : [])); }

function chart(rows, f, target) {
  const W = 760, H = 280, L = 56, R = 16, T = 16, B = 30;
  if (!rows.length) return h('p', {class: 'muted'}, t('r_none'));
  const x0 = dayNumber(rows[0].date), x1 = dayNumber(target);
  const lows = rows.map((r) => r.price).concat(f ? [f.lower] : []), highs = rows.map((r) => r.price).concat(f ? [f.upper] : []);
  let lo = Math.min(...lows), hi = Math.max(...highs);
  const pad = Math.max((hi - lo) * 0.1, 1); lo = Math.max(0, lo - pad); hi += pad;
  const X = (iso) => L + (dayNumber(iso) - x0) / Math.max(1, x1 - x0) * (W - L - R);
  const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': t('history')});
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    svg.append(svgEl('line', {x1: L, x2: W - R, y1: y, y2: y, stroke: 'var(--grid)'}));
    const label = svgEl('text', {x: L - 8, y: y + 4, 'text-anchor': 'end'}); label.textContent = rupee(v); svg.append(label);
  }
  const ticks = 5;
  for (let i = 0; i < ticks; i++) {
    const day = x0 + Math.round((x1 - x0) * i / (ticks - 1)), iso = new Date(day * 86400000).toISOString().slice(0, 10);
    const label = svgEl('text', {x: X(iso), y: H - 8, 'text-anchor': i === 0 ? 'start' : i === ticks - 1 ? 'end' : 'middle'}); label.textContent = shortDate(iso); svg.append(label);
  }
  // Lines join only consecutive reported days; gaps stay gaps.
  let d = '', prev = null;
  for (const r of rows) {
    const consecutive = prev && dayNumber(r.date) - dayNumber(prev) === 1;
    d += `${consecutive ? 'L' : 'M'}${X(r.date).toFixed(1)} ${Y(r.price).toFixed(1)}`; prev = r.date;
  }
  svg.append(svgEl('path', {d, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2, 'stroke-linejoin': 'round'}));
  for (const r of rows) svg.append(svgEl('circle', {cx: X(r.date), cy: Y(r.price), r: rows.length > 90 ? 1.8 : 2.6, fill: 'var(--accent)'}));
  if (f) {
    const last = rows[rows.length - 1], fx = X(target), fy = Y(f.price);
    svg.append(svgEl('line', {x1: X(last.date), y1: Y(last.price), x2: fx, y2: fy, stroke: 'var(--accent)', 'stroke-dasharray': '4 4'}));
    svg.append(svgEl('rect', {x: fx - 5, y: Y(f.upper), width: 10, height: Math.max(1, Y(f.lower) - Y(f.upper)), fill: 'var(--band)', stroke: 'var(--accent)', rx: 3}));
    svg.append(svgEl('circle', {cx: fx, cy: fy, r: 6, fill: 'var(--card)', stroke: 'var(--accent)', 'stroke-width': 3}));
  }
  return h('div', {class: 'chartbox'}, svg,
    h('div', {class: 'legend small muted'},
      h('span', {}, h('span', {class: 'dot', style: 'background:var(--accent)'}), t('rep')),
      f && h('span', {}, h('span', {class: 'dot', style: 'border:3px solid var(--accent);background:var(--card)'}), t('fc'))));
}

const STATUS = {updated: ['ok', 's_updated'], updating: ['warn', 's_updating'], delayed: ['bad', 's_delayed'], insufficient: ['warn', 's_insufficient']};
function reason(fc) {
  if (!fc.reason) return '';
  if (lang === 'en') return fc.reason;
  if (fc.status === 'delayed') return t('r_delayed', {n: fc.age_days});
  return fc.status === 'insufficient' ? t('r_insufficient') : t('r_updating');
}

function render(series, hist, fc) {
  state.last = {series, hist, fc};
  const [kind, labelKey] = STATUS[fc.status] || ['warn', 's_updating'];
  const rows = hist.rows, latest = rows[rows.length - 1], f = fc.forecast;
  const note = reason(fc);
  const stale = fc.status === 'updated' && fc.age_days >= 2;   // forecast rests on an older report: say so
  if (note) banner(kind === 'bad' ? 'bad' : 'warn', note); else banner('warn', stale ? t('r_stale', {n: fc.age_days}) : '');

  const hero = h('section', {class: 'card hero'},
    h('span', {class: 'pill ' + kind}, STATUS[fc.status] ? t(labelKey) : fc.status),
    h('div', {class: 'when'}, `${series.market}, ${series.district} · ${series.commodity}${$('variety-box').hidden ? '' : ` (${series.variety}, ${series.grade})`}`),
    f ? [
      h('div', {class: 'when'}, t('expected', {date: longDate(fc.target_date)})),
      h('div', {class: 'price'}, rupee(f.price), h('span', {class: 'unit'}, t('perQuintal'))),
      h('div', {class: 'change ' + (f.change > 0 ? 'up' : 'down')},
        `${f.change >= 0 ? '▲' : '▼'} ${rupee(Math.abs(f.change))}` + (f.change_pct === null ? '' : ` (${Math.abs(f.change_pct).toFixed(1)}%)`),
        h('span', {class: 'muted small'}, f.change >= 0 ? t('above') : t('below'))),
    ] : [
      h('div', {class: 'when'}, t('noforecast', {date: longDate(fc.target_date)})),
      latest && h('div', {class: 'price'}, rupee(latest.price), h('span', {class: 'unit'}, t('lastReported'))),
    ],
    h('div', {class: 'facts'},
      latest && h('div', {class: 'fact'}, h('span', {class: 'muted small'}, t('latest')), h('b', {}, rupee(latest.price)), h('span', {class: 'small'}, t('on', {date: longDate(latest.date)}))),
      f && h('div', {class: 'fact'}, h('span', {class: 'muted small'}, t('range')), h('b', {}, `${rupee(f.lower)} – ${rupee(f.upper)}`), h('span', {class: 'small muted'}, t('outside'))),
      h('div', {class: 'fact'}, h('span', {class: 'muted small'}, t('basedOn')), h('b', {}, t('days', {n: fc.observations ?? rows.length})), h('span', {class: 'small'}, t('dataUpTo', {date: (fc.data_cutoff || latest?.date) ? shortDate(fc.data_cutoff || latest.date) : '—'})))));

  const toggle = h('div', {class: 'range', role: 'group', 'aria-label': t('history')},
    [30, 90, 180].map((n) => h('button', {type: 'button', class: 'chip' + (n === state.range ? ' on' : ''), 'aria-pressed': n === state.range ? 'true' : 'false', 'data-range': n}, t('range' + n))));
  toggle.addEventListener('click', (e) => {
    const n = Number(e.target.dataset.range); if (!n || n === state.range) return;
    state.range = n; store.set('range', n); loadSeries();
  });
  const chartCard = h('section', {class: 'card'}, h('div', {class: 'cardhead'}, h('h2', {}, t('history')), toggle), chart(rows, f, fc.target_date));

  const recent = rows.slice(-14).reverse();
  const tableCard = h('section', {class: 'card'},
    h('div', {class: 'cardhead'},
      h('h2', {}, t('recent')),
      h('a', {class: 'btn ghost', href: `/api/series/${encodeURIComponent(series.id)}/history.csv`, download: ''}, t('csv'))),
    h('div', {class: 'scroll'}, h('table', {},
      h('thead', {}, h('tr', {}, ['cDate', 'cLow', 'cHigh', 'cModal'].map((k) => h('th', {}, t(k))))),
      h('tbody', {}, recent.map((r) => h('tr', {}, h('td', {}, longDate(r.date)), h('td', {}, r.min_price == null ? '—' : rupee(r.min_price)), h('td', {}, r.max_price == null ? '—' : rupee(r.max_price)), h('td', {}, rupee(r.price))))))));

  const perf = (fc.performance || []).filter((p) => p.n > 0), cov = fc.interval_coverage || {};
  const detail = h('section', {class: 'card'}, h('details', {},
    h('summary', {}, t('how')),
    h('p', {class: 'small muted'}, t('howText', {model: fc.model || '—', version: fc.model_version || '—', generated: stamp(fc.generated_at), checked: stamp(fc.last_successful_update)})),
    perf.length ? [
      h('p', {class: 'small'}, t('tested', {n: perf[0].n, winner: fc.winner ? t('best', {model: fc.winner}) : ''})),
      h('div', {class: 'scroll'}, h('table', {},
        h('thead', {}, h('tr', {}, ['cModel', 'cMae', 'cRmse', 'cMape', 'cTests'].map((k) => h('th', {}, t(k))))),
        h('tbody', {}, perf.map((p) => h('tr', {}, h('td', {}, p.model), h('td', {}, Math.round(p.mae).toLocaleString('en-IN')), h('td', {}, Math.round(p.rmse).toLocaleString('en-IN')), h('td', {}, p.mape == null ? '—' : p.mape.toFixed(1) + '%'), h('td', {}, `${p.n}${p.mape_n !== p.n ? t('mapeOf', {n: p.mape_n}) : ''}`)))))),
      cov.observed != null && h('p', {class: 'small muted'}, t('coverage', {pct: cov.observed.toFixed(0), n: cov.n})),
    ] : h('p', {class: 'small muted'}, t('nohist'))));

  $('result').replaceChildren(hero, chartCard, tableCard, detail);
}

// One button per tracked state; choosing one reloads the filters for that state only.
function drawStates() {
  const names = state.meta.states.map((x) => x.name);
  $('states').hidden = names.length < 2;
  $('states').replaceChildren(...names.map((n) => h('button', {type: 'button', class: 'chip' + (n === state.region ? ' on' : ''), 'aria-pressed': n === state.region ? 'true' : 'false', 'data-state': n}, stateName(n))));
  $('states').setAttribute('aria-label', t('stateLabel'));
}
$('states').addEventListener('click', (e) => {
  const n = e.target.dataset.state; if (!n || n === state.region) return;
  state.region = n; store.set('region', n); state.series = []; banner('warn', ''); $('result').replaceChildren(); refresh();
});

function applyText() {
  document.documentElement.lang = lang;
  $('title').textContent = t('title'); $('lang').textContent = t('lang'); $('pick-title').textContent = t('pick');
  for (const k of ORDER) $('l-' + k).textContent = t(k);
  $('disclaimer').textContent = t('disclaimer'); document.title = t('title');
}

async function loadSeries() {
  if (!state.sid) { $('result').replaceChildren(); return; }
  const token = ++state.token, sid = state.sid, series = state.series.find((s) => s.id === sid);
  try {
    const [hist, fc] = await Promise.all([getJSON(`/api/series/${encodeURIComponent(sid)}/history?days=${state.range}`), getJSON(`/api/series/${encodeURIComponent(sid)}/forecast`)]);
    if (token !== state.token) return;
    render(series, hist, fc);
    $('freshness').textContent = t('checked', {when: stamp(fc.last_successful_update)});
  } catch (e) {
    if (token === state.token) { banner('bad', t('noLoad', {error: e.message})); $('result').replaceChildren(); }
  }
}

async function refresh({reload = false} = {}) {
  try {
    const status = await getJSON('/api/status');
    state.meta = status;
    const names = status.states.map((x) => x.name);
    if (!names.includes(state.region)) state.region = names[0];
    drawStates();
    if (reload || state.loaded !== state.region || !state.series.length || status.last_successful_update !== state.lastUpdate) {
      const data = await getJSON('/api/selections?state=' + encodeURIComponent(state.region));
      if (data.selected !== state.region) return;
      state.series = data.series; state.lastUpdate = status.last_successful_update;
      if (state.loaded !== state.region) state.sid = null;   // another state: start from its own saved choice
      state.loaded = state.region;
    }
    const here = status.states.find((x) => x.name === state.region);
    $('building').hidden = !state.series.length || here.history_complete;
    $('building').textContent = t('building', {state: stateName(state.region)});
    if (!state.series.length) {
      $('result').replaceChildren(); for (const k of ORDER) $(k).replaceChildren();
      banner('warn', here.history_complete ? t('noData') : (status.running && status.running.state === state.region ? t('collecting', {detail: status.running.detail || ''}) : t('building', {state: stateName(state.region)}))); return;
    }
    buildPickers(state.sid ? {...picked()} : store.get('selection:' + state.region, {}));
    if (status.running || status.waiting) $('freshness').title = t('waiting');
    await loadSeries();
  } catch (e) { banner('bad', t('noService', {error: e.message})); }
}

for (const k of ORDER) {
  $(k).addEventListener('change', () => { buildPickers(picked()); store.set('selection:' + state.region, picked()); loadSeries(); });
}
$('only').addEventListener('change', () => { state.only = $('only').checked; store.set('only', state.only); buildPickers(picked()); loadSeries(); });
$('lang').addEventListener('click', () => {
  if (state.series.length) $('l-only').textContent = t('only', {a: state.series.filter((s) => s.f).length.toLocaleString('en-IN'), b: state.series.length.toLocaleString('en-IN')});
  lang = lang === 'en' ? 'hi' : 'en'; store.set('lang', lang); applyText(); if (state.meta.states) { drawStates(); refresh(); }
  if (state.last) render(state.last.series, state.last.hist, state.last.fc);
  $('freshness').textContent = state.last ? t('checked', {when: stamp(state.last.fc.last_successful_update)}) : '';
});
applyText(); refresh();
// Opening the page never scrapes; it only re-reads stored results once a minute.
setInterval(() => refresh(), 60000);
