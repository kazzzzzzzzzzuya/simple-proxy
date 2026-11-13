// README.md -> styled HTML, ready for printing
const fs = require('fs');
const { marked } = require('marked');
const [src, out, css] = process.argv.slice(2);
let md = fs.readFileSync(src, 'utf8');
// The centred header block in the README is HTML wrapped around markdown.
md = md.replace(/<div align="center">([\s\S]*?)<\/div>/, (_, inner) =>
  '<header class="cover">' + marked.parse(inner) + '</header>');
const body = marked.parse(md, { gfm: true });
const html = `<!doctype html><html><head><meta charset="utf-8"><title>Legion Go Toolbox – README</title>
<style>${fs.readFileSync(css, 'utf8')}
header.cover { text-align: center; padding: 6pt 0 10pt; border-bottom: 1.5pt solid var(--ink); margin-bottom: 12pt; }
header.cover h1 { font-size: 26pt; }
header.cover p { color: var(--muted); }
h2 { break-before: auto; }
</style></head><body>${body}</body></html>`;
fs.writeFileSync(out, html);
