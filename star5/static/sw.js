/* Star5 Search service worker: makes the app installable and shows an offline page.
   It never caches pages or API answers: everything is private and behind the login. */
"use strict";
const OFFLINE = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Star5 Search</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,sans-serif;
background:#05213a;color:#e9fffb;text-align:center;padding:24px;box-sizing:border-box}
button{margin-top:16px;font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#5ee6f2;color:#05213a}</style>
</head><body><div><p><b>Star5 Search</b></p><p>Hors ligne · Offline · Sei offline</p>
<button onclick="location.reload()">↻</button></div></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, {headers: {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}})));
});
