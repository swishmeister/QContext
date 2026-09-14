import { spawnSync } from 'node:child_process';
import { env, python, root } from './runtime.mjs';

const result = spawnSync(python, ['-m', 'unittest', 'discover', '-s', 'tests', '-v'], { cwd: root, env, stdio: 'inherit' });
if (result.error) console.error(result.error.message);
process.exitCode = result.status ?? 1;

const profileStats = spawnSync(process.execPath, ['--experimental-strip-types', '--test', 'tests/profile-stats.test.mjs'], { cwd: root, env, stdio: 'inherit' });
if (profileStats.error) console.error(profileStats.error.message);
if (profileStats.status !== 0) process.exitCode = profileStats.status ?? 1;
