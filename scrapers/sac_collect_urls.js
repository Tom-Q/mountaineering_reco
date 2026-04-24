/**
 * Collect SAC alpine tour route IDs via browser console.
 *
 * How to use:
 *   1. Open https://www.sac-cas.ch/en/huts-and-tours/sac-route-portal/?discipline=alpine_tour
 *      in your browser and wait for routes to load.
 *   2. Open DevTools → Console (F12 → Console tab)
 *   3. Paste this entire script and press Enter — it patches fetch/XHR immediately.
 *   4. Manually scroll through / paginate all results in the portal UI.
 *      The script captures routes both from network intercepts and from the DOM.
 *   5. When you've reached the end, call: window.__sacCollect.finish()
 *      It will download sac_route_ids.txt and log a summary.
 *
 * The script collects route IDs two ways:
 *   A) Network intercept — captures API responses as the portal fetches them.
 *   B) DOM scan — finds anchor tags whose href matches /DIGITS/alpine_tour.
 *
 * If you see API calls in the console log, note the URL pattern — we can then
 * call that API directly from Python and skip the browser entirely.
 */

(function () {
  const collected = new Set();   // route IDs (numeric strings)
  const apiCalls = [];           // log of all fetch/XHR URLs the portal makes

  // ── DOM scanner: call at any time to harvest visible links ────────────────
  function scanDOM() {
    const before = collected.size;
    document.querySelectorAll('a[href]').forEach(a => {
      const m = a.getAttribute('href').match(/\/(\d{3,6})\/(alpine_tour|hochtouren)/i);
      if (m) collected.add(m[1]);
    });
    const added = collected.size - before;
    if (added > 0) console.log(`[SAC] DOM scan: +${added} IDs (total ${collected.size})`);
    return added;
  }

  // ── Fetch interceptor ─────────────────────────────────────────────────────
  const _origFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url ?? '';
    const resp = await _origFetch(...args);

    // Only log SAC-internal calls, skip fonts/images/etc.
    if (url.includes('sac-cas.ch') || url.startsWith('/')) {
      apiCalls.push(url);
      console.log('[SAC] fetch →', url);

      // Try to parse JSON and extract IDs from it
      try {
        const clone = resp.clone();
        const ct = resp.headers.get('content-type') || '';
        if (ct.includes('json')) {
          clone.json().then(data => {
            extractIdsFromJson(data, url);
          }).catch(() => {});
        }
      } catch (_) {}
    }
    return resp;
  };

  // ── XHR interceptor ───────────────────────────────────────────────────────
  const _XHROpen = XMLHttpRequest.prototype.open;
  const _XHRSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._sacUrl = url;
    return _XHROpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    const url = this._sacUrl || '';
    if (url.includes('sac-cas.ch') || url.startsWith('/')) {
      apiCalls.push(url);
      console.log('[SAC] XHR →', url);
      this.addEventListener('load', () => {
        try {
          const data = JSON.parse(this.responseText);
          extractIdsFromJson(data, url);
        } catch (_) {}
      });
    }
    return _XHRSend.call(this, ...args);
  };

  // ── JSON ID extractor — walks any JSON structure looking for route IDs ────
  function extractIdsFromJson(obj, sourceUrl, depth = 0) {
    if (depth > 6 || obj === null) return;
    if (typeof obj === 'string') {
      const m = obj.match(/\/(\d{3,6})\/(alpine_tour|hochtouren)/i);
      if (m) { collected.add(m[1]); return; }
    }
    if (typeof obj !== 'object') return;

    // Common field names SAC portals use for route/tour IDs
    const idFields = ['uid', 'id', 'routeId', 'tourId', 'pid', 'route_id'];
    for (const k of idFields) {
      if (obj[k] && /^\d{3,6}$/.test(String(obj[k]))) {
        collected.add(String(obj[k]));
      }
    }
    // Also look for URL-shaped strings inside arrays/objects
    if (Array.isArray(obj)) {
      obj.forEach(item => extractIdsFromJson(item, sourceUrl, depth + 1));
    } else {
      Object.values(obj).forEach(v => extractIdsFromJson(v, sourceUrl, depth + 1));
    }
  }

  // ── Auto-scroll helper: scrolls to bottom and back to trigger lazy load ──
  async function autoScroll() {
    console.log('[SAC] auto-scrolling to trigger lazy load...');
    let lastCount = 0;
    for (let i = 0; i < 30; i++) {
      window.scrollTo(0, document.body.scrollHeight);
      await new Promise(r => setTimeout(r, 800));
      scanDOM();
      if (collected.size === lastCount) break;  // nothing new, stop
      lastCount = collected.size;
    }
    window.scrollTo(0, 0);
    console.log('[SAC] auto-scroll done. IDs so far:', collected.size);
  }

  // ── Finish: download results ──────────────────────────────────────────────
  function finish() {
    scanDOM(); // final DOM sweep

    console.log(`\n[SAC] ═══════════════════════════════════`);
    console.log(`[SAC] Total route IDs collected: ${collected.size}`);
    console.log(`[SAC] API calls observed: ${apiCalls.length}`);
    if (apiCalls.length > 0) {
      console.log(`[SAC] API URLs (useful for direct scraping):`);
      [...new Set(apiCalls)].forEach(u => console.log('  ', u));
    }
    console.log(`[SAC] ═══════════════════════════════════\n`);

    if (collected.size === 0) {
      console.warn('[SAC] No IDs found. Try scrolling through results first, then call finish() again.');
      return;
    }

    const ids = [...collected].sort((a, b) => Number(a) - Number(b));
    const lines = ids.map(id => `https://www.sac-cas.ch/de/huetten-und-touren/sac-tourenportal/${id}/alpine_tour`);

    const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'sac_route_urls.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);

    console.log(`[SAC] Downloaded sac_route_urls.txt (${ids.length} URLs)`);
    console.log('[SAC] Move it to: data/sac_route_urls.txt');
  }

  // ── Expose public API ─────────────────────────────────────────────────────
  window.__sacCollect = { finish, scanDOM, autoScroll, collected, apiCalls };

  // Initial DOM scan + auto-scroll
  scanDOM();
  autoScroll();

  console.log('[SAC] Collector active. Scroll/paginate through all routes, then call:');
  console.log('      window.__sacCollect.finish()');
})();
