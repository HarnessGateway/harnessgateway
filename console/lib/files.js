// Files in and out of the browser: one given to the person to keep, and one asked of them.
import { h } from "./dom.js";

/** Give the person a file to keep. */
export function download(filename, content, type = "application/octet-stream") {
  const link = h("a", { href: URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type })), download: filename });
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
/** Ask the person for a file. Resolves to it, or never when they change their mind. */
export const pickFile = accept => new Promise(resolve => {
  const input = h("input", { type: "file", accept, hidden: true, onchange: () => { if (input.files[0]) resolve(input.files[0]); input.remove(); } });
  document.body.append(input);
  input.click();
});

/** Put text on the clipboard. On plain http, where a page is given no clipboard, it is done the old way. */
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const area = h("textarea", { style: "position:fixed;opacity:0" });
  area.value = text;
  document.body.append(area);
  area.select();
  try { document.execCommand("copy"); } finally { area.remove(); }
}
