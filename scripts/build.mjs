import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { env, root } from './runtime.mjs';

const result = spawnSync(process.execPath, [join(root, 'node_modules/vinext/dist/cli.js'), 'build'], {
  cwd: root, env: { ...env, QUEUE_CONTEXT_BUILD_TARGET: 'node' }, stdio: 'inherit',
});
if (result.error) console.error(result.error.message);
process.exitCode = result.status ?? 1;
