/*
 * Auto updater schedules: the gallery age timeline (list page) and the schedule editor (provider page).
 *
 * Ages are in seconds. The time axis is logarithmic (log(1 + age / 1h)), so the first hours after a release, where
 * most updates go, get as much room as the following weeks.
 */
(function () {
  'use strict';

  const H = 3600, D = 86400;
  const COLORS = {
    update: '#2a95d8',
    tags: '#c4822b',
    other: '#9a86d6',
    grid: '#17428b',
    text: '#8ba4be',
    dim: '#5f7a96',
    surface: '#001a4c',
    cursor: '#e6edf3',
  };
  const UNITS = { w: 7 * D, d: D, h: H, m: 60 };
  const TICKS = [H, 6 * H, 12 * H, D, 2 * D, 3 * D, 7 * D, 14 * D, 30 * D, 60 * D, 90 * D, 180 * D, 365 * D, 730 * D, 1825 * D, 3650 * D];
  const DOMAINS = [7 * D, 14 * D, 30 * D, 60 * D, 90 * D, 180 * D, 365 * D, 730 * D, 3650 * D];
  const EVERY_CHOICES = ['15m', '30m', '1h', '2h', '3h', '4h', '6h', '8h', '12h', '1d', '2d', '3d', '7d', '14d', '30d'];
  const SVGNS = 'http://www.w3.org/2000/svg';

  function esc(value) {
    return String(value === undefined || value === null ? '' : value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function parseAge(value) {
    if (typeof value === 'number') return value >= 0 ? Math.round(value * H) : null;
    const text = String(value || '').trim().toLowerCase();
    if (!text) return null;
    if (/^\d+(\.\d+)?$/.test(text)) return Math.round(parseFloat(text) * H);
    if (!/^(\s*\d+(\.\d+)?\s*[wdhm])+\s*$/.test(text)) return null;
    let seconds = 0;
    text.replace(/(\d+(?:\.\d+)?)\s*([wdhm])/g, (_, n, unit) => { seconds += parseFloat(n) * UNITS[unit]; return ''; });
    return Math.round(seconds);
  }

  function formatAge(seconds) {
    seconds = Math.round(seconds);
    if (seconds <= 0) return '0h';
    let out = '';
    for (const [unit, size] of [['d', D], ['h', H], ['m', 60]]) {
      const count = Math.floor(seconds / size);
      if (count) { out += count + unit; seconds -= count * size; }
    }
    return out || seconds + 's';
  }

  function ruleAges(rule) {
    const start = parseAge(rule.from);
    const end = rule.to === undefined || rule.to === '' ? start : parseAge(rule.to);
    if (start === null || end === null || end < start) return null;
    if (end === start) return [start];
    const every = parseAge(rule.every);
    if (!every || every < 15 * 60) return null;
    const ages = [];
    for (let age = start; age <= end && ages.length <= 1000; age += every) ages.push(age);
    return ages;
  }

  function allAges(rules) {
    const set = new Set();
    rules.forEach(r => (ruleAges(r) || []).forEach(a => set.add(a)));
    return Array.from(set).sort((a, b) => a - b);
  }

  // Snap an age to a grid that gets coarser with the age.
  function snapAge(seconds) {
    const step = seconds < 6 * H ? 15 * 60 : seconds < D ? H : seconds < 3 * D ? 3 * H : seconds < 14 * D ? 12 * H : seconds < 60 * D ? D : 7 * D;
    return Math.max(0, Math.round(seconds / step) * step);
  }

  function makeScale(max, x0, x1) {
    const f = s => Math.log1p(Math.max(s, 0) / H);
    const fmax = f(max);
    return {
      x: s => x0 + (Math.min(f(s), fmax) / fmax) * (x1 - x0),
      inv: x => Math.expm1(Math.min(Math.max((x - x0) / (x1 - x0), 0), 1) * fmax) * H,
      max,
    };
  }

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVGNS, name);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function pickDomain(maxAge) {
    return DOMAINS.find(d => d >= maxAge * 1.15) || DOMAINS[DOMAINS.length - 1];
  }

  function drawAxis(svg, scale, y, x0, x1) {
    el('line', { x1: x0, x2: x1, y1: y, y2: y, stroke: COLORS.grid }, svg);
    let lastX = -Infinity;
    TICKS.filter(t => t <= scale.max).forEach(t => {
      const x = scale.x(t);
      el('line', { x1: x, x2: x, y1: y, y2: y + 4, stroke: COLORS.grid }, svg);
      if (x - lastX < 34) return;
      lastX = x;
      const label = el('text', { x, y: y + 15, fill: COLORS.text, 'font-size': 10, 'text-anchor': 'middle' }, svg);
      label.textContent = formatAge(t);
    });
  }

  // --- List page: a small timeline of the update ages ---

  function drawMini(svg, ages) {
    const w = svg.clientWidth || 300, h = 30;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.innerHTML = '';
    if (!ages.length) return;
    const scale = makeScale(pickDomain(ages[ages.length - 1]), 4, w - 12);
    el('rect', { x: scale.x(ages[0]), y: 7, width: Math.max(scale.x(ages[ages.length - 1]) - scale.x(ages[0]), 1), height: 8, rx: 2, fill: COLORS.update, opacity: 0.18 }, svg);
    ages.forEach(a => {
      const t = el('line', { x1: scale.x(a), x2: scale.x(a), y1: 4, y2: 18, stroke: COLORS.update, 'stroke-width': 2 }, svg);
      el('title', {}, t).textContent = formatAge(a);
    });
    el('line', { x1: 4, x2: w - 12, y1: 22, y2: 22, stroke: COLORS.grid }, svg);
    let lastX = -Infinity;
    [D, 7 * D, 30 * D, 365 * D].filter(t => t <= scale.max).forEach(t => {
      const x = scale.x(t);
      if (x - lastX < 30) return;
      lastX = x;
      const label = el('text', { x, y: 30, fill: COLORS.dim, 'font-size': 8, 'text-anchor': 'middle' }, svg);
      label.textContent = formatAge(t);
    });
  }

  // --- Provider page: the schedule editor ---

  function Editor(root, data) {
    this.root = root;
    this.data = data;
    this.rules = (data.rules || []).map(r => ({ from: r.from, to: r.to !== undefined ? r.to : r.from, every: r.every || '' }));
    this.svg = root.querySelector('[data-au-chart]');
    this.tooltip = root.querySelector('[data-au-tooltip]');
    this.table = root.querySelector('[data-au-rules]');
    this.input = document.getElementById(data.inputId);
    this.summary = root.querySelector('[data-au-summary]');
    this.domainSelect = root.querySelector('[data-au-domain]');
    this.domain = null;
    this.drag = null;
    this.previewTimer = null;
    this.bind();
    this.render();
  }

  Editor.prototype.bind = function () {
    const self = this;
    this.root.querySelectorAll('[data-au-preset]').forEach(btn => btn.addEventListener('click', () => {
      const preset = self.data.presets.find(p => p.key === btn.dataset.auPreset);
      if (!preset) return;
      if (self.rules.length && !confirm(`Replace the rules with "${preset.name}"?`)) return;
      self.rules = preset.rules.map(r => ({ from: r.from, to: r.to !== undefined ? r.to : r.from, every: r.every || '' }));
      self.domain = null;
      self.render();
    }));
    this.root.querySelector('[data-au-add-range]').addEventListener('click', () => {
      const last = allAges(self.rules).pop() || 0;
      const from = snapAge(Math.max(last * 1.5, D));
      self.rules.push({ from: formatAge(from), to: formatAge(snapAge(from * 2)), every: '1d' });
      self.domain = null;
      self.render();
    });
    this.root.querySelector('[data-au-add-single]').addEventListener('click', () => {
      const last = allAges(self.rules).pop() || 0;
      self.rules.push({ from: formatAge(snapAge(Math.max(last * 2, D))), to: '', every: '' });
      self.rules[self.rules.length - 1].to = self.rules[self.rules.length - 1].from;
      self.domain = null;
      self.render();
    });
    if (this.domainSelect) this.domainSelect.addEventListener('change', () => { self.domain = null; self.render(); });
    window.addEventListener('resize', () => self.drawChart());
    this.svg.addEventListener('pointermove', e => self.onMove(e));
    this.svg.addEventListener('pointerleave', () => { if (!self.drag) self.hideCursor(); });
    this.svg.addEventListener('pointerup', e => self.endDrag(e));
    this.svg.addEventListener('pointercancel', e => self.endDrag(e));
    this.svg.addEventListener('dblclick', e => self.onDoubleClick(e));
  };

  Editor.prototype.currentDomain = function () {
    const choice = this.domainSelect ? this.domainSelect.value : 'auto';
    if (choice !== 'auto') return parseInt(choice, 10);
    if (this.domain === null) {
      const ages = allAges(this.rules);
      this.domain = pickDomain(ages.length ? ages[ages.length - 1] : 7 * D);
    }
    return this.domain;
  };

  Editor.prototype.render = function () {
    this.renderTable();
    this.drawChart();
    this.sync();
  };

  Editor.prototype.sync = function () {
    const clean = this.rules.map(r => {
      const d = { from: r.from, to: r.to || r.from };
      if (parseAge(d.to) !== parseAge(d.from) && r.every) d.every = r.every;
      return d;
    });
    if (this.input) this.input.value = JSON.stringify(clean);
    this.renderSummary();
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => this.fetchPreview(clean), 400);
  };

  Editor.prototype.renderTable = function () {
    const self = this;
    const tbody = this.table.querySelector('tbody');
    tbody.innerHTML = '';
    if (!this.rules.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="small dm-muted">No rules: galleries aren\'t updated. Pick a preset, add a rule, or double-click the timeline.</td></tr>';
      return;
    }
    this.rules.forEach((rule, i) => {
      const tr = document.createElement('tr');
      const single = parseAge(rule.to || rule.from) === parseAge(rule.from);
      const ages = ruleAges(rule);
      tr.innerHTML = `
        <td class="small dm-muted">${i + 1}</td>
        <td><input class="form-control form-control-sm au-age" data-k="from" value="${esc(rule.from)}" aria-label="Rule ${i + 1} from"></td>
        <td><input class="form-control form-control-sm au-age" data-k="to" value="${esc(rule.to || rule.from)}" aria-label="Rule ${i + 1} to"></td>
        <td><input class="form-control form-control-sm au-age" data-k="every" value="${single ? '' : esc(rule.every)}" list="au-every-choices" placeholder="${single ? 'once' : '1d'}" ${single ? 'disabled' : ''} aria-label="Rule ${i + 1} every"></td>
        <td class="small text-nowrap">${ages ? ages.length + (ages.length === 1 ? ' update' : ' updates') : '<span class="text-danger">invalid</span>'}</td>
        <td class="text-end text-nowrap">
          ${single ? `<button type="button" class="btn btn-sm btn-outline-light py-0" data-act="range" title="Repeat from this age">+ range</button>` : ''}
          <button type="button" class="btn btn-sm btn-outline-danger py-0" data-act="remove" title="Remove" aria-label="Remove rule ${i + 1}"><i class="fas fa-times"></i></button>
        </td>`;
      tr.querySelectorAll('input').forEach(input => {
        const check = () => {
          const ok = input.value.trim() === '' ? input.dataset.k !== 'from' : parseAge(input.value) !== null;
          input.classList.toggle('is-invalid', !ok);
        };
        check();
        input.addEventListener('input', () => {
          check();
          rule[input.dataset.k] = input.value.trim();
          if (input.dataset.k === 'from' && single) rule.to = rule.from;
          self.drawChart();
          self.sync();
        });
        input.addEventListener('change', () => { self.domain = null; self.render(); });
      });
      tr.querySelector('[data-act="remove"]').addEventListener('click', () => { self.rules.splice(i, 1); self.render(); });
      const toRange = tr.querySelector('[data-act="range"]');
      if (toRange) toRange.addEventListener('click', () => {
        const from = parseAge(rule.from) || D;
        rule.to = formatAge(snapAge(from * 2));
        rule.every = formatAge(snapAge(Math.max(from / 2, H)));
        self.domain = null;
        self.render();
      });
      tbody.appendChild(tr);
    });
  };

  Editor.prototype.layout = function () {
    const w = Math.max(this.svg.clientWidth || 600, 320);
    const lanes = Math.max(this.rules.length, 1);
    const top = 8, histH = 110, laneH = 22;
    const lanesTop = top + histH + 14;
    const allTop = lanesTop + lanes * laneH + 6;
    const axisY = allTop + 26;
    return { w, h: axisY + 22, x0: 12, x1: w - 16, top, histH, laneH, lanesTop, allTop, axisY };
  };

  Editor.prototype.drawChart = function () {
    const svg = this.svg, L = this.layout();
    this.L = L;
    svg.setAttribute('viewBox', `0 0 ${L.w} ${L.h}`);
    svg.setAttribute('height', L.h);
    svg.innerHTML = '';
    const scale = this.scale = makeScale(this.currentDomain(), L.x0, L.x1);

    // Changes found, by gallery age.
    const buckets = (this.data.changes && this.data.changes.buckets) || [];
    const maxCount = Math.max(1, ...buckets.map(b => b.tags + b.other));
    const base = L.top + L.histH;
    el('line', { x1: L.x0, x2: L.x1, y1: base, y2: base, stroke: COLORS.grid }, svg);
    buckets.forEach(b => {
      const from = b.from * H, to = b.to === null ? scale.max : Math.min(b.to * H, scale.max);
      if (from >= scale.max) return;
      const xa = scale.x(from) + 1, xb = scale.x(to) - 1;
      const width = Math.max(xb - xa, 1);
      const hTags = (b.tags / maxCount) * (L.histH - 14), hOther = (b.other / maxCount) * (L.histH - 14);
      const g = el('g', { 'data-tip': `${formatAge(from)}–${b.to === null ? '…' : formatAge(b.to * H)} old: ${b.tags} tag changes, ${b.other} other changes` }, svg);
      el('rect', { x: xa, y: L.top, width, height: L.histH, fill: 'transparent' }, g);
      if (hTags) el('rect', { x: xa, y: base - hTags, width, height: hTags, rx: 2, fill: COLORS.tags }, g);
      if (hOther) el('rect', { x: xa, y: base - hTags - hOther - (hTags ? 2 : 0), width, height: hOther, rx: 2, fill: COLORS.other }, g);
    });
    if (!buckets.length || !(this.data.changes.total > 0)) {
      const t = el('text', { x: (L.x0 + L.x1) / 2, y: L.top + L.histH / 2, fill: COLORS.dim, 'font-size': 11, 'text-anchor': 'middle' }, svg);
      t.textContent = 'No changes recorded for this provider in the change log yet.';
    }

    // Rules, a lane each.
    this.rules.forEach((rule, i) => {
      const y = L.lanesTop + i * L.laneH;
      const ages = ruleAges(rule) || [];
      const g = el('g', {}, svg);
      if (!ages.length) {
        const t = el('text', { x: L.x0, y: y + 14, fill: '#dc3545', 'font-size': 10 }, g);
        t.textContent = `rule ${i + 1}: invalid`;
        return;
      }
      const xa = scale.x(ages[0]), xb = scale.x(parseAge(rule.to || rule.from));
      const single = ages.length === 1 && parseAge(rule.to || rule.from) === ages[0];
      if (!single) {
        const body = el('rect', { x: xa, y: y + 4, width: Math.max(xb - xa, 2), height: L.laneH - 8, rx: 3, fill: COLORS.update, opacity: 0.22, cursor: 'grab', 'data-drag': `move:${i}`, 'data-tip': `Rule ${i + 1}: ${rule.from} to ${rule.to}, every ${rule.every} (${ages.length} updates). Drag to move, drag the ends to resize.` }, g);
        body.addEventListener('pointerdown', e => this.startDrag(e, 'move', i));
      }
      ages.forEach(a => el('line', { x1: scale.x(a), x2: scale.x(a), y1: y + 5, y2: y + L.laneH - 5, stroke: COLORS.update, 'stroke-width': 2, 'pointer-events': 'none' }, g));
      if (single) {
        const dot = el('circle', { cx: xa, cy: y + L.laneH / 2, r: 6, fill: COLORS.update, stroke: COLORS.surface, 'stroke-width': 2, cursor: 'ew-resize', 'data-tip': `Rule ${i + 1}: once at ${rule.from}. Drag to move.` }, g);
        dot.addEventListener('pointerdown', e => this.startDrag(e, 'single', i));
      } else {
        [['from', xa], ['to', xb]].forEach(([k, x]) => {
          const handle = el('rect', { x: x - 4, y: y + 2, width: 8, height: L.laneH - 4, rx: 2, fill: COLORS.update, stroke: COLORS.surface, 'stroke-width': 2, cursor: 'ew-resize', 'data-tip': `Rule ${i + 1} ${k}: ${rule[k]}` }, g);
          handle.addEventListener('pointerdown', e => this.startDrag(e, k, i));
        });
      }
      const label = el('text', { x: Math.min(xb + 10, L.x1 - 2), y: y + 15, fill: COLORS.text, 'font-size': 10, 'text-anchor': xb + 120 > L.x1 ? 'end' : 'start', 'pointer-events': 'none' }, g);
      label.textContent = single ? `once at ${rule.from}` : `every ${rule.every}`;
      if (xb + 120 > L.x1) label.setAttribute('x', xa - 10);
    });

    // Every update, merged.
    const ages = allAges(this.rules);
    el('rect', { x: L.x0, y: L.allTop, width: L.x1 - L.x0, height: 16, rx: 3, fill: COLORS.grid, opacity: 0.35 }, svg);
    ages.forEach(a => {
      const x = scale.x(a);
      el('line', { x1: x, x2: x, y1: L.allTop + 1, y2: L.allTop + 15, stroke: COLORS.update, 'stroke-width': 2, 'data-tip': `Update at ${formatAge(a)}` }, svg);
    });
    const hidden = ages.filter(a => a > scale.max).length;
    if (hidden) {
      const t = el('text', { x: L.x1, y: L.allTop - 3, fill: COLORS.text, 'font-size': 10, 'text-anchor': 'end' }, svg);
      t.textContent = `+${hidden} after ${formatAge(scale.max)} →`;
    }
    drawAxis(svg, scale, L.axisY, L.x0, L.x1);
    this.cursor = el('line', { x1: 0, x2: 0, y1: L.top, y2: L.axisY, stroke: COLORS.cursor, 'stroke-dasharray': '3 3', opacity: 0, 'pointer-events': 'none' }, svg);
  };

  Editor.prototype.pointerX = function (e) {
    const rect = this.svg.getBoundingClientRect();
    return (e.clientX - rect.left) * (this.L.w / rect.width);
  };

  Editor.prototype.startDrag = function (e, kind, index) {
    e.preventDefault();
    e.stopPropagation();
    const rule = this.rules[index];
    this.drag = { kind, index, startAge: this.scale.inv(this.pointerX(e)), from: parseAge(rule.from), to: parseAge(rule.to || rule.from) };
    try { this.svg.setPointerCapture(e.pointerId); } catch (err) { /* not a captured pointer */ }
  };

  Editor.prototype.onMove = function (e) {
    const x = this.pointerX(e);
    const age = this.scale.inv(x);
    if (this.drag) {
      const d = this.drag, rule = this.rules[d.index];
      const every = parseAge(rule.every) || 0;
      if (d.kind === 'single') {
        rule.from = rule.to = formatAge(snapAge(age));
      } else if (d.kind === 'from') {
        rule.from = formatAge(Math.min(snapAge(age), d.to - (every || 0)));
      } else if (d.kind === 'to') {
        rule.to = formatAge(Math.max(snapAge(age), d.from + (every || 0)));
      } else if (d.kind === 'move') {
        // Moves keep the rule's length on the log scale, so it looks like the same bar.
        const ratio = (1 + age / H) / (1 + d.startAge / H);
        const from = snapAge((1 + d.from / H) * ratio * H - H);
        rule.from = formatAge(from);
        rule.to = formatAge(Math.max(snapAge((1 + d.to / H) * ratio * H - H), from));
      }
      this.drawChart();
      this.renderSummary();
    }
    this.showCursor(e, x, age);
  };

  Editor.prototype.endDrag = function (e) {
    if (!this.drag) return;
    try { this.svg.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
    this.drag = null;
    this.render();
  };

  Editor.prototype.onDoubleClick = function (e) {
    if (e.target.closest('[data-drag]') || e.target.tagName === 'circle') return;
    const age = snapAge(this.scale.inv(this.pointerX(e)));
    if (age <= 0) return;
    this.rules.push({ from: formatAge(age), to: formatAge(age), every: '' });
    this.rules.sort((a, b) => (parseAge(a.from) || 0) - (parseAge(b.from) || 0));
    this.render();
  };

  Editor.prototype.showCursor = function (e, x, age) {
    if (!this.cursor) return;
    this.cursor.setAttribute('x1', x);
    this.cursor.setAttribute('x2', x);
    this.cursor.setAttribute('opacity', 0.6);
    const posted = new Date(Date.now() - age * 1000);
    const target = e.target.closest ? e.target.closest('[data-tip]') : null;
    const tip = esc(this.drag ? `${this.rules[this.drag.index].from} → ${this.rules[this.drag.index].to}` : target ? target.getAttribute('data-tip') : '');
    this.tooltip.innerHTML = `<div><strong>${formatAge(snapAge(age))}</strong> old: posted ~${posted.toISOString().slice(0, 16).replace('T', ' ')}</div>${tip ? `<div>${tip}</div>` : '<div class="dm-dim">Double-click to add an update at this age.</div>'}`;
    this.tooltip.hidden = false;
    const rect = this.svg.getBoundingClientRect();
    const left = e.clientX - rect.left + 14;
    this.tooltip.style.left = Math.min(left, rect.width - 260) + 'px';
    this.tooltip.style.top = (e.clientY - rect.top + 14) + 'px';
  };

  Editor.prototype.hideCursor = function () {
    if (this.cursor) this.cursor.setAttribute('opacity', 0);
    this.tooltip.hidden = true;
  };

  Editor.prototype.renderSummary = function () {
    const ages = allAges(this.rules);
    const perDay = this.data.galleries_per_day || 0;
    const invalid = this.rules.some(r => !ruleAges(r));
    const s = this.summary;
    s.querySelector('[data-k="updates"]').textContent = ages.length;
    s.querySelector('[data-k="range"]').textContent = ages.length ? (ages.length === 1 ? formatAge(ages[0]) : `${formatAge(ages[0])} to ${formatAge(ages[ages.length - 1])}`) : '—';
    const fmt = d => d.toISOString().slice(0, 10);
    s.querySelector('[data-k="posted"]').textContent = ages.length ? `${fmt(new Date(Date.now() - ages[ages.length - 1] * 1000))} to ${fmt(new Date(Date.now() - ages[0] * 1000))}` : '—';
    s.querySelector('[data-k="per-day"]').textContent = `~${Math.round(perDay * ages.length).toLocaleString()}`;
    s.querySelector('[data-k="per-day-sub"]').textContent = `~${perDay.toLocaleString(undefined, { maximumFractionDigits: 1 })} galleries posted per day × ${ages.length}`;
    const warn = s.querySelector('[data-k="warning"]');
    warn.hidden = !(invalid || ages.length > this.data.max_updates);
    warn.textContent = invalid ? 'Some rules are invalid: fix them before saving.' : `More than ${this.data.max_updates} updates per gallery: use a longer "every".`;
  };

  Editor.prototype.fetchPreview = function (rules) {
    const s = this.summary;
    if (!this.data.preview_url) return;
    fetch(`${this.data.preview_url}?rules=${encodeURIComponent(JSON.stringify(rules))}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(d => {
        if (d.error) return;
        s.querySelector('[data-k="in-range"]').textContent = (d.in_range || 0).toLocaleString();
        const next = s.querySelector('[data-k="next"]');
        if (next) next.textContent = d.next_run ? `${d.next_run.galleries.toLocaleString()} galleries` : '—';
      })
      .catch(() => {});
  };

  window.AutoUpdater = { parseAge, formatAge, ruleAges, allAges, snapAge, drawMini, Editor, EVERY_CHOICES, COLORS };
})();
