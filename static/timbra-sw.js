/* AMR Timbra – service worker (v4 Offline-First istantaneo)
 * Apre SEMPRE istantaneamente la pagina /timbra dalla memoria del telefono
 * (zero attesa di rete, zero schermo bianco su iPhone anche con 4G/Wi-Fi bloccato),
 * e aggiorna silenziosamente i file in background quando la connessione è stabile.
 * Le chiamate /api/* NON vengono mai messe in cache: le timbrature e le richieste
 * offline sono gestite dalla coda locale nella pagina (localStorage).
 */
const CACHE = 'amr-timbra-v6';
const ASSETS = ['/timbra', '/static/timbra-icon-192.png', '/static/timbra-icon-512.png', '/static/timbra-manifest.json',
  '/static/leaflet/leaflet.js', '/static/leaflet/leaflet.css'];

const FALLBACK_HTML = '<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AMR Timbrature</title><style>body{margin:0;min-height:100vh;background:#0d1117;color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}.sp{width:44px;height:44px;border:4px solid #232a3b;border-top-color:#DF3131;border-radius:50%;animation:r .8s linear infinite;margin-bottom:18px}@keyframes r{to{transform:rotate(360deg)}}h2{margin:0 0 8px;font-size:20px}p{margin:0 0 20px;color:#94a3b8;font-size:15px}button{background:#DF3131;color:#fff;border:0;border-radius:14px;padding:14px 28px;font-size:16px;font-weight:800}</style></head><body><div class="sp"></div><h2>Connessione a Timbrature AMR…</h2><p>Apertura schermata in corso, attendi un istante.</p><button onclick="location.reload()">Riprova ora</button><script>setTimeout(function(){location.reload();},2500);</script></body></html>';

function fetchUrlWithTimeout(urlStr, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    fetch(urlStr, { cache: 'no-cache', credentials: 'same-origin' })
      .then((res) => { clearTimeout(timer); resolve(res); })
      .catch((err) => { clearTimeout(timer); reject(err); });
  });
}

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then(async (c) => {
      // 1. Copia subito eventuali risorse già presenti in cache precedenti così non resta mai vuoto
      for (const u of ASSETS) {
        try {
          const oldMatch = await caches.match(u, { ignoreSearch: true });
          if (oldMatch && oldMatch.ok) await c.put(u, oldMatch.clone());
        } catch (_) {}
      }
      // 2. Scarica le versioni aggiornate dal server usando stringhe URL (compatibile al 100% con iOS Safari WebKit)
      await Promise.allSettled(
        ASSETS.map((u) =>
          fetchUrlWithTimeout(u, 15000).then((r) => (r && r.ok && r.status === 200 ? c.put(u, r.clone()) : null))
        )
      );
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  const normPath = url.pathname === '/timbra/' ? '/timbra' : url.pathname;
  if (!ASSETS.includes(normPath)) return;

  // Strategia Offline-First istantanea (Stale-While-Revalidate) compatibile con iOS Safari WebKit:
  // Usa SEMPRE normPath (stringa) invece di e.request (che ha mode="navigate" e causa TypeError in WebKit se combinato con opzioni fetch).
  e.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(normPath, { ignoreSearch: true }).then((cachedResp) => {
        const bgUpdate = fetchUrlWithTimeout(normPath, 20000)
          .then((netResp) => {
            if (netResp && netResp.ok && netResp.status === 200) {
              cache.put(normPath, netResp.clone());
              return netResp;
            }
            return null;
          })
          .catch(() => null);

        if (cachedResp) {
          e.waitUntil(bgUpdate);
          return cachedResp.clone();
        }

        return bgUpdate.then((netResp) => {
          if (netResp) return netResp;
          if (normPath === '/timbra') {
            return new Response(FALLBACK_HTML, {
              status: 200,
              headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' }
            });
          }
          return new Response('', { status: 504 });
        });
      })
    )
  );
});

self.addEventListener('sync', (e) => {
  if (e.tag === 'amr-timbra-flush') {
    e.waitUntil(
      self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
        clients.forEach((client) => client.postMessage({ type: 'FLUSH_QUEUE' }));
      })
    );
  }
});
