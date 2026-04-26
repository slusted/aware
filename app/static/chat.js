/* Chat SSE handler.
 *
 * One file, vanilla JS, no build step. Two responsibilities:
 *   - Render the assistant turn in real time (parsing the fetch-stream as SSE).
 *   - Wire up Confirm/Cancel buttons on tool-use cards and resume the turn.
 *
 * We use fetch + ReadableStream rather than EventSource because the SSE
 * endpoint is a POST (EventSource is GET-only).
 */
(function () {
  'use strict';

  var session = document.querySelector('.chat-session');
  if (!session) return;

  var sessionId = session.dataset.sessionId;
  var thread = document.getElementById('chat-thread');
  var form = document.getElementById('chat-input-form');
  var input = document.getElementById('chat-input');
  var titleEl = document.getElementById('chat-title');
  var renameBtn = document.getElementById('chat-rename-btn');
  var costEl = document.getElementById('chat-cost');

  // Track the trailing assistant bubble that text_delta events write into.
  var currentAssistantBubble = null;
  var currentAssistantBuffer = '';
  // tool_use_id → element in the DOM, so confirmation/result events can
  // mutate the right card without a page-wide query.
  var toolCards = {};
  var streamInFlight = false;

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

  // Hydrate any server-rendered markdown bodies on initial page load.
  function hydrateMarkdown() {
    document.querySelectorAll('.chat-md').forEach(function (el) {
      el.innerHTML = renderMarkdown(el.dataset.mdSrc || el.textContent);
    });
  }

  function scrollToBottom() {
    window.requestAnimationFrame(function () {
      window.scrollTo(0, document.body.scrollHeight);
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
    // The next text_delta should land in a NEW assistant bubble below
    // the tool card, not the previous bubble.
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
  // Server emits "event: <name>\ndata: <json>\n\n". We parse the byte stream
  // ourselves rather than reach for a library.
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
        // Reset trailing state — a new model call is starting.
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
        // Optional: visual hint while a read-tool fires. We just
        // acknowledge it by leaving the card in the DOM.
        break;
      case 'tool_result':
        appendToolResultCard(data);
        break;
      case 'usage':
        updateCost(data.session_total_cost_usd);
        // The next iteration of the model loop will start a new bubble.
        currentAssistantBubble = null;
        currentAssistantBuffer = '';
        break;
      case 'cost_warning':
        // Soft signal — surface as a transient line under the cost.
        if (costEl) costEl.title =
          'Cost > $' + data.warn_at + ' soft warn (hard cap $' + data.hard_at + ')';
        break;
      case 'confirmation_pending':
        // Stream ended; UI is now waiting for user click.
        break;
      case 'waiting_for_confirmation':
        // Resume blocked because some tools are still pending.
        break;
      case 'turn_end':
        updateCost(data.session_total_cost_usd);
        break;
      case 'error':
        appendErrorBubble(data.message || 'Error');
        break;
      default:
        /* ignore unknown events for forward compatibility */
        break;
    }
  }

  function streamFrom(url, body) {
    if (streamInFlight) return Promise.resolve();
    setStreamingState(true);
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'same-origin',
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
      appendErrorBubble('Network error: ' + err);
    }).then(function () {
      setStreamingState(false);
    });
  }

  // ---------------- Event handlers ----------------

  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (streamInFlight) return;
      var text = (input && input.value || '').trim();
      if (!text) return;
      input.value = '';
      appendUserBubble(text);
      streamFrom('/api/chat/' + sessionId + '/messages', { text: text });
    });

    // Cmd/Ctrl+Enter submits.
    if (input) {
      input.addEventListener('keydown', function (e) {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          e.preventDefault();
          form.requestSubmit();
        }
      });
    }
  }

  // Confirm/Cancel buttons inside tool cards (works for both server-rendered
  // pending cards and client-rendered ones).
  thread.addEventListener('click', function (e) {
    var btn = e.target.closest('.chat-confirm-btn');
    if (!btn) return;
    var card = btn.closest('.chat-tool-card');
    if (!card) return;
    var toolUseId = card.dataset.toolUseId;
    var decision = btn.dataset.decision === 'confirm' ? 'confirm' : 'cancel';

    // Lock the card so a double-click doesn't double-submit.
    card.querySelectorAll('.chat-confirm-btn').forEach(function (b) {
      b.disabled = true;
    });
    card.classList.remove('chat-tool-pending');
    card.classList.add(decision === 'confirm' ? 'chat-tool-confirmed' : 'chat-tool-cancelled');

    // Replace the confirm UI with a static line so the card retains the
    // decision when the page is reloaded later.
    var confirmEl = card.querySelector('.chat-tool-confirm');
    if (confirmEl) {
      confirmEl.innerHTML = '<div class="chat-tool-confirm-text muted">'
        + (decision === 'confirm' ? 'Confirmed — running…' : 'Cancelled.')
        + '</div>';
    }

    streamFrom('/api/chat/' + sessionId + '/confirm', {
      confirmations: [{ tool_use_id: toolUseId, decision: decision }],
    });
  });

  // Inline rename — clicks the title or the rename button.
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
  if (renameBtn) renameBtn.addEventListener('click', startRename);
  if (titleEl) titleEl.addEventListener('dblclick', startRename);

  // ---------------- Init ----------------
  hydrateMarkdown();

  // /chat/new redirects with ?initial=… so the first turn fires from the
  // browser (same request as Send) — guarantees the SSE stream is wired
  // by the time bytes land.
  var params = new URLSearchParams(window.location.search);
  var initial = params.get('initial');
  if (initial) {
    history.replaceState(null, '', '/chat/' + sessionId);
    if (input) {
      input.value = initial;
      // Defer so the form's submit handler is bound.
      window.setTimeout(function () { form.requestSubmit(); }, 0);
    }
  }
})();
