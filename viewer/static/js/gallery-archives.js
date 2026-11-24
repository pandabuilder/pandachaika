// Repeated archives / different filesize pages (templates/viewer/include/gallery_archives_page.html).
// Needs js/dm-pages.js.
document.addEventListener('DOMContentLoaded', function () {
  'use strict';
  const root = document.getElementById('groups-root');
  if (!root) return;

  const titleInput = document.querySelector('#filter-form input[name="title"]');
  if (titleInput) titleInput.dataset.dmHotkey = '/';

  const sel = DM.selection({ root: root, checkbox: '.archive-checkbox', rowClick: true });
  DM.filterChips(document.getElementById('filter-form'), document.getElementById('filter-chips'), { ignore: ['limit'] });

  function eachGroup(fn) {
    root.querySelectorAll('.ga-group').forEach(function (group) {
      fn(Array.from(group.querySelectorAll('.archive-checkbox')));
    });
  }

  document.getElementById('select-different').addEventListener('click', function () {
    eachGroup(function (boxes) {
      boxes.forEach(function (cb) { cb.checked = cb.dataset.sameSize !== '1'; });
    });
    sel.refresh();
  });

  document.getElementById('select-extra').addEventListener('click', function () {
    eachGroup(function (boxes) {
      if (!boxes.length) return;
      const keep = boxes.find(function (cb) { return cb.dataset.sameSize === '1'; }) || boxes[0];
      boxes.forEach(function (cb) { cb.checked = cb !== keep; });
    });
    sel.refresh();
  });

  document.getElementById('select-none').addEventListener('click', function () { sel.setAll(false); });
});
