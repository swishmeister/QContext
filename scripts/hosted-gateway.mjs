import { createHash, timingSafeEqual } from 'node:crypto';
import { createServer, request } from 'node:http';

export function hostedConfig(env = process.env) {
  const publicUrl = env.QUEUE_CONTEXT_PUBLIC_URL || env.RENDER_EXTERNAL_URL;
  if (!publicUrl) throw new Error('Set QUEUE_CONTEXT_PUBLIC_URL (Render provides RENDER_EXTERNAL_URL automatically).');
  const url = new URL(publicUrl);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('The public URL must contain only the scheme and hostname.');
  }
  const local = ['localhost', '127.0.0.1'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
    throw new Error('Hosted access requires an HTTPS public URL.');
  }
  const username = env.QUEUE_CONTEXT_USERNAME || 'owner';
  const password = env.QUEUE_CONTEXT_PASSWORD || '';
  if (username.includes(':') || /[\r\n]/.test(username) || password.length < 20) {
    throw new Error('Set QUEUE_CONTEXT_PASSWORD to a unique password of at least 20 characters.');
  }
  const port = (name, fallback) => {
    const value = Number(env[name] || fallback);
    if (!Number.isInteger(value) || value < 1 || value > 65535) throw new Error(`Invalid ${name}.`);
    return value;
  };
  return {
    origin: url.origin, host: url.host, protocol: url.protocol.slice(0, -1),
    username, password, bind: local ? '127.0.0.1' : '0.0.0.0',
    port: port('PORT', 10000), webPort: port('QUEUE_CONTEXT_WEB_PORT', 3000),
    collectorPort: port('QUEUE_CONTEXT_COLLECTOR_PORT', 8766),
  };
}

const digest = (value) => createHash('sha256').update(value).digest();
const hopHeaders = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade']);
function cleanHeaders(headers) {
  const blocked = new Set([...hopHeaders, ...(headers.connection || '').split(',').map(v => v.trim().toLowerCase())]);
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !blocked.has(name)));
}

export function createGateway(config) {
  const expected = digest(`Basic ${Buffer.from(`${config.username}:${config.password}`).toString('base64')}`);
  const send = (res, status, text, headers = {}) => {
    res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store', ...headers });
    res.end(text);
  };
  const healthy = async (port, path) => {
    const response = await fetch(`http://127.0.0.1:${port}${path}`, { signal: AbortSignal.timeout(3000), redirect: 'error' });
    await response.arrayBuffer();
    return response.ok;
  };
  const server = createServer(async (req, res) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    // Health reveals no research data and is the only unauthenticated route.
    if (req.url === '/healthz' && req.method === 'GET') {
      try {
        const ready = await Promise.all([healthy(config.collectorPort, '/api/health'), healthy(config.webPort, '/favicon.svg')]);
        return send(res, ready.every(Boolean) ? 200 : 503, ready.every(Boolean) ? 'ok' : 'starting');
      } catch { return send(res, 503, 'starting'); }
    }
    if (req.headers.host !== config.host) return send(res, 403, 'Unrecognized host.');
    if (!timingSafeEqual(expected, digest(req.headers.authorization || ''))) {
      return send(res, 401, 'Sign in to Queue Context.', { 'WWW-Authenticate': 'Basic realm="Queue Context", charset="UTF-8"' });
    }
    if (!req.url?.startsWith('/') || req.url.startsWith('//')) return send(res, 400, 'Invalid path.');
    const isApi = req.url.startsWith('/api/');
    if (!['GET', 'HEAD'].includes(req.method) && !(isApi && req.method === 'POST')) {
      return send(res, 405, 'Method not allowed.');
    }
    if (req.headers.origin && req.headers.origin !== config.origin) return send(res, 403, 'Unrecognized origin.');
    if (req.method === 'POST') {
      if (req.headers.origin !== config.origin) return send(res, 403, 'A same-origin request is required.');
      const length = Number(req.headers['content-length']);
      if (req.headers['transfer-encoding'] || !Number.isInteger(length) || length < 0 || length > 4096) {
        return send(res, 413, 'Expected a small JSON request.');
      }
    }
    const upstreamPort = isApi ? config.collectorPort : config.webPort;
    const headers = cleanHeaders(req.headers);
    // Passwords and caller-supplied proxy headers never reach the app or collector.
    for (const name of Object.keys(headers)) {
      if (['authorization', 'cookie', 'forwarded'].includes(name) || name.startsWith('x-forwarded-')) delete headers[name];
    }
    headers.host = `127.0.0.1:${upstreamPort}`;
    if (!isApi) {
      headers['x-forwarded-host'] = config.host;
      headers['x-forwarded-proto'] = config.protocol;
    }
    const upstream = request({ hostname: '127.0.0.1', port: upstreamPort, path: req.url, method: req.method, headers }, response => {
      res.writeHead(response.statusCode || 502, { ...cleanHeaders(response.headers), 'Cache-Control': 'no-store' });
      response.pipe(res);
      response.on('error', () => res.destroy());
    });
    upstream.setTimeout(30000, () => upstream.destroy());
    upstream.on('error', () => { if (!res.headersSent) send(res, 502, 'Queue Context is restarting. Please retry.'); else res.destroy(); });
    req.on('aborted', () => upstream.destroy());
    res.on('close', () => upstream.destroy());
    req.pipe(upstream);
  });
  server.headersTimeout = 15000;
  server.requestTimeout = 30000;
  return server;
}
