import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = dirname(dirname(fileURLToPath(import.meta.url)));
const bundledPython = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3');
export const python = process.env.QUEUE_CONTEXT_PYTHON || process.env.QUEUE_LAB_PYTHON || (existsSync(bundledPython) ? bundledPython : 'python3');
export const env = { ...process.env, PATH: `${dirname(process.execPath)}:${process.env.PATH || ''}` };
