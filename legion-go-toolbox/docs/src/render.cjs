const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  for (const [html, pdf, title] of [
    ['manual.html', 'Legion-Go-Toolbox-Manual.pdf', 'Legion Go Toolbox – User Manual'],
    ['readme.html', 'Legion-Go-Toolbox-README.pdf', 'Legion Go Toolbox – README'],
  ]) {
    await p.goto('file://' + path.resolve(html));
    await p.evaluate(() => document.fonts.ready);
    const footer = `<div style="width:100%;font-family:Inter,sans-serif;font-size:7.5pt;color:#7a8591;padding:0 16mm;display:flex;justify-content:space-between">
      <span>${title}</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`;
    const opts = { path: pdf, format: 'A4', printBackground: true, displayHeaderFooter: true,
      headerTemplate: '<div></div>', footerTemplate: footer,
      margin: { top: '18mm', bottom: '20mm', left: '16mm', right: '16mm' } };
    try { await p.pdf({ ...opts, outline: true, tagged: true }); }
    catch (e) { console.log('no outline support:', e.message.split('\n')[0]); await p.pdf(opts); }
    console.log('wrote', pdf);
  }
  await b.close();
})().catch(e => { console.error('FAILED', e); process.exit(1); });
