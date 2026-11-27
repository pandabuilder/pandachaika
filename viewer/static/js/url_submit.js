/*
 * Interactive multi-link submission and live public-backup checker for /submit/.
 */
(function () {
  'use strict';

  const DEBOUNCE_MS = 420;
  const URL_TOKEN_RE = /https?:\/\/[^\s<>"']+/gi;

  function formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n <= 0) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let idx = 0;
    let val = n;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx += 1;
    }
    return (idx === 0 ? String(Math.round(val)) : val.toFixed(1)) + ' ' + units[idx];
  }

  function safeHref(url) {
    if (!url || typeof url !== 'string') return '';
    const trimmed = url.trim();
    if (trimmed.startsWith('/') && !trimmed.startsWith('//')) return trimmed;
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    return '';
  }

  function parseLines(rawText) {
    if (!rawText) return [];
    return rawText
      .split(/\r?\n/)
      .map(function (line) { return line.trim(); })
      .filter(Boolean);
  }

  function hasMultiUrlLines(lines) {
    return lines.some(function (line) {
      const matches = line.match(URL_TOKEN_RE);
      return matches && matches.length > 1;
    });
  }

  function splitMultiUrlText(rawText) {
    const out = [];
    parseLines(rawText).forEach(function (line) {
      const matches = line.match(URL_TOKEN_RE);
      if (matches && matches.length > 1) {
        matches.forEach(function (m) { out.push(m); });
      } else {
        out.push(line);
      }
    });
    return out;
  }

  document.addEventListener('DOMContentLoaded', function () {
    const app = document.getElementById('url-submit-app');
    const form = document.getElementById('url-submit-form');
    const textarea = document.getElementById('urls');
    if (!app || !form || !textarea) return;

    const checkUrl = app.dataset.checkUrl;
    const maxUrls = parseInt(app.dataset.maxUrls || '100', 10) || 100;
    const isAuthenticated = app.dataset.authenticated === '1';
    const csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');

    const dropzone = document.getElementById('urls-dropzone');
    const quickAddWrap = document.getElementById('quick-add-wrap');
    const quickAddInput = document.getElementById('quick-add-input');
    const quickAddBtn = document.getElementById('quick-add-btn');
    const pasteBtn = document.getElementById('btn-paste-urls');
    const importBtn = document.getElementById('btn-import-file');
    const fileInput = document.getElementById('urls-file-input');
    const extractBtn = document.getElementById('btn-extract-urls');
    const clearBtn = document.getElementById('btn-clear-urls');
    const lineCountBadge = document.getElementById('url-line-count');

    const reasonInput = document.getElementById('reason');
    const reasonCounter = document.getElementById('reason-counter');

    const inspectorSection = document.getElementById('inspector-section');
    const inspectorTbody = document.getElementById('inspector-tbody');
    const inspectorSearch = document.getElementById('inspector-search');
    const thSelectCol = document.getElementById('th-select-col');
    const thRemoveCol = document.getElementById('th-remove-col');
    const toggleAllBox = document.getElementById('toggle-all-submittable');

    const statTotal = document.getElementById('stat-total');
    const statReady = document.getElementById('stat-ready');
    const statAvailable = document.getElementById('stat-available');
    const statAttention = document.getElementById('stat-attention');
    const filterButtons = Array.from(document.querySelectorAll('[data-us-filter]'));

    const allBackedUpBanner = document.getElementById('all-backed-up-banner');
    const bannerClearBtn = document.getElementById('btn-banner-clear');
    const errorAlert = document.getElementById('check-error-alert');
    const statusIndicator = document.getElementById('check-status-indicator');

    const pruneAvailableBtn = document.getElementById('btn-prune-available');
    const pruneAvailableCount = document.getElementById('prune-available-count');
    const pruneAttentionBtn = document.getElementById('btn-prune-attention');
    const pruneAttentionCount = document.getElementById('prune-attention-count');
    const recheckBtn = document.getElementById('btn-recheck');

    const actionbar = document.getElementById('submit-actionbar');
    const submitSummary = document.getElementById('submit-summary');
    const submitBtn = document.getElementById('btn-submit-urls');
    const submitLabel = document.getElementById('btn-submit-label');
    const checkBtn = document.getElementById('btn-check-urls');

    // Reveal progressive-enhancement controls now that JS is running.
    [quickAddWrap, importBtn, clearBtn, thSelectCol, thRemoveCol, recheckBtn, bannerClearBtn].forEach(function (el) {
      if (el) el.classList.remove('d-none');
    });
    if (pasteBtn && navigator.clipboard && window.isSecureContext) {
      pasteBtn.classList.remove('d-none');
    }

    // State
    let activeFilter = 'all';
    let searchTerm = '';
    let currentLines = [];
    let currentItems = []; // Aligned 1-to-1 with currentLines
    let isChecking = false;
    let lastCheckedSignature = '';
    let debounceTimer = null;
    let activeController = null;
    // Per-URL user selection overrides (map of line string -> boolean)
    const selectionOverrides = new Map();
    // Cache of checked server results for single-URL items (keyed by exact line)
    const singleItemCache = new Map();

    function updateReasonCounter() {
      if (!reasonInput || !reasonCounter) return;
      const maxLen = parseInt(reasonInput.getAttribute('maxlength') || '93', 10) || 93;
      reasonCounter.textContent = reasonInput.value.length + ' / ' + maxLen;
    }

    function disposeRowPopovers() {
      if (typeof bootstrap === 'undefined' || !bootstrap.Popover) return;
      inspectorTbody.querySelectorAll('.img-preview').forEach(function (el) {
        const inst = bootstrap.Popover.getInstance(el);
        if (inst) inst.dispose();
      });
    }

    function isItemSelected(item) {
      if (!item || !item.can_submit) return false;
      if (selectionOverrides.has(item.input)) {
        return Boolean(selectionOverrides.get(item.input));
      }
      return Boolean(item.needs_submit);
    }

    function itemMatchesFilter(item) {
      if (activeFilter === 'submittable') {
        if (!item.can_submit) return false;
      } else if (activeFilter === 'already_available') {
        if (item.status !== 'already_available') return false;
      } else if (activeFilter === 'attention') {
        if (item.status === 'already_available' || item.status === 'ready' || item.status === 'gallery_only' || item.status === 'can_submit' || item.status === 'failed' || item.status === 'checking') {
          return false;
        }
      }
      if (searchTerm) {
        const haystack = [
          item.input || '',
          item.provider || '',
          item.message || '',
          item.gallery ? item.gallery.title : '',
          item.gallery ? item.gallery.category : '',
        ].join(' ').toLowerCase();
        if (haystack.indexOf(searchTerm) === -1) return false;
      }
      return true;
    }

    function renderStatusBadge(item) {
      switch (item.status) {
        case 'checking':
          return '<span class="badge bg-secondary"><span class="spinner-border spinner-border-sm me-1" style="width:0.7rem;height:0.7rem;" role="status"></span>Checking…</span>';
        case 'already_available':
          return '<span class="badge bg-success"><i class="fas fa-check-circle me-1"></i>Already backed up</span>';
        case 'ready':
          return '<span class="badge bg-info text-dark"><i class="fas fa-paper-plane me-1"></i>Ready</span>';
        case 'gallery_only':
          return '<span class="badge dm-badge-outline text-info"><i class="fas fa-box-open me-1"></i>No archive yet</span>';
        case 'can_submit':
          return '<span class="badge bg-info text-dark"><i class="fas fa-paper-plane me-1"></i>Can submit</span>';
        case 'failed':
          return '<span class="badge bg-danger"><i class="fas fa-exclamation-triangle me-1"></i>Failed</span>';
        case 'private_available':
          return '<span class="badge bg-secondary"><i class="fas fa-lock me-1"></i>Private archive</span>';
        case 'in_review':
          return '<span class="badge bg-warning text-dark"><i class="fas fa-clock me-1"></i>In review</span>';
        case 'removed': {
          const label = item.gallery && item.gallery.status_display ? item.gallery.status_display : 'Removed';
          return '<span class="badge bg-danger"><i class="fas fa-ban me-1"></i>' + DM.escapeHtml(label) + '</span>';
        }
        case 'duplicate':
          return '<span class="badge bg-secondary"><i class="fas fa-clone me-1"></i>Duplicate</span>';
        case 'note':
          return '<span class="badge dm-badge-outline text-secondary"><i class="fas fa-sticky-note me-1"></i>Note</span>';
        case 'invalid':
          return '<span class="badge bg-danger"><i class="fas fa-times-circle me-1"></i>Not allowed</span>';
        default:
          return '<span class="badge bg-warning text-dark"><i class="fas fa-exclamation-circle me-1"></i>Unsupported</span>';
      }
    }

    function renderThumbCell(item) {
      if (item.gallery && item.gallery.thumbnail_url) {
        const gUrl = safeHref(item.gallery.url);
        const thumbUrl = safeHref(item.gallery.thumbnail_url);
        if (gUrl && thumbUrl) {
          return (
            '<a href="' + DM.escapeHtml(gUrl) + '" class="img-preview" data-image-url="' + DM.escapeHtml(thumbUrl) + '" target="_blank" rel="noopener">' +
            '<img class="dm-thumb" src="' + DM.escapeHtml(thumbUrl) + '" alt="" loading="lazy"></a>'
          );
        }
      }
      let iconClass = 'fas fa-question';
      if (item.status === 'checking') iconClass = 'fas fa-spinner fa-spin text-info';
      else if (item.status === 'already_available') iconClass = 'fas fa-check text-success';
      else if (item.status === 'ready' || item.status === 'gallery_only' || item.status === 'can_submit') iconClass = 'fas fa-link text-info';
      else if (item.status === 'duplicate') iconClass = 'fas fa-clone text-warning';
      else if (item.status === 'note') iconClass = 'fas fa-sticky-note text-secondary';
      else if (item.status === 'invalid' || item.status === 'failed' || item.status === 'removed') iconClass = 'fas fa-exclamation-triangle text-danger';
      return '<span class="dm-thumb dm-thumb-placeholder"><i class="' + iconClass + '"></i></span>';
    }

    function renderArchivePills(archives) {
      if (!archives || !archives.length) return '';
      const pills = archives.map(function (arc) {
        const href = safeHref(arc.url);
        if (!href) return '';
        const isPrivate = isAuthenticated && arc.public === false;
        const thumbAttr = arc.thumbnail_url && safeHref(arc.thumbnail_url)
          ? ' data-image-url="' + DM.escapeHtml(safeHref(arc.thumbnail_url)) + '"'
          : '';
        const previewClass = thumbAttr ? ' img-preview' : '';
        const privClass = isPrivate ? ' us-archive-private' : '';
        const iconColor = isPrivate ? 'text-secondary' : 'text-success';
        const parts = [
          '<i class="fas fa-archive ' + iconColor + '"></i>',
          '<span>Archive #' + DM.escapeHtml(arc.id) + '</span>',
          '<span class="badge bg-dark text-info">' + DM.escapeHtml(arc.relation || 'main') + '</span>',
        ];
        if (arc.public_date) {
          parts.push('<span class="dm-muted">' + DM.escapeHtml(arc.public_date) + '</span>');
        }
        if (arc.filecount) {
          parts.push('<span class="dm-muted">' + DM.escapeHtml(arc.filecount) + 'p</span>');
        }
        if (isPrivate) {
          parts.push('<span class="badge bg-secondary">private</span>');
        }
        if (isAuthenticated && arc.recycled) {
          parts.push('<span class="badge bg-warning text-dark">binned</span>');
        }
        return (
          '<a href="' + DM.escapeHtml(href) + '" target="_blank" rel="noopener" class="us-archive-pill' + privClass + previewClass + '"' + thumbAttr + '>' +
          parts.join('') +
          '</a>'
        );
      }).filter(Boolean);
      if (!pills.length) return '';
      return '<div class="us-archive-list">' + pills.join('') + '</div>';
    }

    function renderDetailsCell(item) {
      const parts = [];
      const href = safeHref(item.input);
      const urlDisplay = href
        ? '<a href="' + DM.escapeHtml(href) + '" target="_blank" rel="noopener noreferrer nofollow">' + DM.escapeHtml(item.input) + '</a>'
        : DM.escapeHtml(item.input);
      const providerBadge = item.provider
        ? ' <span class="badge bg-secondary ms-1">' + DM.escapeHtml(item.provider) + '</span>'
        : '';
      parts.push('<div class="us-url-line">' + urlDisplay + providerBadge + '</div>');

      if (item.gallery) {
        const g = item.gallery;
        const gHref = safeHref(g.url);
        const titleHtml = gHref
          ? '<a class="us-gallery-title" href="' + DM.escapeHtml(gHref) + '" target="_blank" rel="noopener">' + DM.escapeHtml(g.title) + '</a>'
          : '<span class="us-gallery-title">' + DM.escapeHtml(g.title) + '</span>';
        const badges = [];
        if (g.category) {
          badges.push('<span class="badge dm-badge-outline text-info ms-1">' + DM.escapeHtml(g.category) + '</span>');
        }
        if (isAuthenticated) {
          badges.push(
            g.public
              ? '<span class="badge dm-badge-outline text-success ms-1">Public</span>'
              : '<span class="badge dm-badge-outline text-secondary ms-1">Private</span>'
          );
          if (g.status_display && g.status_display !== 'Normal') {
            badges.push('<span class="badge dm-badge-outline text-warning ms-1">' + DM.escapeHtml(g.status_display) + '</span>');
          }
          if (g.failed) {
            badges.push('<span class="badge bg-danger ms-1">Failed</span>');
          }
        }
        parts.push('<div class="mt-1">' + titleHtml + badges.join('') + '</div>');

        const metaBits = [];
        if (g.posted) metaBits.push('Posted ' + DM.escapeHtml(g.posted));
        if (g.filecount) metaBits.push(DM.escapeHtml(g.filecount) + ' files');
        if (g.filesize) metaBits.push(DM.escapeHtml(formatBytes(g.filesize)));
        if (metaBits.length) {
          parts.push('<div class="us-meta">' + metaBits.join(' · ') + '</div>');
        }
      }

      if (item.status === 'already_available') {
        parts.push(
          '<div class="us-callout-available mt-1"><i class="fas fa-check-circle"></i><span>' +
          DM.escapeHtml(item.message) + '</span></div>'
        );
      } else if (item.message) {
        parts.push('<div class="us-meta mt-1">' + DM.escapeHtml(item.message) + '</div>');
      }

      if (item.archives && item.archives.length) {
        parts.push(renderArchivePills(item.archives));
      }

      return parts.join('');
    }

    function computeCounts() {
      const counts = {
        total: currentItems.length,
        submittable: 0,
        selected: 0,
        already_available: 0,
        attention: 0,
        checking: 0,
      };
      currentItems.forEach(function (item) {
        if (item.status === 'checking') {
          counts.checking += 1;
        } else if (item.status === 'already_available') {
          counts.already_available += 1;
        } else if (item.can_submit) {
          counts.submittable += 1;
          if (isItemSelected(item)) counts.selected += 1;
          if (item.status === 'removed' || item.status === 'private_available' || item.status === 'in_review') {
            counts.attention += 1;
          }
        } else {
          counts.attention += 1;
        }
      });
      return counts;
    }

    function render() {
      disposeRowPopovers();

      const counts = computeCounts();

      // Update header badge and extract button
      if (lineCountBadge) {
        lineCountBadge.textContent = String(currentLines.length);
        lineCountBadge.classList.toggle('d-none', currentLines.length === 0);
      }
      if (extractBtn) {
        extractBtn.classList.toggle('d-none', !hasMultiUrlLines(currentLines));
      }

      inspectorSection.classList.toggle('d-none', currentLines.length === 0);

      statTotal.textContent = String(counts.total);
      statReady.textContent = String(counts.submittable);
      statAvailable.textContent = String(counts.already_available);
      statAttention.textContent = String(counts.attention);

      filterButtons.forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.usFilter === activeFilter);
      });

      // Prune buttons & search input
      if (pruneAvailableBtn) {
        pruneAvailableCount.textContent = String(counts.already_available);
        pruneAvailableBtn.classList.toggle('d-none', counts.already_available === 0);
      }
      const prunableAttentionCount = currentItems.filter(function (it) {
        return it.status === 'duplicate' || it.status === 'unsupported' || it.status === 'invalid';
      }).length;
      if (pruneAttentionBtn) {
        pruneAttentionCount.textContent = String(prunableAttentionCount);
        pruneAttentionBtn.classList.toggle('d-none', prunableAttentionCount === 0);
      }
      if (inspectorSearch) {
        inspectorSearch.classList.toggle('d-none', currentItems.length <= 5);
      }

      // All-backed-up banner: shown when every entered link is already publicly backed up (allowing duplicates/notes)
      const hasBlockingIssues = currentItems.some(function (it) {
        return it.status === 'unsupported' || it.status === 'invalid';
      });
      const allAlreadyBackedUp =
        !isChecking &&
        counts.total > 0 &&
        counts.already_available > 0 &&
        counts.submittable === 0 &&
        !hasBlockingIssues;
      if (allBackedUpBanner) {
        allBackedUpBanner.classList.toggle('d-none', !allAlreadyBackedUp);
      }

      // Status indicator
      if (statusIndicator) {
        if (isChecking) {
          statusIndicator.innerHTML = '<span class="spinner-border spinner-border-sm text-info me-1" style="width:0.75rem;height:0.75rem;"></span>Checking against backup…';
        } else if (counts.total > 0) {
          statusIndicator.textContent = 'Checked ' + counts.total + ' line' + (counts.total === 1 ? '' : 's');
        } else {
          statusIndicator.textContent = '';
        }
      }

      // Rows
      const visibleEntries = [];
      currentItems.forEach(function (item, index) {
        if (itemMatchesFilter(item)) {
          visibleEntries.push({ item: item, index: index });
        }
      });

      if (!visibleEntries.length) {
        inspectorTbody.innerHTML =
          '<tr><td colspan="5" class="dm-empty"><i class="fas fa-filter"></i>No links match the current filter.</td></tr>';
      } else {
        inspectorTbody.innerHTML = visibleEntries.map(function (entry) {
          const item = entry.item;
          const idx = entry.index;
          const selected = isItemSelected(item);
          const rowClasses = [];
          if (item.status === 'already_available') rowClasses.push('us-row-available');
          else if (!item.can_submit && item.status !== 'checking') rowClasses.push('us-row-muted');
          if (selected) rowClasses.push('dm-selected');

          const checkboxHtml = item.can_submit
            ? '<input type="checkbox" class="form-check-input us-row-check" data-index="' + idx + '"' + (selected ? ' checked' : '') + ' aria-label="Include URL in submission">'
            : '<input type="checkbox" class="form-check-input" disabled aria-label="Cannot submit">';

          return (
            '<tr class="' + rowClasses.join(' ') + '" data-index="' + idx + '">' +
            '<td class="dm-check-col">' + checkboxHtml + '</td>' +
            '<td>' + renderThumbCell(item) + '</td>' +
            '<td class="dm-wrap" style="min-width: 18rem;">' + renderDetailsCell(item) + '</td>' +
            '<td class="text-nowrap">' + renderStatusBadge(item) + '</td>' +
            '<td class="text-end">' +
              '<button type="button" class="us-remove-btn" data-remove-index="' + idx + '" title="Remove this line" aria-label="Remove line">' +
                '<i class="fas fa-times"></i>' +
              '</button>' +
            '</td>' +
            '</tr>'
          );
        }).join('');
        DM.initPopovers(inspectorTbody);
      }

      // Header select-all checkbox state
      if (toggleAllBox) {
        const submittableItems = currentItems.filter(function (it) { return it.can_submit; });
        const selectedSubmittable = submittableItems.filter(isItemSelected);
        toggleAllBox.disabled = submittableItems.length === 0;
        toggleAllBox.checked = submittableItems.length > 0 && selectedSubmittable.length === submittableItems.length;
        toggleAllBox.indeterminate = selectedSubmittable.length > 0 && selectedSubmittable.length < submittableItems.length;
      }

      // Action bar & Submit button state
      if (actionbar) {
        actionbar.classList.toggle('has-selection', counts.selected > 0);
      }
      if (submitSummary) {
        if (counts.total === 0) {
          submitSummary.innerHTML = '<span class="dm-muted">Paste one or more gallery URLs above to check and submit them.</span>';
        } else if (isChecking) {
          submitSummary.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Checking ' + counts.total + ' line' + (counts.total === 1 ? '' : 's') + '…</span>';
        } else if (allAlreadyBackedUp) {
          submitSummary.innerHTML = '<span class="text-success fw-semibold"><i class="fas fa-check-circle me-1"></i>All ' + counts.already_available + ' link' + (counts.already_available === 1 ? ' is' : 's are') + ' already backed up — no need to submit.</span>';
        } else {
          const bits = [];
          if (counts.selected > 0) {
            bits.push('<strong class="text-info">' + counts.selected + '</strong> URL' + (counts.selected === 1 ? '' : 's') + ' selected to submit');
          } else {
            bits.push('<span class="text-warning">0 URLs selected to submit</span>');
          }
          if (counts.already_available > 0) {
            bits.push('<span class="text-success">' + counts.already_available + ' already backed up (skipped)</span>');
          }
          if (counts.attention > 0) {
            bits.push('<span class="dm-muted">' + counts.attention + ' other</span>');
          }
          submitSummary.innerHTML = bits.join(' · ');
        }
      }
      if (submitBtn && submitLabel) {
        if (counts.total === 0) {
          submitBtn.disabled = true;
          submitLabel.textContent = 'Submit URLs';
        } else if (isChecking) {
          submitBtn.disabled = false;
          submitLabel.textContent = 'Submit URLs';
        } else if (counts.selected === 0) {
          submitBtn.disabled = true;
          submitLabel.textContent = allAlreadyBackedUp ? 'Already backed up' : 'Nothing to submit';
        } else {
          submitBtn.disabled = false;
          submitLabel.textContent = 'Submit ' + counts.selected + ' URL' + (counts.selected === 1 ? '' : 's');
        }
      }
    }

    function showError(msg) {
      if (!errorAlert) return;
      if (msg) {
        errorAlert.textContent = msg;
        errorAlert.classList.remove('d-none');
      } else {
        errorAlert.textContent = '';
        errorAlert.classList.add('d-none');
      }
    }

    function buildPlaceholderItems(lines) {
      return lines.map(function (line) {
        if (singleItemCache.has(line)) {
          return Object.assign({}, singleItemCache.get(line));
        }
        return {
          input: line,
          provider: '',
          status: 'checking',
          can_submit: false,
          needs_submit: false,
          message: 'Checking…',
          gallery: null,
          archives: [],
          duplicate_of: '',
        };
      });
    }

    async function runCheck(forceRecheck) {
      const lines = parseLines(textarea.value);
      currentLines = lines;
      const signature = lines.join('\n');

      showError('');

      if (!lines.length) {
        if (activeController) {
          activeController.abort();
          activeController = null;
        }
        isChecking = false;
        currentItems = [];
        lastCheckedSignature = '';
        activeFilter = 'all';
        render();
        return;
      }

      if (!forceRecheck && signature === lastCheckedSignature && !isChecking) {
        render();
        return;
      }

      if (forceRecheck) {
        singleItemCache.clear();
      }

      if (lines.length > maxUrls) {
        isChecking = false;
        currentItems = lines.slice(0, maxUrls).map(function (line) {
          return {
            input: line,
            provider: '',
            status: 'unsupported',
            can_submit: false,
            needs_submit: false,
            message: 'Reduce the list to ' + maxUrls + ' lines or fewer to run live check.',
            gallery: null,
            archives: [],
            duplicate_of: '',
          };
        });
        showError('You entered ' + lines.length + ' lines. Live check supports up to ' + maxUrls + ' lines at a time.');
        render();
        return;
      }

      if (activeController) {
        activeController.abort();
      }
      activeController = new AbortController();
      isChecking = true;
      currentItems = buildPlaceholderItems(lines);
      render();

      try {
        const resp = await fetch(checkUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfInput ? csrfInput.value : '',
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: JSON.stringify({ urls: lines }),
          signal: activeController.signal,
        });
        const data = await resp.json().catch(function () { return {}; });
        if (!resp.ok) {
          throw new Error(data.error || ('Check failed (HTTP ' + resp.status + ')'));
        }
        // Only apply if textarea hasn't changed while in flight.
        if (parseLines(textarea.value).join('\n') !== signature) return;

        currentItems = Array.isArray(data.items) ? data.items : [];
        lastCheckedSignature = signature;
        currentItems.forEach(function (item) {
          // Cache non-duplicate items so adding more links feels instant while re-validating duplicates on server.
          if (item.status !== 'duplicate') {
            singleItemCache.set(item.input, item);
          }
        });
        isChecking = false;
        render();
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        isChecking = false;
        showError(err && err.message ? err.message : 'Could not check URLs right now. You can still submit directly.');
        // Allow manual submit even if live check failed.
        currentItems = lines.map(function (line) {
          return {
            input: line,
            provider: '',
            status: 'ready',
            can_submit: true,
            needs_submit: true,
            message: 'Live check unavailable — will be verified on submit.',
            gallery: null,
            archives: [],
            duplicate_of: '',
          };
        });
        render();
      }
    }

    function scheduleCheck(immediate) {
      clearTimeout(debounceTimer);
      if (immediate) {
        runCheck(false);
      } else {
        debounceTimer = setTimeout(function () { runCheck(false); }, DEBOUNCE_MS);
      }
    }

    function setTextareaLines(lines, immediate) {
      textarea.value = lines.join('\n');
      scheduleCheck(Boolean(immediate));
    }

    function appendLines(newText) {
      const incoming = splitMultiUrlText(newText);
      if (!incoming.length) return;
      const existing = parseLines(textarea.value);
      setTextareaLines(existing.concat(incoming), true);
    }

    // --- Event wiring ---

    textarea.addEventListener('input', function () {
      scheduleCheck(false);
    });

    textarea.addEventListener('paste', function () {
      // Wait one microtask for pasted content to land in textarea, auto-split multi-URL lines if needed, then check immediately.
      setTimeout(function () {
        const lines = parseLines(textarea.value);
        if (hasMultiUrlLines(lines)) {
          textarea.value = splitMultiUrlText(textarea.value).join('\n');
        }
        scheduleCheck(true);
      }, 0);
    });

    textarea.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        runCheck(false);
      }
    });

    // Quick-add input
    function handleQuickAdd() {
      if (!quickAddInput) return;
      const val = quickAddInput.value.trim();
      if (!val) {
        quickAddInput.focus();
        return;
      }
      appendLines(val);
      quickAddInput.value = '';
      quickAddInput.focus();
    }
    if (quickAddBtn && quickAddInput) {
      quickAddBtn.addEventListener('click', handleQuickAdd);
      quickAddInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          handleQuickAdd();
        }
      });
      quickAddInput.addEventListener('paste', function () {
        setTimeout(function () {
          if (quickAddInput.value.trim()) handleQuickAdd();
        }, 0);
      });
    }

    // Paste from clipboard button
    if (pasteBtn) {
      pasteBtn.addEventListener('click', async function () {
        try {
          const text = await navigator.clipboard.readText();
          if (text && text.trim()) {
            appendLines(text);
          }
        } catch (err) {
          showError('Clipboard access was denied by your browser. Paste directly into the text box instead.');
        }
      });
    }

    // Import .txt file
    if (importBtn && fileInput) {
      importBtn.addEventListener('click', function () { fileInput.click(); });
      fileInput.addEventListener('change', function () {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = function () {
          if (typeof reader.result === 'string') appendLines(reader.result);
          fileInput.value = '';
        };
        reader.readAsText(file);
      });
    }

    // Drag & drop files or URLs onto textarea
    if (dropzone) {
      ['dragenter', 'dragover'].forEach(function (evName) {
        dropzone.addEventListener(evName, function (e) {
          e.preventDefault();
          dropzone.classList.add('us-drag-over');
        });
      });
      ['dragleave', 'drop'].forEach(function (evName) {
        dropzone.addEventListener(evName, function (e) {
          e.preventDefault();
          dropzone.classList.remove('us-drag-over');
        });
      });
      dropzone.addEventListener('drop', function (e) {
        const dt = e.dataTransfer;
        if (!dt) return;
        if (dt.files && dt.files.length) {
          Array.from(dt.files).forEach(function (file) {
            const reader = new FileReader();
            reader.onload = function () {
              if (typeof reader.result === 'string') appendLines(reader.result);
            };
            reader.readAsText(file);
          });
          return;
        }
        const droppedText = dt.getData('text/uri-list') || dt.getData('text/plain');
        if (droppedText) {
          // text/uri-list comments start with '#'
          const cleaned = droppedText
            .split(/\r?\n/)
            .filter(function (l) { return l.trim() && !l.trim().startsWith('#'); })
            .join('\n');
          appendLines(cleaned);
        }
      });
    }

    if (extractBtn) {
      extractBtn.addEventListener('click', function () {
        setTextareaLines(splitMultiUrlText(textarea.value), true);
      });
    }

    function clearAll() {
      selectionOverrides.clear();
      activeFilter = 'all';
      searchTerm = '';
      if (inspectorSearch) inspectorSearch.value = '';
      setTextareaLines([], true);
      textarea.focus();
    }
    if (clearBtn) clearBtn.addEventListener('click', clearAll);
    if (bannerClearBtn) bannerClearBtn.addEventListener('click', clearAll);

    // Reason counter
    if (reasonInput) {
      reasonInput.addEventListener('input', updateReasonCounter);
      updateReasonCounter();
    }

    // KPI filter tiles
    filterButtons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        activeFilter = btn.dataset.usFilter || 'all';
        render();
      });
    });

    // Quick search inside checked list
    if (inspectorSearch) {
      inspectorSearch.addEventListener('input', function () {
        searchTerm = inspectorSearch.value.trim().toLowerCase();
        render();
      });
      inspectorSearch.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          inspectorSearch.value = '';
          searchTerm = '';
          render();
        }
      });
    }

    // Prune buttons
    if (pruneAvailableBtn) {
      pruneAvailableBtn.addEventListener('click', function () {
        const kept = currentItems
          .filter(function (it) { return it.status !== 'already_available'; })
          .map(function (it) { return it.input; });
        if (activeFilter === 'already_available') activeFilter = 'all';
        setTextareaLines(kept, true);
      });
    }

    if (pruneAttentionBtn) {
      pruneAttentionBtn.addEventListener('click', function () {
        const kept = currentItems
          .filter(function (it) {
            return it.status !== 'duplicate' && it.status !== 'unsupported' && it.status !== 'invalid';
          })
          .map(function (it) { return it.input; });
        if (activeFilter === 'attention') activeFilter = 'all';
        setTextareaLines(kept, true);
      });
    }

    if (recheckBtn) {
      recheckBtn.addEventListener('click', function () { runCheck(true); });
    }

    // Header Check button runs live AJAX check instead of full page reload when JS is active
    if (checkBtn) {
      checkBtn.addEventListener('click', function (e) {
        e.preventDefault();
        runCheck(true);
      });
    }

    // Select all submittable toggle
    if (toggleAllBox) {
      toggleAllBox.addEventListener('change', function () {
        const checked = toggleAllBox.checked;
        currentItems.forEach(function (item) {
          if (item.can_submit) {
            selectionOverrides.set(item.input, checked);
          }
        });
        render();
      });
    }

    // Row checkboxes & remove buttons (event delegation)
    inspectorTbody.addEventListener('change', function (e) {
      const cb = e.target.closest('.us-row-check');
      if (!cb) return;
      const idx = parseInt(cb.dataset.index || '-1', 10);
      const item = currentItems[idx];
      if (!item || !item.can_submit) return;
      selectionOverrides.set(item.input, cb.checked);
      render();
    });

    inspectorTbody.addEventListener('click', function (e) {
      const removeBtn = e.target.closest('[data-remove-index]');
      if (!removeBtn) return;
      const idx = parseInt(removeBtn.dataset.removeIndex || '-1', 10);
      if (idx < 0 || idx >= currentLines.length) return;
      const nextLines = currentLines.slice(0, idx).concat(currentLines.slice(idx + 1));
      setTextareaLines(nextLines, true);
    });

    // Form submit: only send selected submittable URLs + any non-URL note lines when live check completed.
    form.addEventListener('submit', function (e) {
      const submitter = e.submitter;
      if (submitter && submitter.name === 'check') return;

      const lines = parseLines(textarea.value);
      if (!lines.length) {
        e.preventDefault();
        textarea.focus();
        return;
      }

      // If current textarea matches our checked state, submit only selected submittable URLs + kept notes.
      if (!isChecking && lines.join('\n') === lastCheckedSignature && currentItems.length) {
        const finalLines = [];
        let selectedUrlCount = 0;
        currentItems.forEach(function (item) {
          if (item.can_submit && isItemSelected(item)) {
            finalLines.push(item.input);
            selectedUrlCount += 1;
          } else if (item.status === 'note') {
            finalLines.push(item.input);
          }
        });
        if (selectedUrlCount === 0) {
          e.preventDefault();
          showError('No submittable URLs are selected.');
          return;
        }
        textarea.value = finalLines.join('\n');
      }
    });

    // Initial check if URLs were prefilled (e.g. via ?url= or server POST check).
    if (parseLines(textarea.value).length > 0) {
      runCheck(false);
    } else {
      render();
    }
  });
})();
