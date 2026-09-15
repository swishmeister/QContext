// Integration test of the actual built frontend, gateway and Python collector.
// Uses a disposable database and synthetic credentials; never starts a Riot import.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { env, python, root } from '../scripts/runtime.mjs';

async function freePort() {
  const server = createServer().listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

test('built app serves assets, protects imports, and retains data without retaining keys after restart', { timeout: 40000 }, async t => {
  const dataDir = await mkdtemp(join(tmpdir(), 'queue-context-test-'));
  t.after(() => rm(dataDir, { recursive: true, force: true }));
  const port = await freePort(), webPort = await freePort(), collectorPort = await freePort();
  const origin = `http://127.0.0.1:${port}`;
  const password = 'synthetic-hosted-test-password';
  const headers = { authorization: `Basic ${Buffer.from(`owner:${password}`).toString('base64')}` };
  const database = join(dataDir, 'queue-lab.sqlite3');
  const seed = spawnSync(python, ['-c', 'from collector.server import Store; import sys; s=Store(sys.argv[1]); s.put_setting("account", {"puuid":"test-player", "gameName":"Synthetic", "tagLine":"TEST"})', database], { cwd: root, env, encoding: 'utf8' });
  assert.equal(seed.status, 0, seed.stderr);
  let processHandle;
  let log = '';
  async function stop() {
    if (!processHandle || processHandle.exitCode !== null) return;
    const exited = once(processHandle, 'exit');
    processHandle.kill('SIGTERM');
    await exited;
  }
  t.after(stop);
  async function start(riotKey = '') {
    processHandle = spawn(process.execPath, ['scripts/start-production.mjs'], { cwd: root, env: {
      ...env, DATABASE_URL: '', RIOT_API_KEY: riotKey, PORT: String(port), QUEUE_CONTEXT_PUBLIC_URL: origin, QUEUE_CONTEXT_PASSWORD: password,
      QUEUE_CONTEXT_DATA_DIR: dataDir, QUEUE_CONTEXT_WEB_PORT: String(webPort), QUEUE_CONTEXT_COLLECTOR_PORT: String(collectorPort),
    }, stdio: ['ignore', 'pipe', 'pipe'] });
    processHandle.stdout.on('data', chunk => { log += chunk; });
    processHandle.stderr.on('data', chunk => { log += chunk; });
    for (let attempt = 0; attempt < 80; attempt++) {
      if (processHandle.exitCode !== null) assert.fail(`Production launcher stopped: ${log}`);
      try { if ((await fetch(origin + '/healthz')).ok) return; } catch { /* startup */ }
      await delay(100);
    }
    assert.fail(`Production startup timed out: ${log}`);
  }
  await start('RGAPI-' + 'x'.repeat(24));
  assert.equal((await fetch(origin + '/')).status, 401);
  const page = await fetch(origin + '/', { headers });
  assert.equal(page.status, 200);
  const html = await page.text();
  assert.match(html, /Queue Context/);
  const asset = html.match(/(?:src|href)="([^" ]+\.js(?:\?[^" ]*)?)"/);
  assert.ok(asset, 'HTML references a production JavaScript asset');
  assert.equal((await fetch(new URL(asset[1].replaceAll('&amp;', '&'), origin), { headers })).status, 200);
  assert.equal((await fetch(origin + '/ranks/emerald.png', { headers })).status, 200);
  const status = await (await fetch(origin + '/api/status', { headers })).json();
  assert.equal(status.account.name, 'Synthetic');
  const body = JSON.stringify({ keys: ['RGAPI-' + 'x'.repeat(24)] });
  const postHeaders = { ...headers, origin, 'content-type': 'application/json' };
  assert.equal((await fetch(origin + '/api/keys', { method: 'POST', headers: postHeaders, body })).status, 403);
  assert.equal((await fetch(origin + '/api/keys', { method: 'POST', headers: { ...postHeaders, 'x-queue-lab-token': status.csrf }, body })).status, 404);
  const connected = await (await fetch(origin + '/api/status', { headers })).json();
  assert.equal(connected.connected, true);
  assert.equal(connected.apiKeys, undefined);
  assert.ok(!JSON.stringify(connected).includes('RGAPI-' + 'x'.repeat(24)));
  assert.equal((await fetch(origin + '/api/pause', { method: 'POST', headers: { ...postHeaders, 'x-queue-lab-token': status.csrf }, body: '{}' })).status, 200);
  const exported = await (await fetch(origin + '/api/export', { headers })).json();
  assert.equal(exported.apiKeys, undefined);
  assert.equal(exported.csrf, undefined);
  await stop();
  assert.ok(!(await readFile(database)).includes(Buffer.from('RGAPI-' + 'x'.repeat(24))));
  await start();
  const restarted = await (await fetch(origin + '/api/status', { headers })).json();
  assert.equal(restarted.account.name, 'Synthetic');
  assert.equal(restarted.connected, false);
  assert.equal(restarted.apiKeys, undefined);
  assert.notEqual(restarted.csrf, status.csrf);
  assert.ok(!log.includes(password));
  assert.ok(!log.includes('RGAPI-' + 'x'.repeat(24)));
});
