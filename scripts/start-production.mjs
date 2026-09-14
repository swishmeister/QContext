import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { env, python, root } from './runtime.mjs';
import { createGateway, hostedConfig } from './hosted-gateway.mjs';

let config;
try { config = hostedConfig(); }
catch (error) { console.error(error.message); process.exit(1); }
const children = [];
const gateway = createGateway(config);
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  gateway.close();
  gateway.closeAllConnections();
  for (const child of children) child.kill('SIGTERM');
  const deadline = setTimeout(() => {
    for (const child of children) if (child.exitCode === null) child.kill('SIGKILL');
  }, 8000);
  deadline.unref();
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => stop());
const childEnv = {
  ...env, NODE_ENV: 'production',
  QUEUE_CONTEXT_PUBLIC_URL: config.origin,
  QUEUE_CONTEXT_COLLECTOR_PORT: String(config.collectorPort),
  VINEXT_TRUSTED_HOSTS: config.host,
};
delete childEnv.QUEUE_CONTEXT_PASSWORD;
function launch(command, args) {
  const child = spawn(command, args, { cwd: root, env: childEnv, stdio: 'inherit' });
  children.push(child);
  child.on('error', () => { console.error('A Queue Context service could not start.'); stop(1); });
  child.on('exit', () => { if (!stopping) { console.error('A Queue Context service stopped.'); stop(1); } });
}
launch(python, ['collector/server.py']);
launch(process.execPath, [join(root, 'node_modules/vinext/dist/cli.js'), 'start', '--hostname', '127.0.0.1', '--port', String(config.webPort)]);
gateway.on('error', () => { console.error('Could not open the public web port.'); stop(1); });
gateway.listen(config.port, config.bind, () => console.log(`Queue Context is listening on port ${config.port}.`));
