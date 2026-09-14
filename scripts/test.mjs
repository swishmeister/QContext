import { spawnSync } from 'node:child_process';
import { env, python, root } from './runtime.mjs';

const result = spawnSync(python, ['-m', 'unittest', 'discover', '-s', 'tests', '-v'], { cwd: root, env, stdio: 'inherit' });
if (result.error) console.error(result.error.message);
process.exitCode = result.status ?? 1;
