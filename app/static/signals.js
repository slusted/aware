// User signal tracking for the stream. Emits view / dwell / open events
// to /api/signals/events/batch. See docs/ranker/01-signal-log.md.
//
// Contract:
//   - `view`  fires once per card per page load, after ≥50% visible for
//             ≥500ms. Re-entering the viewport does NOT re-fire.
//   - `dwell` fires when the card leaves the viewport after a `view`, with
//             `value = dwell_ms`. Below the 500ms noise floor → dropped.
//   - `open`  fires on click of the card's title link (target="_blank"
//             external URL).
//
// Events are queued and flushed in batches: periodically, on visibility
// change, on beforeunload (via sendBeacon), and when the queue fills up.
// HTMX re-observes new cards after every swap.

(() => {
  'use strict';

  const BATCH_ENDPOINT = '/api/signals/events/batch';
  const FLUSH_INTERVAL_MS = 10_000;
  const FLUSH_AT_QUEUE_SIZE = 50;
  const VIEW_VISIBLE_THRESHOLD = 0.5;
  const VIEW_DWELL_MIN_MS = 500;
  const MAX_BATCH = 100;

  /** @type {Array<{event_type:string, source:string, finding_id:number, value?:number, meta?:object}>} */
  const queue = [];

  // Per-card state. Keyed by finding_id so re-renders of the same card
  // (e.g. after pin toggle) don't double-count.
  //
  //   emitted: true once `view` has fired this page load
  //   inViewSince: timestamp when card crossed into view (null when out)
  //   viewTs: when we actually emitted `view` (for dwell duration)
  const cardState = new Map();

  function enqueue(event) {
    queue.push(event);
    if (queue.length >= FLUSH_AT_QUEUE_SIZE) {
      flush();
    }
  }

  // Optimistic read-state flip: the moment we emit `view`, demote the card
  // from `state-new` to `state-seen` in the DOM so the "NEW" indicator fades
  // without a server round-trip. The batch endpoint writes the SignalView row
  // within FLUSH_INTERVAL_MS; if the POST fails the card reverts on reload —
  // a read-state flicker is cheaper than blocking the UI on telemetry.
  function markCardSeen(findingId) {
    const card = document.getElementById('card-' + findingId);
    if (!card) return;
    if (card.classList.contains('state-new')) {
      card.classList.remove('state-new');
      card.classList.add('state-seen');
    }
  }

  function flush() {
    if (queue.length === 0) return;
    // Drain up to MAX_BATCH; anything over stays for the next flush.
    const batch = queue.splice(0, MAX_BATCH);
    try {
      fetch(BATCH_ENDPOINT, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ events: batch }),
        keepalive: true,
      }).catch(() => {
        // Drop on failure — stream rendering is not coupled to telemetry
        // reliability. If this becomes a problem we'll add retry.
      });
    } catch (_) { /* noop */ }
  }

  function flushBeacon() {
    // Navigations / tab-hide: fire-and-forget with sendBeacon so the
    // browser guarantees delivery even as the page unloads. Falls back
    // to a keepalive fetch when sendBeacon is unavailable.
    if (queue.length === 0) return;
    const batch = queue.splice(0, MAX_BATCH);
    const body = JSON.stringify({ events: batch });
    if (navigator.sendBeacon) {
      const blob = new Blob([body], { type: 'application/json' });
      navigator.sendBeacon(BATCH_ENDPOINT, blob);
    } else {
      try {
        fetch(BATCH_ENDPOINT, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body,
          keepalive: true,
        });
      } catch (_) { /* noop */ }
    }
  }

  function onIntersect(entries) {
    const now = performance.now();
    for (const entry of entries) {
      const fid = Number(entry.target.dataset.findingId);
      if (!fid) continue;
      let st = cardState.get(fid);
      if (!st) {
        st = { emitted: false, inViewSince: null, viewTs: null };
        cardState.set(fid, st);
      }
      if (entry.isIntersecting && entry.intersectionRatio >= VIEW_VISIBLE_THRESHOLD) {
        if (st.inViewSince === null) st.inViewSince = now;
        // Delay the `view` emission by VIEW_DWELL_MIN_MS of sustained
        // visibility — avoids flash-scroll inflation.
        if (!st.emitted) {
          setTimeout(() => {
            const s = cardState.get(fid);
            // Must still be in view, still not emitted, and must have been
            // in view for the full threshold (inViewSince can't have reset).
            if (!s || s.emitted || s.inViewSince === null) return;
            if (performance.now() - s.inViewSince < VIEW_DWELL_MIN_MS) return;
            s.emitted = true;
            s.viewTs = performance.now();
            enqueue({ event_type: 'view', source: 'stream', finding_id: fid });
            markCardSeen(fid);
          }, VIEW_DWELL_MIN_MS);
        }
      } else {
        // Leaving the viewport — if we already emitted view, finalize dwell.
        if (st.emitted && st.viewTs !== null) {
          const dwellMs = Math.round(performance.now() - st.viewTs);
          if (dwellMs >= VIEW_DWELL_MIN_MS) {
            enqueue({
              event_type: 'dwell',
              source: 'stream',
              finding_id: fid,
              value: dwellMs,
            });
          }
          // Reset dwell timer but keep `emitted` true — we don't re-emit
          // view on the same card within one page load.
          st.viewTs = null;
        }
        st.inViewSince = null;
      }
    }
  }

  const observer = new IntersectionObserver(onIntersect, {
    threshold: [0, VIEW_VISIBLE_THRESHOLD, 1.0],
  });

  function observeAllCards() {
    for (const card of document.querySelectorAll('.signal-card[data-finding-id]')) {
      // IntersectionObserver de-dupes observed targets automatically —
      // calling observe() on an already-observed node is a noop.
      observer.observe(card);
    }
  }

  // Click handler for the external title link: fires `open`. Delegated so
  // we don't rebind on every HTMX swap. Using click (not the auxclick /
  // middle-click paths) because the link has target="_blank" and the
  // browser handles the navigation itself — we just need to log.
  document.addEventListener('click', (e) => {
    const link = e.target.closest('.signal-card .signal-title');
    if (!link) return;
    const card = link.closest('.signal-card[data-finding-id]');
    if (!card) return;
    const fid = Number(card.dataset.findingId);
    if (!fid) return;
    enqueue({
      event_type: 'open',
      source: 'stream',
      finding_id: fid,
      meta: { target: 'url' },
    });
  });

  // Initial attach + re-attach after HTMX swaps in new cards.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', observeAllCards);
  } else {
    observeAllCards();
  }
  document.addEventListener('htmx:afterSettle', observeAllCards);

  // Periodic flush so events don't sit in memory forever on a long-open tab.
  setInterval(flush, FLUSH_INTERVAL_MS);

  // Tab hidden = user likely navigating away or switching context. Flush
  // dwell events for any cards currently in view before we lose them.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      // Finalize dwell for any still-in-view cards so we don't lose the
      // session when the user switches tabs.
      const now = performance.now();
      for (const [fid, st] of cardState.entries()) {
        if (st.emitted && st.viewTs !== null) {
          const dwellMs = Math.round(now - st.viewTs);
          if (dwellMs >= VIEW_DWELL_MIN_MS) {
            enqueue({
              event_type: 'dwell',
              source: 'stream',
              finding_id: fid,
              value: dwellMs,
            });
          }
          st.viewTs = null;
        }
      }
      flushBeacon();
    }
  });

  window.addEventListener('beforeunload', flushBeacon);
})();
