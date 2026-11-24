/* HomeMusic service worker: app shell cache, cover cache, Web Share Target, notification clicks.
   The rest of /api/* is never cached. BUILD_ID is stamped by vite.config.ts at build time. */
const BUILD_ID = '__BUILD_ID__';
const SHELL = `hm-shell-${BUILD_ID}`;
const ASSETS = 'hm-assets-v1'; // hashed file names: safe to keep across builds
// Bump the version to drop entries of the previous strategy on activate.
const COVERS = 'hm-covers-v3';
const COVER_MAX = 300; // entries; the oldest are evicted first
const COVER_REFRESH_MS = 24 * 60 * 60 * 1000; // a hit younger than this is not revalidated: no request at all
const SHELL_URLS = ['/', '/manifest.webmanifest', '/icons/icon.svg', '/icons/icon-192.png', '/icons/badge-96.png'];

// Same layout as src/lib/shareStore.ts
const SHARE_DB = 'homemusic-share';
const SHARE_STORE = 'shares';
const SHARE_KEY = 'pending';

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const shell = await caches.open(SHELL);
      await shell.addAll(SHELL_URLS);
      // Precache the hashed bundles referenced by index.html so the shell works offline right away.
      try {
        const html = await (await shell.match('/')).text();
        const urls = [...new Set([...html.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]))];
        const assets = await caches.open(ASSETS);
        await assets.addAll(urls);
      } catch (e) {
        /* best effort */
      }
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL, ASSETS, COVERS]);
      for (const key of await caches.keys()) if (!keep.has(key)) await caches.delete(key);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // covers from Deezer etc.: browser cache

  if (req.method === 'POST' && url.pathname === '/share-target') {
    event.respondWith(handleShare(req));
    return;
  }
  if (req.method !== 'GET') return;
  // Only the two cover routes can use Cache Storage. Other API requests stay network-only.
  if (isCoverPath(url.pathname)) {
    serveCover(req, event);
    return;
  }
  if (url.pathname.startsWith('/api/') || url.pathname === '/healthz' || url.pathname.startsWith('/app/')) return;

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(req, ASSETS));
    return;
  }
  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    event.respondWith(networkFirstShell(req));
    return;
  }
  if (SHELL_URLS.includes(url.pathname) || url.pathname.startsWith('/icons/')) {
    event.respondWith(staleWhileRevalidate(req, SHELL));
  }
});

function isCoverPath(pathname) {
  // Upload covers are intentionally excluded.
  return /^\/api\/nd\/cover\/[^/]+$/.test(pathname) || /^\/api\/library\/albums\/\d+\/cover$/.test(pathname);
}

let coverSession;
function currentCoverUser() {
  // Share only an in-flight check. Do not serve previous users' images after logout or expiry.
  coverSession ??= fetch('/api/auth/me', { cache: 'no-store', credentials: 'same-origin' })
    .then((res) => res.ok ? res.json() : null)
    .then((me) => Number.isSafeInteger(me?.id) ? String(me.id) : null)
    .catch(() => null)
    .finally(() => { coverSession = undefined; });
  return coverSession;
}

function serveCover(req, event) {
  const lookup = (async () => {
    const user = await currentCoverUser();
    if (!user) return null;
    const key = new URL(req.url);
    key.searchParams.set('_sw_cover_user', user);
    const cache = await caches.open(COVERS);
    return { cache, key: key.href, user, hit: await cache.match(key.href) };
  })().catch(() => null);

  const network = lookup.then((entry) => {
    if (entry?.hit && isCoverFresh(entry.hit)) return null;
    // A refresh must reach Hub even if the browser still has an immutable HTTP entry.
    return fetch(req, { cache: entry ? (entry.hit ? 'reload' : 'default') : 'no-store' });
  });
  const update = Promise.all([lookup, network]).then(async ([entry, res]) => {
    if (!entry || !res) return;
    if (res.status === 401 || res.status === 403 || res.status === 404) {
      if (entry.hit) await entry.cache.delete(entry.key);
      return;
    }
    if (res.status !== 200 || res.headers.get('x-cover-user') !== entry.user
        || !res.headers.get('content-type')?.startsWith('image/')) return;
    await entry.cache.put(entry.key, res.clone());
    await trimCoverCache(entry.cache);
  });
  event.waitUntil(update.catch(() => {}));
  event.respondWith(lookup.then(async (entry) => entry?.hit || (await network)).catch(() => Response.error()));
}

function isCoverFresh(res) {
  const at = Date.parse(res.headers.get('date') || '');
  const maxAge = /(?:^|,\s*)max-age=(\d+)/i.exec(res.headers.get('cache-control') || '');
  return !!at && !!maxAge && Date.now() - at < Math.min(COVER_REFRESH_MS, Number(maxAge[1]) * 1000);
}

/** Keep the newest `COVER_MAX` entries: `cache.keys()` lists them in insertion order, so the oldest go first. */
async function trimCoverCache(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - COVER_MAX; i++) await cache.delete(keys[i]);
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) cache.put(req, res.clone());
  return res;
}

async function networkFirstShell(req) {
  const cache = await caches.open(SHELL);
  try {
    const res = await fetch(req);
    if (res.ok && res.headers.get('content-type')?.includes('text/html')) cache.put('/', res.clone());
    return res;
  } catch (e) {
    const hit = (await cache.match('/')) || (await cache.match(req));
    if (hit) return hit;
    throw e;
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  const net = fetch(req)
    .then((res) => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    })
    .catch(() => hit);
  return hit || net;
}

// --- Web Share Target ------------------------------------------------------------------------------

function openShareDb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(SHARE_DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(SHARE_STORE);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function handleShare(req) {
  try {
    const form = await req.formData();
    const files = form.getAll('files').filter((f) => f && typeof f === 'object' && 'size' in f && f.size > 0);
    const payload = {
      files,
      title: String(form.get('title') || ''),
      text: String(form.get('text') || ''),
      url: String(form.get('url') || ''),
      ts: Date.now(),
    };
    const db = await openShareDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(SHARE_STORE, 'readwrite');
      tx.objectStore(SHARE_STORE).put(payload, SHARE_KEY);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    db.close();
    return Response.redirect('/#/share', 303);
  } catch (e) {
    // Could not store locally: let the server-side fallback handle it next time.
    return Response.redirect('/#/assistant', 303);
  }
}

// --- notifications -----------------------------------------------------------------------------------

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/#/downloads';
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const c of all) {
        if (new URL(c.url).origin === self.location.origin) {
          await c.focus();
          if ('navigate' in c) return c.navigate(target);
          return;
        }
      }
      return self.clients.openWindow(target);
    })(),
  );
});
