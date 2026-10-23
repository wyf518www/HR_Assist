// Local preview only. No mailbox, model, or recruitment data access.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..', 'prototype');
const files = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/index.html', ['index.html', 'text/html; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']]
]);
http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
  const entry = files.get(pathname);
  if (!entry || !['GET', 'HEAD'].includes(request.method)) {
    response.writeHead(404); response.end('Not found'); return;
  }
  response.setHeader('Content-Type', entry[1]);
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  if (request.method === 'HEAD') { response.end(); return; }
  fs.createReadStream(path.join(root, entry[0])).pipe(response);
}).listen(8765, '127.0.0.1', () => {
  console.log('Prototype preview: http://127.0.0.1:8765 (loopback only)');
});
