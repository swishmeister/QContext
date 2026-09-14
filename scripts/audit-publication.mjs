// Reports suspect filenames only, never matching credential values.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { root } from './runtime.mjs';

const git = (...args) => execFileSync('git', args, { cwd: root, maxBuffer: 64 * 1024 * 1024 });
const suspicious = new Set();
const secret = /RGAPI-[A-Za-z0-9-]{20,}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/;
const privatePath = /(?:^|\/)(?:data|node_modules|\.git)\/|(?:^|\/)\.env(?!\.example$)(?:\.|$)|\.(?:sqlite[^/]*|db|pem)$/;
function inspect(name, content) {
  if (privatePath.test(name) || secret.test(content.toString('utf8'))) suspicious.add(name);
}
const paths = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').toString().split('\0').filter(Boolean);
for (const path of new Set(paths)) inspect(path, readFileSync(new URL('../' + path, import.meta.url)));
const objects = git('rev-list', '--objects', '--all').toString().trim().split('\n');
let blobs = 0;
for (const entry of objects) {
  const split = entry.indexOf(' ');
  if (split < 0) continue;
  const id = entry.slice(0, split), name = entry.slice(split + 1);
  if (git('cat-file', '-t', id).toString().trim() !== 'blob') continue;
  inspect(name, git('cat-file', 'blob', id));
  blobs++;
}
if (suspicious.size) {
  console.error('Review these paths before publishing:', [...suspicious].sort().join(', '));
  process.exitCode = 1;
} else console.log(`Publication audit passed: ${new Set(paths).size} source files and ${blobs} historical blobs; no matching credentials or private data paths.`);
