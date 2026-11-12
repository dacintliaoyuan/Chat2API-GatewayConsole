// Retire the former console worker without clearing unrelated origin data.
self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const prefix = `rhine-lab:${new URL(self.registration.scope).pathname}:`;
    const names = await caches.keys();
    await Promise.all(names.filter(name => name.startsWith(prefix)).map(name => caches.delete(name)));
    await self.clients.claim();
    await self.registration.unregister();
  })());
});
