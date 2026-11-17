/* 起爆点·选股雷达 — Service Worker（v4）
   核心原则：**HTML 必须 network-first**。
   前一版把 index.html 放进 stale-while-revalidate，等于永远先给旧页面、
   后台再更新 —— 你传了新文件，手机上看到的还是上一版，就以为是没生效。
   data.js 同理，它是 window.__DATA__ 的来源，走 SW 缓存会一直慢一版。 */

const VER        = 'v5';
const STATIC_CACHE = 'qibao-static-' + VER;  // index.html / manifest / 图标
const DATA_CACHE   = 'qibao-data-' + VER;    // data.json / auction.json

// 只预缓存「不会变」的东西。index.html 和 data.js 一律走网络。
const SHELL = ['./manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(STATIC_CACHE);
    // 逐个 add：addAll 是原子的，一个 404 全批失败
    for (const f of SHELL) {
      try { await c.add(f); } catch (err) { /* 缺一个不影响启动 */ }
    }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter(k => k !== STATIC_CACHE && k !== DATA_CACHE).map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

/** 网络优先，失败回退缓存；页面上有「刷新」按钮，宁可给新数据也别给旧页面 */
async function networkFirst(req, cacheName, timeoutMs) {
  const url = new URL(req.url);
  const clean = url.origin + url.pathname;   // 去掉 ?t= 时间戳再存，否则缓存永久 miss
  try {
    const ctl = new AbortController();
    const tid = setTimeout(() => ctl.abort(), timeoutMs || 12000);
    const res = await fetch(req, { signal: ctl.signal });
    clearTimeout(tid);
    if (res && res.ok) {
      const c = await caches.open(cacheName);
      c.put(clean, res.clone()).catch(() => {});
    }
    return res;
  } catch (err) {
    const hit = await caches.match(clean);
    if (hit) return hit;
    if (cacheName === DATA_CACHE) {
      return new Response(JSON.stringify({ error: '离线状态，无缓存数据' }),
                          { headers: { 'Content-Type': 'application/json' } });
    }
    // 完全没缓存时给一个最小占位，避免 respondWith(null) 抛错
    return new Response(
      '<!doctype html><meta charset="utf-8"><body style="background:#0d1117;color:#8fa8c8;' +
      'font:14px/1.9 -apple-system,sans-serif;padding:40px 20px;text-align:center">' +
      '<h3 style="color:#ffb020">起爆点·选股雷达</h3>' +
      '<p>第一次打开需要联网。<br>连上网后刷新即可。</p></body>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }
}

async function cacheFirst(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res && res.ok) {
      const c = await caches.open(STATIC_CACHE);
      c.put(req, res.clone()).catch(() => {});
    }
    return res;
  } catch (err) {
    return new Response('', { status: 504 });
  }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // 只管自己域

  const p = url.pathname;

  // 1) 动态数据：永远先问网络（有 12s 保护），断了才吃缓存
  if (p.endsWith('/data.json') || p.endsWith('/lhb.json') || p.endsWith('/auction.json')) {
    e.respondWith(networkFirst(req, DATA_CACHE, 12000));
    return;
  }

  // 2) 页面本体 + SW 自身 + data.js：network-first，保证「传了就生效」
  if (p.endsWith('/') || p.endsWith('/index.html') || p.endsWith('/sw.js') ||
      p.endsWith('/data.js')) {
    e.respondWith(networkFirst(req, STATIC_CACHE, 10000));
    return;
  }

  // 3) 图标 / manifest：几乎不变，直接吃缓存
  e.respondWith(cacheFirst(req));
});
