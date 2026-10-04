// Drive the delivered viewer's own Export > PNG in Archify's headless Chrome and save the blob.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
// Usage: node tools/archify-export-png.mjs <delivered.html> light|dark <out.png>
// ARCHIFY_HOME overrides the Archify skill directory (default ~/.claude/skills/archify).
const home = process.env.ARCHIFY_HOME || path.join(os.homedir(), '.claude', 'skills', 'archify');
const skill = path.join(home, 'bin', 'visual-check.mjs');
const { ChromeVisualBrowser, findChrome } = await import(pathToFileURL(skill).href);

const [html, theme, out] = process.argv.slice(2);
const chrome = findChrome();
const browser = new ChromeVisualBrowser(chrome.path || chrome.executable || chrome);
try {
  await browser.inspect({ artifactPath: html, width: 1600, height: 1000, theme });
  const sessionId = await browser.sessionPromise;
  const result = await browser.cdp.send('Runtime.evaluate', {
    awaitPromise: true,
    returnByValue: true,
    expression: `new Promise(function (resolve, reject) {
      var original = URL.createObjectURL;
      URL.createObjectURL = function (blob) {
        if (blob.type !== "image/png") return original.call(URL, blob);
        var reader = new FileReader();
        reader.onload = function () { setTimeout(function () {
          var root = document.documentElement;
          resolve({
            data: String(reader.result).split(',')[1],
            bytes: blob.size,
            type: blob.type,
            theme: root.getAttribute('data-theme'),
            canonical: root.getAttribute('data-last-export-canonical'),
            reportedBytes: root.getAttribute('data-last-export-bytes'),
            width: root.getAttribute('data-last-export-width'),
            height: root.getAttribute('data-last-export-height')
          }); }, 200);
        };
        reader.onerror = reject;
        reader.readAsDataURL(blob);
        return original.call(URL, blob);
      };
      var button = document.querySelector('.export-menu button[data-format="png"]');
      if (!button) return reject(new Error('PNG export button not found'));
      button.click();
      setTimeout(function () { reject(new Error('export timed out')); }, 30000);
    })`,
  }, sessionId, 40000);
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  const value = result.result.value;
  const buffer = Buffer.from(value.data, 'base64');
  fs.writeFileSync(out, buffer);
  delete value.data;
  console.log(JSON.stringify({ out, written: buffer.length, ...value }));
} finally {
  await browser.close();
}
