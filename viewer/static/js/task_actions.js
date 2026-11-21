// Follows background task actions started through HTMX (see viewer/utils/task_actions.py).
//
// Markup: wrap the buttons of an action in an element with class "task-action", hx-target="this" and
// hx-swap="outerHTML", and give each form the hx-post of the action view. The view answers
// with templates/viewer/include/task_status.html, which polls until the task finishes. This script locks each
// button's width across its idle -> queued -> running -> done stages so neighboring buttons never shift, and
// reloads the page once a task finishes (unless the user has started editing a form on it).
(function () {
  'use strict';
  if (typeof htmx === 'undefined') {
    return;
  }

  let formDirty = false;
  let reloadTimer = null;
  // Carries the measured pixel width of an outgoing .task-action / .task-status across an HTMX swap.
  const pendingWidths = new WeakMap();

  document.addEventListener('input', function (evt) {
    if (evt.target.closest && evt.target.closest('form') && !evt.target.closest('.task-action, .task-status')) {
      formDirty = true;
    }
  });

  function isFluidElement(el) {
    return !!(
      el &&
      (el.dataset.taskFluid === 'true' ||
        el.classList.contains('task-status-block') ||
        (el.closest && el.closest('[data-task-fluid="true"]')))
    );
  }

  function measureElementWidth(el) {
    if (!el || isFluidElement(el)) {
      return null;
    }
    if (el.dataset && el.dataset.taskLockedWidth) {
      const existing = parseFloat(el.dataset.taskLockedWidth);
      if (existing > 0) {
        return existing;
      }
    }
    if (el.matches && el.matches('.task-status')) {
      const btn = el.querySelector('.task-status-btn') || el;
      const w = btn.getBoundingClientRect().width;
      return w > 0 ? Math.ceil(w) : null;
    }
    if (el.classList && el.classList.contains('btn')) {
      const w = el.getBoundingClientRect().width;
      return w > 0 ? Math.ceil(w) : null;
    }
    // Wrapper elements (.task-action spans/forms with display: contents or d-inline): measure their visible children.
    const rects = [];
    for (let i = 0; i < el.children.length; i++) {
      const child = el.children[i];
      if (child.tagName === 'INPUT' && child.type === 'hidden') {
        continue;
      }
      const r = child.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        rects.push(r);
      }
    }
    if (rects.length === 1) {
      return Math.ceil(rects[0].width);
    }
    if (rects.length > 1) {
      let minTop = Infinity;
      let maxTop = -Infinity;
      let minLeft = Infinity;
      let maxRight = -Infinity;
      let maxWidth = 0;
      for (let i = 0; i < rects.length; i++) {
        const r = rects[i];
        if (r.top < minTop) minTop = r.top;
        if (r.top > maxTop) maxTop = r.top;
        if (r.left < minLeft) minLeft = r.left;
        if (r.right > maxRight) maxRight = r.right;
        if (r.width > maxWidth) maxWidth = r.width;
      }
      // Single horizontal row: span includes flex gap between paired buttons (e.g. Expand + Expand resized).
      const span = maxTop - minTop < 8 ? maxRight - minLeft : maxWidth;
      return span > 0 ? Math.ceil(span) : null;
    }
    const fallback = el.getBoundingClientRect().width;
    return fallback > 0 ? Math.ceil(fallback) : null;
  }

  function lockStatusWidth(statusEl, width) {
    if (!statusEl || !width || width <= 0 || isFluidElement(statusEl)) {
      return;
    }
    statusEl.dataset.taskLockedWidth = String(width);
    const btn = statusEl.querySelector('.task-status-btn');
    if (btn) {
      const px = width + 'px';
      btn.style.width = px;
      btn.style.minWidth = px;
      btn.style.maxWidth = px;
    }
  }

  function ensureLockedWidths(root) {
    if (!root) {
      return;
    }
    const statuses =
      root.matches && root.matches('.task-status')
        ? [root]
        : root.querySelectorAll
          ? root.querySelectorAll('.task-status')
          : [];
    statuses.forEach(function (status) {
      if (isFluidElement(status)) {
        return;
      }
      if (status.dataset.taskLockedWidth) {
        lockStatusWidth(status, parseFloat(status.dataset.taskLockedWidth));
      } else {
        const w = measureElementWidth(status);
        if (w) {
          lockStatusWidth(status, w);
        }
      }
    });
  }

  function syncNoWorkerBanners() {
    const containersWithWarning = new Set();
    document.querySelectorAll('.task-status[data-worker-running="false"]').forEach(function (status) {
      const state = status.dataset.taskState;
      if (state !== 'pending' && state !== 'processing') {
        return;
      }
      const container =
        status.closest('.action-toolbar-card, .tools-toolbar-card, .list-group') ||
        document.getElementById('content') ||
        document.body;
      if (container) {
        containersWithWarning.add(container);
      }
    });

    document.querySelectorAll('.task-worker-warning').forEach(function (banner) {
      if (!containersWithWarning.has(banner.parentElement)) {
        banner.remove();
      }
    });

    containersWithWarning.forEach(function (container) {
      if (container.querySelector(':scope > .task-worker-warning')) {
        return;
      }
      const notice = document.createElement('div');
      notice.className = 'task-worker-warning';
      notice.setAttribute('role', 'status');
      notice.innerHTML =
        '<i class="fas fa-exclamation-triangle me-1" aria-hidden="true"></i>' +
        'No worker process is reporting a heartbeat — queued tasks will wait until a worker starts.';
      container.appendChild(notice);
    });
  }

  function scheduleReload() {
    if (reloadTimer !== null || formDirty) {
      return;
    }
    // Leave the "done" state visible for a moment before reloading.
    reloadTimer = window.setTimeout(function () {
      reloadTimer = null;
      if (formDirty) {
        return;
      }
      if (document.querySelector('.modal.show')) {
        // Don't pull a dialog away from the user; reload once it's closed.
        scheduleReload();
        return;
      }
      window.location.reload();
    }, 800);
  }

  htmx.onLoad(function (elt) {
    ensureLockedWidths(elt);
    syncNoWorkerBanners();
    const statuses = elt.matches && elt.matches('.task-status') ? [elt] : elt.querySelectorAll('.task-status');
    statuses.forEach(function (status) {
      if (status.dataset.taskState === 'completed') {
        scheduleReload();
      }
    });
  });

  document.addEventListener('click', function (evt) {
    const btn = evt.target.closest('[data-task-reload]');
    if (btn) {
      evt.preventDefault();
      window.location.reload();
    }
  });

  function isTaskElement(elt) {
    return elt && elt.closest && elt.closest('.task-action, .task-status');
  }

  // 1) A view that still answers with a redirect (e.g. a permission error or an expired session): follow it like a
  //    normal navigation instead of swapping a whole page into the button.
  // 2) Capture the outgoing button/action width before HTMX replaces it so the incoming status keeps the exact width.
  document.body.addEventListener('htmx:beforeSwap', function (evt) {
    const xhr = evt.detail.xhr;
    const pathInfo = evt.detail.pathInfo || {};
    const requestedPath = pathInfo.finalPath || pathInfo.path;
    if (isTaskElement(evt.detail.elt) && xhr && xhr.responseURL && requestedPath) {
      const requested = new URL(requestedPath, window.location.href);
      const responded = new URL(xhr.responseURL);
      if (requested.pathname !== responded.pathname) {
        evt.detail.shouldSwap = false;
        window.location.href = xhr.responseURL;
        return;
      }
    }

    const target = evt.detail.target;
    if (
      evt.detail.shouldSwap !== false &&
      xhr &&
      target &&
      target.matches &&
      target.matches('.task-action, .task-status') &&
      !target.matches('.task-tray')
    ) {
      const width = measureElementWidth(target);
      if (width) {
        pendingWidths.set(xhr, width);
      }
    }
  });

  // Runs synchronously right after HTMX inserts the swapped node, before the browser paints.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    const xhr = evt.detail && evt.detail.xhr;
    const swapped = evt.target;
    if (!swapped || !swapped.matches) {
      return;
    }
    const statusEl = swapped.matches('.task-status')
      ? swapped
      : swapped.querySelector
        ? swapped.querySelector('.task-status')
        : null;
    if (!statusEl) {
      return;
    }
    const locked = xhr ? pendingWidths.get(xhr) : null;
    if (xhr) {
      pendingWidths.delete(xhr);
    }
    if (locked) {
      lockStatusWidth(statusEl, locked);
    } else if (!isFluidElement(statusEl)) {
      const initialWidth = measureElementWidth(statusEl);
      if (initialWidth) {
        lockStatusWidth(statusEl, initialWidth);
      }
    }
    syncNoWorkerBanners();
  });

  function showError(elt, message) {
    const target = elt.closest('.task-action, .task-status');
    if (!target) {
      return;
    }
    const fluid = isFluidElement(target);
    const width = fluid ? null : measureElementWidth(target);
    const span = document.createElement('span');
    span.className = fluid ? 'task-status task-status-block' : 'task-status d-inline-flex align-items-center';
    span.dataset.taskState = 'error';
    if (fluid) {
      span.dataset.taskFluid = 'true';
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = fluid
      ? 'list-group-item list-group-item-action list-group-item-danger text-start w-100 task-status-btn task-status-block-btn'
      : 'btn btn-sm btn-outline-danger task-status-btn';
    btn.setAttribute('data-task-reload', '');
    btn.title = message;
    btn.innerHTML =
      '<i class="fas fa-exclamation-triangle me-1 flex-shrink-0" aria-hidden="true"></i>' +
      '<span class="task-status-text"></span>';
    btn.querySelector('.task-status-text').textContent = message;
    span.appendChild(btn);
    target.replaceWith(span);
    if (width) {
      lockStatusWidth(span, width);
    }
  }

  function onRequestError(evt) {
    const elt = evt.detail.elt;
    if (!isTaskElement(elt)) {
      return;
    }
    if (elt.matches('.task-status')) {
      // Polling hiccup (server restart, network): try again later.
      htmx.trigger(elt, 'task-retry');
      return;
    }
    const status = evt.detail.xhr ? evt.detail.xhr.status : 0;
    showError(
      elt,
      status ? 'Request failed (' + status + '). Reload the page and try again.' : 'Request failed. Reload the page and try again.',
    );
  }

  document.body.addEventListener('htmx:responseError', onRequestError);
  document.body.addEventListener('htmx:sendError', onRequestError);
})();
