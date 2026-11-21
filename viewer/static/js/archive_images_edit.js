// Staff image editor of the archive edit form (templates/viewer/include/archive_images_edit.html).
//
// Cards are reordered with native drag and drop, by typing a position, or with the sort/reverse buttons; the trash
// button marks a card for deletion. The form posts "order" (kept image pks, comma-separated, in display order) and
// one "delete" per marked image; the view answers with a fresh editor that HTMX swaps in.
(function () {
  'use strict';
  if (typeof htmx === 'undefined') {
    return;
  }

  function initEditor(form) {
    const grid = form.querySelector('[data-editor-grid]');
    const orderInput = form.querySelector('input[name="order"]');
    const saveBtn = form.querySelector('[data-editor-save]');
    const resetBtn = form.querySelector('[data-editor-reset]');
    const status = form.querySelector('[data-editor-status]');
    const initialStatus = status.textContent.trim();
    const cards = () => Array.from(grid.children);
    const initialOrder = cards();
    let dragged = null;

    function isDeleted(card) {
      return card.classList.contains('is-deleted');
    }

    function sync() {
      form.querySelectorAll('input[name="delete"]').forEach((input) => input.remove());
      const kept = [];
      let deleted = 0;
      let moved = 0;
      cards().forEach((card) => {
        const input = card.querySelector('[data-editor-position]');
        if (isDeleted(card)) {
          deleted++;
          input.value = '';
          input.disabled = true;
          const hidden = document.createElement('input');
          hidden.type = 'hidden';
          hidden.name = 'delete';
          hidden.value = card.dataset.pk;
          form.appendChild(hidden);
          return;
        }
        kept.push(card.dataset.pk);
        input.disabled = false;
        input.max = String(initialOrder.length);
        input.value = String(kept.length);
        const wasAt = initialOrder.indexOf(card) + 1;
        card.classList.toggle('is-moved', wasAt !== kept.length);
        if (wasAt !== kept.length) {
          moved++;
        }
      });
      orderInput.value = kept.join(',');
      const dirty = deleted > 0 || cards().some((card, i) => card !== initialOrder[i]);
      saveBtn.disabled = !dirty;
      resetBtn.disabled = !dirty;
      status.textContent = dirty
        ? 'Unsaved changes: ' + moved + ' moved, ' + deleted + ' marked for deletion.'
        : initialStatus;
    }

    function placeAll(ordered) {
      ordered.forEach((card) => grid.appendChild(card));
      sync();
    }

    // Drag and drop: cards move live while dragging, so the drop is where the gap is.
    grid.addEventListener('dragstart', (evt) => {
      const card = evt.target.closest('.image-editor-card');
      if (!card) {
        return;
      }
      dragged = card;
      card.classList.add('is-dragging');
      evt.dataTransfer.effectAllowed = 'move';
      evt.dataTransfer.setData('text/plain', card.dataset.pk);
    });
    grid.addEventListener('dragover', (evt) => {
      if (!dragged) {
        return;
      }
      evt.preventDefault();
      const target = evt.target.closest('.image-editor-card');
      if (!target || target === dragged) {
        return;
      }
      const rect = target.getBoundingClientRect();
      const after = evt.clientX > rect.left + rect.width / 2;
      grid.insertBefore(dragged, after ? target.nextSibling : target);
    });
    grid.addEventListener('drop', (evt) => evt.preventDefault());
    grid.addEventListener('dragend', () => {
      if (dragged) {
        dragged.classList.remove('is-dragging');
        dragged = null;
        sync();
      }
    });

    // Typing a position moves the card there (among the kept images).
    grid.addEventListener('change', (evt) => {
      const input = evt.target.closest('[data-editor-position]');
      if (!input) {
        return;
      }
      const card = input.closest('.image-editor-card');
      const kept = cards().filter((c) => !isDeleted(c) && c !== card);
      const position = Math.min(Math.max(parseInt(input.value, 10) || 1, 1), kept.length + 1);
      grid.insertBefore(card, kept[position - 1] || null);
      sync();
      card.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      card.classList.remove('is-flash');
      void card.offsetWidth; // restart the animation
      card.classList.add('is-flash');
    });
    // Enter in a position input moves the card instead of submitting the form.
    grid.addEventListener('keydown', (evt) => {
      if (evt.key === 'Enter' && evt.target.matches('[data-editor-position]')) {
        evt.preventDefault();
        evt.target.dispatchEvent(new Event('change', {bubbles: true}));
      }
    });

    grid.addEventListener('click', (evt) => {
      const btn = evt.target.closest('[data-editor-delete]');
      if (!btn) {
        return;
      }
      const card = btn.closest('.image-editor-card');
      card.classList.toggle('is-deleted');
      btn.setAttribute('aria-pressed', String(isDeleted(card)));
      btn.classList.toggle('active', isDeleted(card));
      sync();
    });

    // Deleting removes the files too: ask first (htmx 1.6 has no dynamic hx-confirm, so cancel the request here).
    form.addEventListener('htmx:beforeRequest', (evt) => {
      const deleted = form.querySelectorAll('input[name="delete"]').length;
      if (evt.detail.elt === form && deleted && !window.confirm('Delete ' + deleted + ' image(s) and their files?')) {
        evt.preventDefault();
      }
    });

    resetBtn.addEventListener('click', () => {
      cards().forEach((card) => {
        card.classList.remove('is-deleted');
        const btn = card.querySelector('[data-editor-delete]');
        btn.setAttribute('aria-pressed', 'false');
        btn.classList.remove('active');
      });
      placeAll(initialOrder);
    });
    form.querySelector('[data-editor-sort]').addEventListener('click', () => {
      const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
      placeAll(cards().sort((a, b) => collator.compare(a.dataset.name, b.dataset.name)));
    });
    form.querySelector('[data-editor-reverse]').addEventListener('click', () => {
      placeAll(cards().reverse());
    });

    sync();
  }

  htmx.onLoad((elt) => {
    const forms = elt.matches && elt.matches('[data-image-editor]') ? [elt] : elt.querySelectorAll('[data-image-editor]');
    forms.forEach(initEditor);
  });
})();
