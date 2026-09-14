import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { env, python, root } from './runtime.mjs';

const children = [];
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  for (const child of children) child.kill('SIGTERM');
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => stop());
function launch(command, args) {
  const child = spawn(command, args, { cwd: root, env, stdio: 'inherit' });
  children.push(child);
  child.on('error', (error) => { console.error(`Could not start Queue Context: ${error.message}`); stop(1); });
  child.on('exit', (code) => { if (!stopping) stop(code ?? 1); });
}
console.log('Starting Queue Context at http://127.0.0.1:5173 — keep this window open.');
launch(python, ['collector/server.py']);
launch(process.execPath, [join(root, 'node_modules/vinext/dist/cli.js'), 'dev', '--hostname', '127.0.0.1', '--port', '5173']);
