/*
 * Shared helpers for the streamlined admin/collaborator pages (see css/dm-pages.css).
 *
 *   DM.selection(...)   checkbox selection: select all, shift-click ranges, row highlight,
 *                       selection count and buttons that need a selection.
 *   DM.autoRefresh(...) auto-refresh toggle + interval, paused while the tab is hidden.
 *   DM.filterChips(...) chips for the active filters of a GET form, each one removable.
 *   DM.escapeHtml, DM.initPopovers, DM.relativeTimes
 *
 * Declarative bits wired on load:
 *   [data-dm-confirm="msg"]   confirm before the click/submit goes through ({n} = selection count).
 *   [data-dm-reltime="iso"]   shows "5m ago" style text, full date in the title.
 *   form.dm-page-jump         jumps to a page keeping the current query string.
 *   [data-dm-hotkey="/"]      focuses the element when the key is pressed outside inputs.
 */
window.DM = (function () {
  'use strict';

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function initPopovers(scope) {
    if (typeof bootstrap === 'undefined' || !bootstrap.Popover) return;
    (scope || document).querySelectorAll('.img-preview').forEach(function (el) {
      if (bootstrap.Popover.getInstance(el)) return;
      new bootstrap.Popover(el, {
        trigger: 'hover',
        html: true,
        placement: 'auto',
        content: function () {
          const url = el.dataset.imageUrl;
          return url ? '<img alt="" style="max-width: 250px; max-height: 350px;" src="' + escapeHtml(url) + '">' : '';
        },
      });
    });
  }

  // --- Selection ---------------------------------------------------------------------------------
  /**
   * @param {object} opts
   * @param {Element} opts.root           container of the checkboxes
   * @param {string}  opts.checkbox       selector of the row checkboxes
   * @param {Element} [opts.toggleAll]    header checkbox
   * @param {string}  [opts.row]          selector of the row element to highlight (default "tr")
   * @param {boolean} [opts.rowClick]     clicking on an empty part of a row toggles it
   * @param {Function} [opts.onChange]    called with the list of checked checkboxes
   */
  function selection(opts) {
    const root = opts.root;
    if (!root) return null;
    const rowSelector = opts.row || 'tr';
    let lastClicked = null;

    function boxes() { return Array.from(root.querySelectorAll(opts.checkbox)); }
    function checked() { return boxes().filter(function (cb) { return cb.checked; }); }

    function refresh() {
      const all = boxes();
      const sel = all.filter(function (cb) { return cb.checked; });
      all.forEach(function (cb) {
        const row = cb.closest(rowSelector);
        if (row) row.classList.toggle('dm-selected', cb.checked);
      });
      if (opts.toggleAll) {
        opts.toggleAll.checked = all.length > 0 && sel.length === all.length;
        opts.toggleAll.indeterminate = sel.length > 0 && sel.length < all.length;
      }
      document.querySelectorAll('[data-dm-sel-count]').forEach(function (el) {
        el.textContent = sel.length === 0 ? 'None selected' : sel.length + ' selected';
      });
      document.querySelectorAll('[data-dm-needs-selection]').forEach(function (el) {
        el.disabled = sel.length === 0;
      });
      document.querySelectorAll('.dm-actionbar').forEach(function (el) {
        el.classList.toggle('has-selection', sel.length > 0);
      });
      if (opts.onChange) opts.onChange(sel);
    }

    function setAll(value) {
      boxes().forEach(function (cb) { cb.checked = value; });
      refresh();
    }

    root.addEventListener('click', function (e) {
      const cb = e.target.closest(opts.checkbox);
      if (cb) {
        const all = boxes();
        if (e.shiftKey && lastClicked && all.indexOf(lastClicked) !== -1) {
          const a = all.indexOf(lastClicked);
          const b = all.indexOf(cb);
          all.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (other) { other.checked = cb.checked; });
        }
        lastClicked = cb;
        refresh();
        return;
      }
      if (!opts.rowClick) return;
      // Toggle when clicking on a plain part of the row, not on links, buttons or inputs.
      if (e.target.closest('a, button, input, select, textarea, label, pre, .dm-no-row-toggle')) return;
      if (window.getSelection && String(window.getSelection())) return;
      const row = e.target.closest(rowSelector);
      if (!row || !root.contains(row)) return;
      const rowBox = row.querySelector(opts.checkbox);
      if (!rowBox) return;
      rowBox.checked = !rowBox.checked;
      lastClicked = rowBox;
      refresh();
    });
    root.addEventListener('mousedown', function (e) {
      // Avoid selecting text on shift-click ranges.
      if (e.shiftKey && e.target.closest(opts.checkbox)) e.preventDefault();
    });
    if (opts.toggleAll) {
      opts.toggleAll.addEventListener('change', function () { setAll(opts.toggleAll.checked); });
    }
    refresh();
    return { checked: checked, refresh: refresh, setAll: setAll, count: function () { return checked().length; } };
  }

  // --- Auto refresh ------------------------------------------------------------------------------
  /**
   * @param {object} opts
   * @param {Element} opts.button      toggle button
   * @param {Element} [opts.select]    interval <select> (ms values)
   * @param {Element} [opts.container] element that gets the .is-live class
   * @param {Element} [opts.updated]   element showing the last update time
   * @param {string}  opts.storageKey  prefix for remembering the choice
   * @param {Function} opts.tick       async function doing the refresh
   * @param {boolean} [opts.defaultOn] start enabled when there is no stored choice
   */
  function autoRefresh(opts) {
    let timer = null;
    let busy = false;
    const intervalKey = opts.storageKey + '_interval';
    const onKey = opts.storageKey + '_on';

    function store(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (e) { /* ignore */ } }
    function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

    const saved = load(intervalKey);
    if (saved && opts.select && opts.select.querySelector('option[value="' + saved + '"]')) opts.select.value = saved;

    function intervalMs() { return parseInt(opts.select ? opts.select.value : '5000', 10) || 5000; }

    async function run() {
      if (busy || document.hidden) return;
      busy = true;
      try {
        await opts.tick();
        if (opts.updated) opts.updated.textContent = 'Updated ' + new Date().toLocaleTimeString();
      } catch (e) {
        if (opts.updated) opts.updated.textContent = 'Update failed';
        console.warn('Auto-refresh failed:', e);
      } finally {
        busy = false;
      }
    }

    function render() {
      const on = timer !== null;
      if (opts.container) opts.container.classList.toggle('is-live', on);
      opts.button.classList.toggle('btn-success', on);
      opts.button.classList.toggle('btn-outline-light', !on);
      opts.button.innerHTML = on
        ? '<span class="dm-live-dot"></span>Live'
        : '<span class="dm-live-dot"></span>Paused';
      opts.button.title = on ? 'Auto-refresh is on, click to pause' : 'Auto-refresh is paused, click to resume';
    }

    function start() {
      stop(true);
      run();
      timer = setInterval(run, intervalMs());
      store(onKey, '1');
      render();
    }
    function stop(silent) {
      if (timer !== null) clearInterval(timer);
      timer = null;
      if (!silent) { store(onKey, '0'); render(); }
    }

    opts.button.addEventListener('click', function () { if (timer !== null) stop(); else start(); });
    if (opts.select) {
      opts.select.addEventListener('change', function () {
        store(intervalKey, opts.select.value);
        if (timer !== null) start();
      });
    }
    // Catch up straight away when coming back to the tab.
    document.addEventListener('visibilitychange', function () { if (!document.hidden && timer !== null) run(); });

    const stored = load(onKey);
    if (stored === '1' || (stored === null && opts.defaultOn)) start(); else render();
    return { start: start, stop: stop, run: run, isOn: function () { return timer !== null; } };
  }

  // --- Active filter chips -----------------------------------------------------------------------
  function fieldLabel(form, name) {
    const field = form.querySelector('[name="' + CSS.escape(name) + '"]');
    if (!field) return null;
    if (field.dataset.chipLabel) return field.dataset.chipLabel;
    if (field.id && !((field.type === 'checkbox' || field.type === 'radio') && form.querySelectorAll('[name="' + CSS.escape(name) + '"]').length > 1)) {
      const label = form.querySelector('label[for="' + CSS.escape(field.id) + '"]');
      if (label && label.textContent.trim()) return label.textContent.trim().replace(/:$/, '');
    }
    return field.getAttribute('aria-label') || field.getAttribute('placeholder') || name;
  }

  function fieldValueText(form, name, value) {
    const fields = form.querySelectorAll('[name="' + CSS.escape(name) + '"]');
    for (const field of fields) {
      if (field.type === 'checkbox' || field.type === 'radio') {
        if (field.value !== value) continue;
        // A lone checkbox: its label says it all. In a group: the option's own label.
        if (fields.length === 1) return null;
        const label = field.id ? form.querySelector('label[for="' + CSS.escape(field.id) + '"]') : null;
        return label ? label.textContent.trim() : value;
      } else if (field.tagName === 'SELECT') {
        const opt = Array.from(field.options).find(function (o) { return o.value === value; });
        if (opt) return opt.textContent.trim();
      }
    }
    return value;
  }

  /**
   * Render chips for the active filters (from the current URL) of a GET form.
   * @param {HTMLFormElement} form
   * @param {Element} container
   * @param {object} [opts] ignore: param names to skip; defaults: {name: value} that don't count as filters
   */
  function filterChips(form, container, opts) {
    if (!form || !container) return 0;
    opts = opts || {};
    const ignore = new Set(['page', 'apply', 'json', 'clear'].concat(opts.ignore || []));
    const defaults = opts.defaults || {};
    const params = new URLSearchParams(window.location.search);
    const chips = [];
    params.forEach(function (value, name) {
      if (ignore.has(name) || value === '') return;
      if (defaults[name] !== undefined && defaults[name] === value) return;
      if (!form.querySelector('[name="' + CSS.escape(name) + '"]') && !(opts.extra && opts.extra[name])) return;
      const next = new URLSearchParams();
      params.forEach(function (v, n) {
        if (n === 'page') return;
        if (n === name && v === value) return;
        next.append(n, v);
      });
      // Removing a param that has a non-empty default keeps it explicitly empty, so the default doesn't come back.
      if (defaults[name] !== undefined && defaults[name] !== '' && !next.has(name)) next.append(name, '');
      if (!next.has('apply')) next.append('apply', '');
      const label = (opts.extra && opts.extra[name]) || fieldLabel(form, name);
      const text = fieldValueText(form, name, value);
      chips.push(
        '<span class="dm-chip"><span class="dm-chip-label">' + escapeHtml(label) + '</span>' +
        (text !== null ? '<span class="dm-chip-value" title="' + escapeHtml(text) + '">' + escapeHtml(text) + '</span>' : '') +
        '<a class="dm-chip-remove" href="?' + escapeHtml(next.toString()) + '" title="Remove this filter" aria-label="Remove filter ' + escapeHtml(label) + '"><i class="fas fa-times"></i></a></span>'
      );
    });
    container.innerHTML = chips.join('');
    container.closest('[data-dm-chips-wrapper]')?.classList.toggle('d-none', chips.length === 0);
    return chips.length;
  }

  // --- Relative times ----------------------------------------------------------------------------
  function relative(date) {
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    const abs = Math.abs(seconds);
    const suffix = seconds >= 0 ? ' ago' : ' from now';
    if (abs < 45) return seconds >= 0 ? 'just now' : 'in a moment';
    if (abs < 3600) return Math.round(abs / 60) + 'm' + suffix;
    if (abs < 86400) return Math.round(abs / 3600) + 'h' + suffix;
    if (abs < 86400 * 30) return Math.round(abs / 86400) + 'd' + suffix;
    return null;
  }

  function relativeTimes(scope) {
    (scope || document).querySelectorAll('[data-dm-reltime]').forEach(function (el) {
      const date = new Date(el.dataset.dmReltime);
      if (isNaN(date.getTime())) return;
      if (!el.dataset.dmAbs) el.dataset.dmAbs = el.textContent.trim();
      const rel = relative(date);
      el.title = el.dataset.dmAbs;
      el.textContent = rel || el.dataset.dmAbs;
    });
  }

  // --- Declarative wiring ------------------------------------------------------------------------
  let currentSelectionCount = function () { return 0; };

  function wireConfirms() {
    document.addEventListener('click', function (e) {
      const el = e.target.closest('[data-dm-confirm]');
      if (!el || el.tagName === 'FORM') return;
      const msg = el.dataset.dmConfirm.replace('{n}', String(currentSelectionCount()));
      if (!window.confirm(msg)) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
    document.addEventListener('submit', function (e) {
      const form = e.target;
      if (form.dataset && form.dataset.dmConfirm && !window.confirm(form.dataset.dmConfirm)) e.preventDefault();
    }, true);
  }

  function wirePageJump() {
    document.querySelectorAll('form.dm-page-jump').forEach(function (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        const input = form.querySelector('input[name="page"]');
        const params = new URLSearchParams(window.location.search);
        params.set('page', input.value || '1');
        window.location.search = params.toString();
      });
    });
    document.querySelectorAll('select.dm-per-page').forEach(function (select) {
      select.addEventListener('change', function () {
        const params = new URLSearchParams(window.location.search);
        params.set(select.name, select.value);
        params.delete('page');
        window.location.search = params.toString();
      });
    });
  }

  function wireHotkeys() {
    document.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest('input, textarea, select, [contenteditable]')) return;
      const el = document.querySelector('[data-dm-hotkey="' + CSS.escape(e.key) + '"]');
      if (!el) return;
      e.preventDefault();
      // Open a collapsed filter card first.
      const collapse = el.closest('.collapse:not(.show)');
      if (collapse && typeof bootstrap !== 'undefined') bootstrap.Collapse.getOrCreateInstance(collapse).show();
      el.focus();
      if (el.dataset.bsToggle === 'dropdown' && typeof bootstrap !== 'undefined') bootstrap.Dropdown.getOrCreateInstance(el).show();
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    wireConfirms();
    wirePageJump();
    wireHotkeys();
    relativeTimes(document);
    setInterval(function () { relativeTimes(document); }, 60000);
  });

  return {
    escapeHtml: escapeHtml,
    initPopovers: initPopovers,
    selection: function (opts) {
      const sel = selection(opts);
      if (sel) currentSelectionCount = sel.count;
      return sel;
    },
    autoRefresh: autoRefresh,
    filterChips: filterChips,
    relativeTimes: relativeTimes,
  };
})();
