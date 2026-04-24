/**
 * Collect all SummitPost mountaineering route URLs via browser console.
 *
 * How to use:
 *   1. Open any page on summitpost.org in your browser (you need the Cloudflare cookie)
 *   2. Open DevTools → Console (F12 → Console tab)
 *   3. Paste this entire script and press Enter
 *   4. Wait ~2 minutes while it pages through 97 pages (500ms delay each)
 *   5. When done, it prints all URLs and copies them to your clipboard
 *   6. Paste clipboard into data/route_urls.txt  (or use the copy it prints)
 *
 * If clipboard copy fails (some browsers block it without a user gesture),
 * the full list is also logged — just select it in the console and copy manually.
 */
(async () => {
  const BASE_URL = 'https://www.summitpost.org/object_list.php'
    + '?distance_lat_2=50.879&distance_lon_2=4.3525'
    + '&search_in=name_only&route_type_2=Mountaineering'
    + '&map_2=1&order_type=DESC&object_type=2'
    + '&orderby=object_scores.hits';

  const TOTAL_PAGES = 97;
  const DELAY_MS = 500;   // be polite; 97 × 500ms ≈ 50s total

  const allUrls = [];
  const parser = new DOMParser();
  let errors = 0;

  for (let page = 1; page <= TOTAL_PAGES; page++) {
    const url = `${BASE_URL}&page=${page}`;
    try {
      const resp = await fetch(url, { credentials: 'include' });
      if (!resp.ok) {
        console.warn(`Page ${page}: HTTP ${resp.status}`);
        errors++;
      } else {
        const html = await resp.text();
        const doc = parser.parseFromString(html, 'text/html');
        const links = doc.querySelectorAll('p.cci-title > a');
        links.forEach(a => {
          const href = a.getAttribute('href');
          if (href) {
            const full = href.startsWith('http')
              ? href
              : 'https://www.summitpost.org' + href;
            allUrls.push(full);
          }
        });
        console.log(`Page ${page}/${TOTAL_PAGES}: ${links.length} links (running total: ${allUrls.length})`);
      }
    } catch (e) {
      console.error(`Page ${page} error:`, e);
      errors++;
    }
    await new Promise(r => setTimeout(r, DELAY_MS));
  }

  console.log(`\n✓ Done. ${allUrls.length} URLs collected, ${errors} page errors.`);

  // Download as a file — works even when clipboard/console is limited
  const text = allUrls.join('\n');
  const blob = new Blob([text], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'route_urls.txt';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  console.log(`\nDownloaded route_urls.txt with ${allUrls.length} URLs. Move it to data/route_urls.txt`);
})();
