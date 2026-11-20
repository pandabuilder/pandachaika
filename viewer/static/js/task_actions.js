// Follows background task actions started through HTMX (see viewer/utils/task_actions.py).
//
// Markup: wrap the buttons of an action in an element with class "task-action", hx-target="this" and
// hx-swap="outerHTML", and give each button/link/form the hx-get or hx-post of the action view. The view answers
// with templates/viewer/include/task_status.html, which polls until the task finishes. This script then reloads the
// page, unless the user has started editing a form on it, in which case the "done" button reloads on click.
(function () {
  'use strict';
  if (typeof htmx === 'undefined') {
    return;
  }

  let formDirty = false;
  let reloadTimer = null;

  document.addEventListener('input', function (evt) {
    if (evt.target.closest && evt.target.closest('form') && !evt.target.closest('.task-action, .task-status')) {
      formDirty = true;
    }
  });

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

  // A view that still answers with a redirect (e.g. a permission error or an expired session): follow it like a
  // normal navigation instead of swapping a whole page into the button.
  document.body.addEventListener('htmx:beforeSwap', function (evt) {
    const xhr = evt.detail.xhr;
    const pathInfo = evt.detail.pathInfo || {};
    const requestedPath = pathInfo.finalPath || pathInfo.path;
    if (!isTaskElement(evt.detail.elt) || !xhr.responseURL || !requestedPath) {
      return;
    }
    const requested = new URL(requestedPath, window.location.href);
    const responded = new URL(xhr.responseURL);
    if (requested.pathname !== responded.pathname) {
      evt.detail.shouldSwap = false;
      window.location.href = xhr.responseURL;
    }
  });

  function showError(elt, message) {
    const target = elt.closest('.task-action, .task-status');
    if (!target) {
      return;
    }
    const span = document.createElement('span');
    span.className = 'task-status text-danger small';
    span.dataset.taskState = 'error';
    span.textContent = message;
    target.replaceWith(span);
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
    showError(elt, status ? 'Request failed (' + status + '). Reload the page and try again.' : 'Request failed. Reload the page and try again.');
  }

  document.body.addEventListener('htmx:responseError', onRequestError);
  document.body.addEventListener('htmx:sendError', onRequestError);
})();
