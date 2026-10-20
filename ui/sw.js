// Faraway の Service Worker。アプリとしてインストールできるようにするためだけのもの。
// 何もキャッシュしない（API の応答・トークンを保存しない）。すべてそのままネットワークへ通す。
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => { if (e.request.method === 'GET') e.respondWith(fetch(e.request)); });
