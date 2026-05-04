/* Floating chat drawer.
 *
 * Per docs/chat/03-floating-chat-panel.md. Loaded on every authenticated
 * page (see base.html). Owns: launcher button click, drawer open/close,
 * Esc + Ctrl+/ shortcuts, fetch-and-swap of the drawer body, hand-off
 * to window.AwareChat.init() once a session partial is mounted.
 *
 * Persists the active session id in localStorage so reopening returns
 * to the same conversation.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'aware.chat.activeSessionId';

  var launcher = document.getElementById('chat-launcher');
  var drawer = document.getElementById('chat-drawer');
  var backdrop = document.querySelector('[data-chat-drawer-backdrop]');
  if (!launcher || !drawer || !backdrop) return;

  var body = drawer.querySelector('[data-chat-drawer-body]');
  var lastFocus = null;
  var currentSessionRoot = null;

  function readActiveId() {
    try {
      var v = window.localStorage.getItem(STORAGE_KEY);
      var n = v ? parseInt(v, 10) : NaN;
      return isNaN(n) ? null : n;
    } catch (_) { return null; }
  }

  function writeActiveId(id) {
    try {
      if (id == null) window.localStorage.removeItem(STORAGE_KEY);
      else window.localStorage.setItem(STORAGE_KEY, String(id));
    } catch (_) {}
  }

  function disposeCurrent() {
    if (currentSessionRoot && window.AwareChat) {
      window.AwareChat.dispose(currentSessionRoot);
    }
    currentSessionRoot = null;
  }

  function executeInlineScripts(rootEl) {
    // innerHTML doesn't run <script> tags. Re-create them so HTMX
    // attributes inside the partial are processed.
    rootEl.querySelectorAll('script').forEach(function (old) {
      var s = document.createElement('script');
      for (var i = 0; i < old.attributes.length; i++) {
        s.setAttribute(old.attributes[i].name, old.attributes[i].value);
      }
      s.text = old.textContent;
      old.parentNode.replaceChild(s, old);
    });
  }

  function processHtmx(rootEl) {
    if (window.htmx && typeof window.htmx.process === 'function') {
      try { window.htmx.process(rootEl); } catch (_) {}
    }
  }

  function loadDrawer(sessionId, opts) {
    opts = opts || {};
    var url = '/api/chat/drawer';
    var params = [];
    if (sessionId != null) params.push('session_id=' + encodeURIComponent(sessionId));
    if (opts.initial) params.push('initial=' + encodeURIComponent(opts.initial));
    if (params.length) url += '?' + params.join('&');

    body.setAttribute('aria-busy', 'true');
    return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (resp) {
        if (resp.status === 404 && sessionId != null) {
          // Session was archived or deleted — fall back to the picker.
          writeActiveId(null);
          return loadDrawer(null, {});
        }
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.text();
      })
      .then(function (html) {
        if (typeof html !== 'string') return; // already recursed
        disposeCurrent();
        body.innerHTML = html;
        executeInlineScripts(body);
        processHtmx(body);
        body.dataset.loaded = '1';

        var sessionRoot = body.querySelector('.chat-session');
        if (sessionRoot && window.AwareChat) {
          window.AwareChat.init(sessionRoot);
          currentSessionRoot = sessionRoot;
          var sid = parseInt(sessionRoot.dataset.sessionId, 10);
          if (!isNaN(sid)) writeActiveId(sid);
          if (drawer.classList.contains('chat-drawer-open')) {
            window.AwareChat.focusInput(sessionRoot);
          }
        } else {
          wirePicker();
        }
      })
      .catch(function (err) {
        body.innerHTML =
          '<div class="muted" style="padding:24px">Failed to load chat: '
          + (err && err.message ? err.message : 'unknown error') + '</div>';
        // Leave body.dataset.loaded unset so a re-open retries.
      })
      .then(function () {
        body.removeAttribute('aria-busy');
      });
  }

  function wirePicker() {
    var newInput = body.querySelector('[data-chat-drawer-new-input]');
    var newSubmit = body.querySelector('[data-chat-drawer-new-submit]');
    var examples = body.querySelectorAll('.chat-example');
    examples.forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (newInput) {
          newInput.value = btn.dataset.prompt || '';
          newInput.focus();
        }
      });
    });
    function startNew() {
      var text = (newInput && newInput.value || '').trim();
      if (!text) {
        if (newInput) newInput.focus();
        return;
      }
      newSubmit.disabled = true;
      // Mirror the page-mode flow: create an empty session, then hand
      // the typed prompt to chat.js so the SSE turn fires from the
      // browser. Avoids the "first turn never fires" race.
      fetch('/api/chat/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ title: 'New chat' }),
      })
        .then(function (r) {
          if (!r.ok) return r.text().then(function (t) { throw new Error(t || 'HTTP ' + r.status); });
          return r.json();
        })
        .then(function (data) {
          loadDrawer(data.id, { initial: text });
        })
        .catch(function (err) {
          newSubmit.disabled = false;
          window.alert('Could not start chat: ' + (err && err.message ? err.message : err));
        });
    }
    if (newSubmit) newSubmit.addEventListener('click', startNew);
    if (newInput) newInput.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        startNew();
      }
    });
  }

  function open(opts) {
    opts = opts || {};
    if (drawer.classList.contains('chat-drawer-open')) return;
    lastFocus = document.activeElement;
    drawer.classList.add('chat-drawer-open');
    drawer.setAttribute('aria-hidden', 'false');
    backdrop.hidden = false;
    document.documentElement.classList.add('chat-drawer-active');
    launcher.setAttribute('aria-expanded', 'true');
    launcher.classList.add('is-hidden');

    // First open of this page-load → load content. On subsequent opens
    // we keep the existing DOM (preserves any in-flight stream).
    if (!body.dataset.loaded) {
      var preferred = opts.sessionId != null ? opts.sessionId : readActiveId();
      loadDrawer(preferred, opts);
    } else if (currentSessionRoot && window.AwareChat) {
      window.AwareChat.focusInput(currentSessionRoot);
    } else {
      var pickerInput = body.querySelector('[data-chat-drawer-new-input]');
      if (pickerInput) pickerInput.focus();
    }
  }

  function close() {
    if (!drawer.classList.contains('chat-drawer-open')) return;
    drawer.classList.remove('chat-drawer-open');
    drawer.setAttribute('aria-hidden', 'true');
    backdrop.hidden = true;
    document.documentElement.classList.remove('chat-drawer-active');
    launcher.setAttribute('aria-expanded', 'false');
    launcher.classList.remove('is-hidden');
    if (lastFocus && typeof lastFocus.focus === 'function') {
      try { lastFocus.focus(); } catch (_) {}
    }
  }

  function toggle() {
    if (drawer.classList.contains('chat-drawer-open')) close();
    else open();
  }

  // ---- Listeners ----

  launcher.addEventListener('click', function () { open(); });
  backdrop.addEventListener('click', close);

  // Delegated clicks inside the drawer body.
  drawer.addEventListener('click', function (e) {
    if (e.target.closest('[data-chat-drawer-close]')) {
      e.preventDefault();
      close();
      return;
    }
    if (e.target.closest('[data-chat-drawer-pick]')) {
      e.preventDefault();
      // Switch to picker; don't clear active id (so reopen returns
      // to the conversation if the user changes their mind).
      body.dataset.loaded = '1';
      loadDrawer(null, {});
      return;
    }
    var sessLink = e.target.closest('[data-chat-drawer-open-session]');
    if (sessLink) {
      e.preventDefault();
      var sid = parseInt(sessLink.dataset.chatDrawerOpenSession, 10);
      if (!isNaN(sid)) loadDrawer(sid, {});
    }
  });

  // Keyboard: Esc closes; Ctrl/Cmd+/ toggles.
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && drawer.classList.contains('chat-drawer-open')) {
      // Don't swallow Esc inside <input> for things like cancelling
      // an in-flight prompt — the input already drops focus, so
      // closing the drawer is the right next step.
      close();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === '/') {
      e.preventDefault();
      toggle();
    }
  });
})();
