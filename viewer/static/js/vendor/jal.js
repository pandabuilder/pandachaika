/**
 * Vanilla JS Autocomplete for text inputs (replacement for legacy jquery.autocomplete-light).
 * Supports comma-separated terms (e.g. tags) and single queries (e.g. titles).
 */
(function () {
  'use strict';

  function getCursorWordPositions(input) {
    var val = input.value || '';
    var pos = input.selectionStart != null ? input.selectionStart : val.length;

    // Find start (after previous comma)
    var start = pos - 1;
    while (start >= 0 && val[start] !== ',') {
      start--;
    }
    start = start < 0 ? 0 : start + 1;

    // Find end (before next comma)
    var end = pos;
    while (end < val.length && val[end] !== ',') {
      end++;
    }

    // Trim whitespace bounds
    while (start < end && val[start] === ' ') {
      start++;
    }
    while (end > start && val[end - 1] === ' ') {
      end--;
    }

    return [start, end];
  }

  function getCursorWord(input) {
    var val = input.value || '';
    var bounds = getCursorWordPositions(input);
    return val.substring(bounds[0], bounds[1]);
  }

  function positionBox(input, box) {
    var rect = input.getBoundingClientRect();
    box.style.position = 'absolute';
    box.style.top = (rect.bottom + window.scrollY) + 'px';
    box.style.left = (rect.left + window.scrollX) + 'px';
    box.style.minWidth = Math.max(rect.width, 160) + 'px';
  }

  function initJalInput(input) {
    if (input.dataset.jalInitialized) return;
    input.dataset.jalInitialized = 'true';

    input.setAttribute('autocomplete', 'off');

    var url = input.getAttribute('data-autocomplete-url');
    if (!url) return;

    var minChars = parseInt(input.getAttribute('data-autocomplete-minimum-characters'), 10);
    if (isNaN(minChars)) minChars = 3;

    var xhrWait = parseInt(input.getAttribute('data-autocomplete-xhr-wait'), 10);
    if (isNaN(xhrWait)) xhrWait = 100;

    var choiceSelector = input.getAttribute('data-autocomplete-choice-selector') || '[data-value], .choice, a';

    // Create dropdown box
    var box = document.createElement('div');
    box.className = 'yourlabs-autocomplete autocomplete-light-text-widget';
    box.style.display = 'none';
    box.style.zIndex = '10000';
    document.body.appendChild(box);

    var currentXhr = null;
    var debounceTimer = null;
    var isMouseDownOnBox = false;

    function hideBox() {
      box.style.display = 'none';
      box.innerHTML = '';
      input.classList.remove('xhr-pending');
    }

    function hilight(choice) {
      box.querySelectorAll('.hilight').forEach(function (el) {
        el.classList.remove('hilight');
      });
      if (choice) {
        choice.classList.add('hilight');
        choice.scrollIntoView({ block: 'nearest' });
      }
    }

    function selectChoice(choice) {
      if (!choice) return;

      // If it's a direct link with an href (like archive/gallery link in search), follow it
      var href = choice.getAttribute('href') || (choice.querySelector('a') && choice.querySelector('a').getAttribute('href'));
      if (href) {
        hideBox();
        window.location.href = href;
        return;
      }

      var text = choice.getAttribute('data-value');
      if (text === null || text === undefined) {
        text = choice.textContent.trim();
      }

      var bounds = getCursorWordPositions(input);
      var val = input.value || '';
      var before = val.substring(0, bounds[0]);
      var after = val.substring(bounds[1]);

      // If comma-separated, format appropriately
      var newVal = before + text;
      var newPos = newVal.length;
      if (after.length > 0) {
        newVal += after;
      } else if (val.indexOf(',') !== -1) {
        newVal += ', ';
        newPos = newVal.length;
      }

      input.value = newVal;
      input.setSelectionRange(newPos, newPos);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));

      hideBox();
      input.focus();
    }

    function fetchResults(query) {
      if (currentXhr) {
        currentXhr.abort();
      }

      var requestUrl = url + (url.indexOf('?') !== -1 ? '&' : '?') + 'q=' + encodeURIComponent(query);
      input.classList.add('xhr-pending');

      var xhr = new XMLHttpRequest();
      currentXhr = xhr;

      xhr.open('GET', requestUrl);
      xhr.onload = function () {
        input.classList.remove('xhr-pending');
        if (xhr.status >= 200 && xhr.status < 300) {
          var html = xhr.responseText.trim();
          if (!html) {
            hideBox();
            return;
          }
          box.innerHTML = html;
          positionBox(input, box);
          box.style.display = 'block';

          // Bind choice hover
          var choices = box.querySelectorAll(choiceSelector);
          choices.forEach(function (ch) {
            ch.addEventListener('mouseenter', function () {
              hilight(ch);
            });
          });
        } else {
          hideBox();
        }
      };
      xhr.onerror = function () {
        input.classList.remove('xhr-pending');
        hideBox();
      };
      xhr.send();
    }

    function onInput() {
      var word = getCursorWord(input);
      if (word.length < minChars) {
        hideBox();
        return;
      }
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        fetchResults(word);
      }, xhrWait);
    }

    input.addEventListener('input', onInput);

    input.addEventListener('focus', function () {
      var word = getCursorWord(input);
      if (word.length >= minChars) {
        onInput();
      }
    });

    input.addEventListener('keydown', function (e) {
      if (box.style.display === 'none') {
        return;
      }

      var choices = Array.from(box.querySelectorAll(choiceSelector));
      if (!choices.length) return;

      var current = box.querySelector('.hilight');
      var index = choices.indexOf(current);

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        var next = index + 1 >= choices.length ? 0 : index + 1;
        hilight(choices[next]);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        var prev = index - 1 < 0 ? choices.length - 1 : index - 1;
        hilight(choices[prev]);
      } else if (e.key === 'Enter') {
        if (current) {
          e.preventDefault();
          selectChoice(current);
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        hideBox();
      }
    });

    input.addEventListener('blur', function () {
      if (!isMouseDownOnBox) {
        hideBox();
      }
    });

    box.addEventListener('mousedown', function () {
      isMouseDownOnBox = true;
    });

    box.addEventListener('mouseup', function (e) {
      isMouseDownOnBox = false;
      var target = e.target.closest(choiceSelector);
      if (target) {
        selectChoice(target);
      }
    });

    window.addEventListener('resize', function () {
      if (box.style.display !== 'none') {
        positionBox(input, box);
      }
    });
    window.addEventListener('scroll', function () {
      if (box.style.display !== 'none') {
        positionBox(input, box);
      }
    }, true);
  }

  function initAll() {
    var inputs = document.querySelectorAll('input[data-widget-bootstrap="text"], input.autocomplete-light-text-widget');
    inputs.forEach(initJalInput);

    // Also ensure any autocomplete custom element inputs have autocomplete="off"
    document.querySelectorAll('autocomplete-select input, autocomplete-light input, autocomplete-select-input input').forEach(function (el) {
      el.setAttribute('autocomplete', 'off');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }

  // Also support dynamic form additions
  if (window.MutationObserver) {
    var observer = new MutationObserver(function () {
      initAll();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  window.initJalAutocomplete = initAll;
})();
