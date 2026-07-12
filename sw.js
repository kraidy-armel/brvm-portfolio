/* Service worker — Mon Portefeuille BRVM
   - App (même origine) : réseau d'abord, cache en secours -> toujours à jour en ligne, marche hors-ligne.
   - Librairies CDN : cache d'abord -> chargement rapide + hors-ligne.
   - Cours en direct (relais, github) : NON interceptés -> données toujours fraîches.
   Change CACHE_VERSION pour forcer une mise à jour du cache. */
const CACHE_VERSION = "brvm-v3";
const CORE = ["./", "./index.html", "./mon-portefeuille-brvm.html",
              "./icon-192.png", "./icon-512.png", "./manifest.webmanifest"];

self.addEventListener("install", e => {
  // On ne bloque pas l'installation si un fichier manque (réseau) : cache "au mieux"
  e.waitUntil(
    caches.open(CACHE_VERSION).then(c =>
      Promise.all(CORE.map(u => c.add(u).catch(() => null)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Fichiers de l'app (même origine) : réseau d'abord AVEC revalidation forcée
  // ({cache:"no-cache"} contourne les 10 min de cache HTTP de GitHub Pages :
  // les mises à jour apparaissent dès le déploiement, plus d'attente)
  if (url.origin === location.origin) {
    e.respondWith(
      fetch(req, { cache: "no-cache" }).then(res => {
        if (res && res.ok) {                       // ne cache QUE les réponses valides
          const copy = res.clone();
          caches.open(CACHE_VERSION).then(c => c.put(req, copy));
        }
        return res;
      }).catch(() => caches.match(req).then(m => m || caches.match("./mon-portefeuille-brvm.html") || caches.match("./index.html")))
    );
    return;
  }

  // Librairies CDN connues : cache d'abord
  if (/cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net/.test(url.hostname)) {
    e.respondWith(
      caches.match(req).then(m => m || fetch(req).then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE_VERSION).then(c => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // Tout le reste (cours en direct, relais) : réseau normal, non mis en cache
});
