/* BlogReader Service Worker
 *
 * 策略：
 * - App 外壳（/ 和 CDN 静态资源）用 stale-while-revalidate：优先秒开缓存，
 *   后台更新，保证离线也能打开界面。
 * - 所有 /api/* 请求一律走网络（network-only）：数据必须实时，且带鉴权 token，
 *   绝不缓存，避免登录/数据串味。
 */
const CACHE = 'blogreader-shell-v2';
const SHELL = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // 只处理 GET；非 GET（登录/刷新等）直接走网络
  if (req.method !== 'GET') return;

  // API 一律走网络，不缓存
  if (url.pathname.startsWith('/api/')) return;

  // 跨域（CDN：vue/mathjax）与同源外壳：stale-while-revalidate
  e.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => {
          if (res && res.ok && (url.origin === self.location.origin || req.mode !== 'navigate')) {
            cache.put(req, res.clone()).catch(() => {});
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
