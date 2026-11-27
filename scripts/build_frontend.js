const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'dist');
let url = (process.env.NEXT_PUBLIC_API_URL || '').trim().replace(/\/+$/, '');
if (url) {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) throw Error('NEXT_PUBLIC_API_URL must be an HTTP(S) origin without credentials, path, query, or fragment');
  url = parsed.origin;
}
fs.rmSync(dist, {recursive:true, force:true});
fs.mkdirSync(path.join(dist, 'static'), {recursive:true});
for (const file of ['app.js','style.css']) fs.copyFileSync(path.join(root, 'static', file), path.join(dist, 'static', file));
fs.copyFileSync(path.join(root, 'static', 'index.html'), path.join(dist, 'index.html'));
fs.writeFileSync(path.join(dist, 'static', 'config.js'), 'window.SYNCRIA_API_URL = '+JSON.stringify(url)+';\n');
console.log('Frontend built to dist/ ('+(url || 'same origin')+')');
