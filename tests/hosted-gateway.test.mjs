import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer, request } from 'node:http';
import test from 'node:test';
import { createGateway, hostedConfig } from '../scripts/hosted-gateway.mjs';

test('hosted configuration refuses missing passwords and insecure public URLs', () => {
  assert.throws(() => hostedConfig({}), /public/i);
  assert.throws(() => hostedConfig({ QUEUE_CONTEXT_PUBLIC_URL: 'https://queue.example' }), /password/i);
  assert.throws(() => hostedConfig({ QUEUE_CONTEXT_PUBLIC_URL: 'http://queue.example', QUEUE_CONTEXT_PASSWORD: 'x'.repeat(24) }), /HTTPS/);
  const config = hostedConfig({ RENDER_EXTERNAL_URL: 'https://queue.example', QUEUE_CONTEXT_PASSWORD: 'x'.repeat(24) });
  assert.equal(config.origin, 'https://queue.example');
  assert.equal(config.bind, '0.0.0.0');
});

test('gateway authenticates all app routes, checks origins, and strips credentials before proxying', async t => {
  // Node fetch owns the Host header; use raw HTTP to exercise host validation.
  const fetch = (url, options = {}) => new Promise((resolve, reject) => {
    const headers = { ...options.headers };
    if (options.body !== undefined) headers['content-length'] = Buffer.byteLength(options.body);
    const outgoing = request(url, { method: options.method, headers }, incoming => {
      incoming.resume();
      incoming.on('end', () => resolve({ status: incoming.statusCode }));
    });
    outgoing.on('error', reject);
    outgoing.end(options.body);
  });
  const calls = [];
  const backend = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    calls.push({ path: req.url, headers: req.headers, body });
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ok: true }));
  });
  backend.listen(0, '127.0.0.1');
  await once(backend, 'listening');
  t.after(() => { backend.closeAllConnections(); backend.close(); });
  const config = { host: 'queue.example', origin: 'https://queue.example', protocol: 'https', username: 'owner', password: 'test-only-password-12345', webPort: backend.address().port, collectorPort: backend.address().port };
  const gateway = createGateway(config);
  gateway.listen(0, '127.0.0.1');
  await once(gateway, 'listening');
  t.after(() => { gateway.closeAllConnections(); gateway.close(); });
  const url = `http://127.0.0.1:${gateway.address().port}`;
  const auth = `Basic ${Buffer.from(`${config.username}:${config.password}`).toString('base64')}`;
  const headers = { host: config.host, authorization: auth };
  for (const path of ['/', '/api/status', '/api/export', '/ranks/gold.png']) {
    assert.equal((await fetch(url + path, { headers: { host: config.host } })).status, 401);
  }
  assert.equal(calls.length, 0);
  assert.equal((await fetch(url + '/healthz')).status, 200);
  assert.equal((await fetch(url + '/', { headers: { ...headers, host: 'evil.example' } })).status, 403);
  assert.equal((await fetch(url + '/api/keys', { method: 'POST', headers, body: '{}' })).status, 403);
  assert.equal((await fetch(url + '/api/keys', { method: 'POST', headers: { ...headers, origin: 'https://evil.example' }, body: '{}' })).status, 403);
  assert.equal((await fetch(url + '/', { method: 'POST', headers: { ...headers, origin: config.origin }, body: '{}' })).status, 405);
  assert.equal((await fetch(url + '/api/keys', { method: 'POST', headers: { ...headers, origin: config.origin }, body: 'x'.repeat(4097) })).status, 413);
  const accepted = await fetch(url + '/api/pause', { method: 'POST', headers: { ...headers, origin: config.origin, 'content-type': 'application/json', 'x-queue-lab-token': 'csrf-test', cookie: 'secret=test', 'x-forwarded-host': 'evil.example' }, body: '{}' });
  assert.equal(accepted.status, 200);
  const call = calls.at(-1);
  assert.equal(call.body, '{}');
  assert.equal(call.headers['x-queue-lab-token'], 'csrf-test');
  assert.equal(call.headers.authorization, undefined);
  assert.equal(call.headers.cookie, undefined);
  assert.equal(call.headers['x-forwarded-host'], undefined);
  assert.equal(call.headers.host, `127.0.0.1:${config.collectorPort}`);
  await fetch(url + '/', { headers });
  assert.equal(calls.at(-1).headers['x-forwarded-host'], config.host);
});
