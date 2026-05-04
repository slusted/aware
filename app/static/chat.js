/* Chat SSE handler.
 *
 * Vanilla JS, no build step. Two responsibilities:
 *   - Render the assistant turn in real time (parsing the fetch-stream as SSE).
 *   - Wire up Confirm/Cancel buttons on tool-use cards and resume the turn.
 *
 * Mounts on a root element that carries `data-session-id`. Two callers:
 *   - chat_session.html (full-page mode, root has data-mode="page")
 *   - the floating drawer (root has data-mode="drawer", see chat-drawer.js)
 *
 * Exposes window.AwareChat = { init(rootEl), dispose(rootEl) }.
 * init() is idempotent per root. dispose() cancels the in-flight stream
 * (AbortController) and detaches all listeners so the drawer can swap
 * sessions or unmount cleanly.
 *
 * We use fetch + ReadableStream rather than EventSource because the SSE
 * endpoint is a POST (EventSource is GET-only).
 */
(function () {
  'use strict';

  var instances = new WeakMap();

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function renderMarkdown(text) {
    if (window.marked && typeof window.marked.parse === 'function') {
      try { return window.marked.parse(text || ''); }
      catch (_) { return escapeHtml(text); }
    }
    return escapeHtml(text);
  }

  function createInstance(rootEl) {
    var sessionId = rootEl.dataset.sessionId;
    var thread = rootEl.querySelector('.chat-thread');
    var form = rootEl.querySelector('.chat-input-form');
    var input = form ? form.querySelector('textarea') : null;
    var titleEl = rootEl.querySelector('.chat-title');
    var renameBtn = rootEl.querySelector('.chat-rename-btn');
    var costEl = rootEl.querySelector('.chat-cost');

    if (!thread) return null;

    // Track the trailing assistant bubble that text_delta events write into.
    var currentAssistantBubble = null;
    var currentAssistantBuffer = '';
    // tool_use_id → element in the DOM, so confirmation/result events can
    // mutate the right card without a page-wide query.
    var toolCards = {};
    var streamInFlight = false;
    var abortController = null;

    function scrollToBottom() {
      window.requestAnimationFrame(function () {
        // In drawer mode the scroll container is the thread itself; in
        // page mode it's the window.
        if (rootEl.dataset.mode === 'drawer') {
          thread.scrollTop = thread.scrollHeight;
        } else {
          window.scrollTo(0, document.body.scrollHeight);
        }
      });
    }

    function hydrateMarkdown() {
      rootEl.querySelectorAll('.chat-md').forEach(function (el) {
        el.innerHTML = renderMarkdown(el.dataset.mdSrc || el.textContent);
      });
    }

    function appendUserBubble(text) {
      var wrap = document.createElement('div');
      wrap.className = 'chat-msg chat-msg-user';
      wrap.innerHTML = '<div class="chat-bubble chat-bubble-user"></div>';
      wrap.querySelector('.chat-bubble').textContent = text;
      thread.appendChild(wrap);
      scrollToBottom();
    }

    function ensureAssistantBubble() {
      if (currentAssistantBubble) return currentAssistantBubble;
      var wrap = document.createElement('div');
      wrap.className = 'chat-msg chat-msg-assistant';
      wrap.innerHTML = '<div class="chat-bubble chat-bubble-assistant">'
        + '<div class="chat-md"></div></div>';
      thread.appendChild(wrap);
      currentAssistantBubble = wrap.querySelector('.chat-md');
      currentAssistantBuffer = '';
      scrollToBottom();
      return currentAssistantBubble;
    }

    function appendToolUseCard(payload) {
      var status = payload.confirmation_status || 'auto';
      var wrap = document.createElement('div');
      wrap.className = 'chat-tool-card chat-tool-' + status;
      wrap.dataset.toolUseId = payload.id;
      wrap.dataset.toolName = payload.name;

      if (payload.requires_confirmation && status === 'pending') {
        var summary = payload.confirmation_summary || ('Run ' + payload.name + '?');
        wrap.innerHTML =
          '<div class="chat-tool-confirm">'
          + '<div class="chat-tool-confirm-text"></div>'
          + '<div class="chat-tool-confirm-actions">'
          + '<button type="button" class="btn chat-confirm-btn" data-decision="confirm">Confirm</button>'
          + '<button type="button" class="btn-ghost chat-confirm-btn" data-decision="cancel">Cancel</button>'
          + '</div></div>';
        wrap.querySelector('.chat-tool-confirm-text').textContent = summary;
      } else {
        wrap.innerHTML =
          '<details class="chat-tool-details"><summary>'
          + '<span class="chat-tool-name">⚙ ' + escapeHtml(payload.name) + '</span>'
          + '</summary><pre class="chat-tool-json"></pre></details>';
        wrap.querySelector('.chat-tool-json').textContent =
          JSON.stringify(payload.input || {}, null, 2);
      }
      thread.appendChild(wrap);
      toolCards[payload.id] = wrap;
      currentAssistantBubble = null;
      scrollToBottom();
    }

    function appendToolResultCard(payload) {
      var wrap = document.createElement('div');
      wrap.className = 'chat-tool-result' + (payload.is_error ? ' chat-tool-error' : '');
      wrap.dataset.toolUseId = payload.tool_use_id;
      wrap.innerHTML =
        '<details class="chat-tool-details"><summary>'
        + '<span class="chat-tool-name">↳ result</span>'
        + (payload.is_error ? ' <span style="color:var(--err)">· error</span>' : '')
        + '</summary><pre class="chat-tool-json"></pre></details>';
      var pre = wrap.querySelector('.chat-tool-json');
      try { pre.textContent = JSON.stringify(payload.output, null, 2); }
      catch (_) { pre.textContent = String(payload.output); }
      thread.appendChild(wrap);
      currentAssistantBubble = null;
      scrollToBottom();
    }

    function appendErrorBubble(text) {
      var wrap = document.createElement('div');
      wrap.className = 'chat-msg chat-msg-error';
      wrap.innerHTML = '<div class="chat-bubble chat-bubble-error"></div>';
      wrap.querySelector('.chat-bubble').textContent = text;
      thread.appendChild(wrap);
      scrollToBottom();
    }

    function setStreamingState(streaming) {
      streamInFlight = streaming;
      if (input) input.disabled = streaming;
      var submitBtn = form ? form.querySelector('button[type="submit"]') : null;
      if (submitBtn) submitBtn.disabled = streaming;
    }

    function updateCost(total) {
      if (costEl && typeof total === 'number' && !isNaN(total)) {
        costEl.textContent = '$' + total.toFixed(4);
      }
    }

    // ---------------- SSE parsing ----------------
    function parseSSEFrames(buffer) {
      var frames = [];
      var idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        var raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        var ev = 'message';
        var data = '';
        raw.split('\n').forEach(function (line) {
          if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
          else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
        });
        var payload = null;
        if (data) {
          try { payload = JSON.parse(data); }
          catch (_) { payload = { _raw: data }; }
        }
        frames.push({ event: ev, data: payload });
      }
      return { frames: frames, leftover: buffer };
    }

    function handleEvent(ev, data) {
      if (!data) return;
      switch (ev) {
        case 'turn_start':
          currentAssistantBubble = null;
          currentAssistantBuffer = '';
          break;
        case 'text_delta':
          var bubble = ensureAssistantBubble();
          currentAssistantBuffer += data.text || '';
          bubble.innerHTML = renderMarkdown(currentAssistantBuffer);
          scrollToBottom();
          break;
        case 'tool_use':
          appendToolUseCard(data);
          break;
        case 'tool_running':
          break;
        case 'tool_result':
          appendToolResultCard(data);
          break;
        case 'usage':
          updateCost(data.session_total_cost_usd);
          currentAssistantBubble = null;
          currentAssistantBuffer = '';
          break;
        case 'cost_warning':
          if (costEl) costEl.title =
            'Cost > $' + data.warn_at + ' soft warn (hard cap $' + data.hard_at + ')';
          break;
        case 'confirmation_pending':
        case 'waiting_for_confirmation':
          break;
        case 'turn_end':
          updateCost(data.session_total_cost_usd);
          break;
        case 'error':
          appendErrorBubble(data.message || 'Error');
          break;
        default:
          break;
      }
    }

    function streamFrom(url, body) {
      if (streamInFlight) return Promise.resolve();
      setStreamingState(true);
      abortController = new AbortController();
      return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin',
        signal: abortController.signal,
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (txt) {
            appendErrorBubble('HTTP ' + resp.status + ': ' + txt.slice(0, 200));
          });
        }
        var ct = resp.headers.get('content-type') || '';
        if (ct.indexOf('text/event-stream') === -1) {
          return resp.text().then(function (txt) {
            appendErrorBubble(txt || 'Unexpected response.');
          });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder('utf-8');
        var buffer = '';
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              if (buffer.length) {
                var parsed = parseSSEFrames(buffer + '\n\n');
                parsed.frames.forEach(function (f) { handleEvent(f.event, f.data); });
              }
              return;
            }
            buffer += decoder.decode(chunk.value, { stream: true });
            var parsed = parseSSEFrames(buffer);
            buffer = parsed.leftover;
            parsed.frames.forEach(function (f) { handleEvent(f.event, f.data); });
            return pump();
          });
        }
        return pump();
      }).catch(function (err) {
        if (err && err.name === 'AbortError') return;
        appendErrorBubble('Network error: ' + err);
      }).then(function () {
        abortController = null;
        setStreamingState(false);
      });
    }

    // ---------------- Listeners ----------------

    function onSubmit(e) {
      e.preventDefault();
      if (streamInFlight) return;
      var text = (input && input.value || '').trim();
      if (!text) return;
      input.value = '';
      appendUserBubble(text);
      streamFrom('/api/chat/' + sessionId + '/messages', { text: text });
    }

    function onKeydown(e) {
      // Cmd/Ctrl+Enter submits.
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        if (form) form.requestSubmit();
      }
    }

    function onThreadClick(e) {
      var btn = e.target.closest('.chat-confirm-btn');
      if (!btn) return;
      var card = btn.closest('.chat-tool-card');
      if (!card) return;
      var toolUseId = card.dataset.toolUseId;
      var decision = btn.dataset.decision === 'confirm' ? 'confirm' : 'cancel';

      card.querySelectorAll('.chat-confirm-btn').forEach(function (b) {
        b.disabled = true;
      });
      card.classList.remove('chat-tool-pending');
      card.classList.add(decision === 'confirm' ? 'chat-tool-confirmed' : 'chat-tool-cancelled');

      var confirmEl = card.querySelector('.chat-tool-confirm');
      if (confirmEl) {
        confirmEl.innerHTML = '<div class="chat-tool-confirm-text muted">'
          + (decision === 'confirm' ? 'Confirmed — running…' : 'Cancelled.')
          + '</div>';
      }

      streamFrom('/api/chat/' + sessionId + '/confirm', {
        confirmations: [{ tool_use_id: toolUseId, decision: decision }],
      });
    }

    function startRename() {
      if (!titleEl) return;
      var current = titleEl.dataset.title || titleEl.textContent;
      var next = window.prompt('Rename this conversation:', current);
      if (next == null) return;
      next = next.trim();
      if (!next || next === current) return;
      fetch('/api/chat/' + sessionId + '/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: next }),
        credentials: 'same-origin',
      }).then(function (r) {
        if (r.ok) { titleEl.textContent = next; titleEl.dataset.title = next; }
      });
    }

    if (form) form.addEventListener('submit', onSubmit);
    if (input) input.addEventListener('keydown', onKeydown);
    thread.addEventListener('click', onThreadClick);
    if (renameBtn) renameBtn.addEventListener('click', startRename);
    if (titleEl) titleEl.addEventListener('dblclick', startRename);

    hydrateMarkdown();
    scrollToBottom();

    // Page-mode only: /chat/new redirects with ?initial=… so the first
    // turn fires from the browser (same request as Send).
    if (rootEl.dataset.mode === 'page') {
      var params = new URLSearchParams(window.location.search);
      var initial = params.get('initial');
      if (initial) {
        history.replaceState(null, '', '/chat/' + sessionId);
        if (input) {
          input.value = initial;
          window.setTimeout(function () { if (form) form.requestSubmit(); }, 0);
        }
      }
    }

    // Drawer-mode: data-initial on the root submits the first turn after mount.
    if (rootEl.dataset.mode === 'drawer' && rootEl.dataset.initial) {
      var initialText = rootEl.dataset.initial;
      rootEl.removeAttribute('data-initial');
      if (input) {
        input.value = initialText;
        window.setTimeout(function () { if (form) form.requestSubmit(); }, 0);
      }
    }

    function cleanup() {
      if (abortController) {
        try { abortController.abort(); } catch (_) {}
        abortController = null;
      }
      if (form) form.removeEventListener('submit', onSubmit);
      if (input) input.removeEventListener('keydown', onKeydown);
      thread.removeEventListener('click', onThreadClick);
      if (renameBtn) renameBtn.removeEventListener('click', startRename);
      if (titleEl) titleEl.removeEventListener('dblclick', startRename);
    }

    return { cleanup: cleanup, focusInput: function () { if (input) input.focus(); } };
  }

  function init(rootEl) {
    if (!rootEl || instances.has(rootEl)) return;
    var inst = createInstance(rootEl);
    if (inst) instances.set(rootEl, inst);
  }

  function dispose(rootEl) {
    if (!rootEl) return;
    var inst = instances.get(rootEl);
    if (!inst) return;
    inst.cleanup();
    instances.delete(rootEl);
  }

  function focusInput(rootEl) {
    var inst = instances.get(rootEl);
    if (inst) inst.focusInput();
  }

  window.AwareChat = { init: init, dispose: dispose, focusInput: focusInput };

  // Auto-init any page-mode chat on the current page.
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.chat-session[data-mode="page"]').forEach(init);
  });
})();
