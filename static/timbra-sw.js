/* AMR Timbra – service worker (v4 Offline-First istantaneo)
 * Apre SEMPRE istantaneamente la pagina /timbra dalla memoria del telefono
 * (zero attesa di rete, zero schermo bianco su iPhone anche con 4G/Wi-Fi bloccato),
 * e aggiorna silenziosamente i file in background quando la connessione è stabile.
 * Le chiamate /api/* NON vengono mai messe in cache: le timbrature e le richieste
 * offline sono gestite dalla coda locale nella pagina (localStorage).
 */
const CACHE = 'amr-timbra-v4';
const ASSETS = ['/timbra', '/static/timbra-icon-192.png', '/static/timbra-icon-512.png', '/static/timbra-manifest.json',
  '/static/leaflet/leaflet.js', '/static/leaflet/leaflet.css'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function fetchWithTimeout(req, ms) {
  if (typeof AbortController === 'undefined') return fetch(req);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  return fetch(req, { signal: ctrl.signal, cache: 'no-store' }).finally(() => clearTimeout(timer));
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (!ASSETS.includes(url.pathname)) return;

  // Strategia Offline-First istantanea (Stale-While-Revalidate):
  // 1. Se la pagina/risorsa è già nella memoria del telefono, la mostra SUBITO (0 ms, mai schermo bianco).
  // 2. Intanto in background scarica la versione aggiornata dal server (solo se HTTP 200 valida).
  e.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(url.pathname).then((cachedResp) => {
        const netPromise = fetchWithTimeout(e.request, 4000)
          .then((netResp) => {
            if (netResp && netResp.ok && netResp.status === 200) {
              cache.put(url.pathname, netResp.clone());
              return netResp;
            }
            return cachedResp || netResp;
          })
          .catch(() => cachedResp);

        // Se abbiamo già la pagina sul telefono, la restituiamo ISTANTANEAMENTE senza aspettare la rete!
        if (cachedResp) {
          e.waitUntil(netPromise.catch(() => {}));
          return cachedResp;
        }
        return netPromise;
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
