/* AMR Timbra – service worker (v8 Offline-First + Cassaforte di Emergenza 2s)
 * 1. Apre SEMPRE istantaneamente la pagina /timbra dalla memoria del telefono (0 ms).
 * 2. Non cancella MAI le cache precedenti finché la nuova versione di /timbra non è salvata.
 * 3. Se iOS Safari avesse svuotato la cache e non c'è rete in capannone (timeout 2s),
 *    mostra una schermata di timbratura offline integrata che salva subito Entrata/Uscita
 *    in localStorage (amr_timbra_queue) all'orario esatto del tocco.
 */
const CACHE = 'amr-timbra-v8';
const ASSETS = ['/timbra', '/static/timbra-icon-192.png', '/static/timbra-icon-512.png', '/static/timbra-manifest.json',
  '/static/leaflet/leaflet.js', '/static/leaflet/leaflet.css'];

const FALLBACK_HTML = `<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>AMR Timbrature (Offline)</title><style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;min-height:100vh;background:#0d1117;color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:20px}
.card{width:100%;max-width:380px;background:#161c26;border:1px solid #263042;border-radius:20px;padding:22px}
h2{margin:0 0 6px;font-size:21px}
p{margin:0 0 16px;color:#94a3b8;font-size:14px}
input{width:100%;padding:14px;font-size:22px;text-align:center;letter-spacing:6px;border-radius:12px;border:1px solid #334155;background:#0d1117;color:#fff;margin-bottom:14px}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
button{border:0;border-radius:14px;padding:18px 12px;font-size:17px;font-weight:800;color:#fff;cursor:pointer}
.b-in{background:#16a34a}.b-out{background:#dc2626}.b-rel{background:#334155;width:100%;margin-top:14px;padding:12px;font-size:14px}
#okBox{display:none;margin-top:14px;padding:14px;border-radius:12px;background:#064e3b;color:#a7f3d0;font-weight:700;font-size:15px}
</style></head><body><div class="card">
<h2 id="wName">Modalità Senza Rete</h2>
<p id="wSub">Segnale assente: timbra qui sotto, l'orario esatto viene salvato sul telefono e inviato appena torna la linea.</p>
<input id="pinIn" type="tel" maxlength="4" placeholder="PIN (4 cifre)">
<div class="btns">
  <button class="b-in" onclick="saveOff('Entrata')">▶ ENTRATA</button>
  <button class="b-out" onclick="saveOff('Uscita')">■ USCITA</button>
</div>
<div id="okBox"></div>
<button class="b-rel" onclick="location.reload()">↻ Riprova connessione</button>
</div><script>
(function(){
  try {
    var u = JSON.parse(localStorage.getItem('amr_timbra_user') || 'null');
    var p = localStorage.getItem('amr_timbra_pin') || (u && u.pin) || '';
    if (p) document.getElementById('pinIn').value = p;
    if (u && u.nome) document.getElementById('wName').textContent = u.nome;
  } catch(e){}
})();
function saveOff(azione){
  var p = (document.getElementById('pinIn').value || '').trim();
  if (!/^\\d{4}$/.test(p)) { alert('Inserisci il tuo PIN di 4 cifre'); return; }
  var u = null;
  try { u = JSON.parse(localStorage.getItem('amr_timbra_user') || 'null'); } catch(e){}
  var dev = localStorage.getItem('amr_timbra_dev') || ('dev-' + Date.now().toString(36));
  var ts = Date.now();
  var item = {
    client_id: 'off-' + ts.toString(36) + '-' + Math.random().toString(36).slice(2,8),
    pin: p, nome: (u && u.pin === p && u.nome) ? u.nome : ('PIN ' + p),
    azione: azione, ts: ts, lat: null, lng: null, acc: null, device_id: dev, confermata: false
  };
  var q = [];
  try { q = JSON.parse(localStorage.getItem('amr_timbra_queue') || '[]'); } catch(e){}
  q.push(item);
  localStorage.setItem('amr_timbra_queue', JSON.stringify(q));
  var d = new Date(ts);
  var hh = ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
  var b = document.getElementById('okBox');
  b.style.display = 'block';
  b.textContent = '✓ ' + azione.toUpperCase() + ' delle ' + hh + ' salvata sul telefono! Verrà inviata appena torna la rete.';
  setTimeout(function(){ location.reload(); }, 3000);
}
</script></body></html>`;

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
      // 2. Scarica le versioni aggiornate dal server
      await Promise.allSettled(
        ASSETS.map((u) =>
          fetchUrlWithTimeout(u, 10000).then((r) => (r && r.ok && r.status === 200 ? c.put(u, r.clone()) : null))
        )
      );
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.open(CACHE).then(async (newCache) => {
      const hasTimbra = await newCache.match('/timbra', { ignoreSearch: true });
      if (hasTimbra) {
        const keys = await caches.keys();
        await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
      }
      await self.clients.claim();
    })
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  const normPath = url.pathname === '/timbra/' ? '/timbra' : url.pathname;
  if (!ASSETS.includes(normPath)) return;

  e.respondWith(
    caches.match(normPath, { ignoreSearch: true }).then((cachedResp) => {
      const bgUpdate = fetchUrlWithTimeout(normPath, cachedResp ? 15000 : 2000)
        .then(async (netResp) => {
          if (netResp && netResp.ok && netResp.status === 200) {
            const c = await caches.open(CACHE);
            await c.put(normPath, netResp.clone());
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
