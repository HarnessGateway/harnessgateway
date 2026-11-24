// The viewer: draws what the console sends it (a Mermaid diagram, a Word document, a spreadsheet, or a page from
// a reply) in a frame that is sandboxed into an origin of its own, so the libraries that parse these, and the
// page an agent wrote, never run in the console. It is sent one {type: "render", kind, data, theme}; it answers
// "ready", then its height as that changes, or an error. Each library is pinned and checked by an integrity hash.
(() => {
  "use strict";
  const CDN = "https://cdn.jsdelivr.net/npm";
  const LIBRARIES = {
    mermaid: { global: "mermaid", src: `${CDN}/mermaid@12.3.0/dist/mermaid.min.js`, integrity: "sha384-HHW54r639fK42Ip0coGF3mz8oSiGn0eEoWNTLs75X7H63pLSDBa0Tqs0RwGmLlXE" },
    docx: { global: "mammoth", src: `${CDN}/mammoth@1.13.0/mammoth.browser.min.js`, integrity: "sha384-8NWE3bZDrmRI4IePMoQaEoLhAQ1MmRMJkdNgnGiHRFanTqsFKr0TmK6aSZvFJ/PO" },
    sheet: { global: "XLSX", src: "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js", integrity: "sha384-EnyY0/GSHQGSxSgMwaIPzSESbqoOLSexfnSMN2AP+39Ckmn92stwABZynq1JyzdT" },
  };
  const SHEET_ROWS = 5000;                         // the rows of a sheet that are drawn; the rest is in the download
  const out = document.getElementById("out");
  const tell = message => parent.postMessage(message, "*");
  const library = ({ global, src, integrity }) => new Promise((loaded, failed) => {
    const script = Object.assign(document.createElement("script"), { src, integrity, crossOrigin: "anonymous" });
    script.onload = () => loaded(window[global]);
    script.onerror = () => failed(new Error("the renderer could not be loaded (is this browser offline?)"));
    document.head.append(script);
  });

  const DRAW = {
    // A page from a reply: the whole frame becomes it, and it runs here, in this frame's own origin.
    page(source, _theme, language) {
      document.open();
      document.write(language === "svg" ? `<!doctype html><body style="margin:0;display:grid;place-items:center;min-height:100vh">${source}</body>` : String(source));
      document.close();
    },
    async mermaid(source, theme) {
      const mermaid = await library(LIBRARIES.mermaid);
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: theme === "dark" ? "dark" : "default" });
      out.className = "diagram";
      out.innerHTML = (await mermaid.render("diagram", String(source))).svg;
    },
    async docx(data) {
      const mammoth = await library(LIBRARIES.docx);
      out.className = "paper";
      out.innerHTML = `<div class="docx">${(await mammoth.convertToHtml({ arrayBuffer: data })).value}</div>`;
    },
    async sheet(data) {
      const XLSX = await library(LIBRARIES.sheet), book = XLSX.read(data, { type: "array", sheetRows: SHEET_ROWS + 1, cellDates: true });
      const tabs = Object.assign(document.createElement("div"), { className: "tabs" }), sheet = Object.assign(document.createElement("div"), { className: "sheet" });
      const show = name => {
        const range = book.Sheets[name]["!fullref"] || book.Sheets[name]["!ref"], rows = range ? XLSX.utils.decode_range(range).e.r + 1 : 0;
        sheet.innerHTML = XLSX.utils.sheet_to_html(book.Sheets[name], { header: "", footer: "" });
        if (rows > SHEET_ROWS) sheet.insertAdjacentHTML("beforeend", `<div class="limit">Showing ${SHEET_ROWS} of ${rows} rows.</div>`);
        for (const tab of tabs.children) tab.classList.toggle("on", tab.textContent === name);
      };
      for (const name of book.SheetNames) {
        const tab = Object.assign(document.createElement("button"), { textContent: name });
        tab.addEventListener("click", () => show(name));
        tabs.append(tab);
      }
      out.className = "";
      out.replaceChildren(...(book.SheetNames.length > 1 ? [tabs] : []), sheet);
      show(book.SheetNames[0]);
    },
  };

  const size = () => tell({ type: "size", height: out.getBoundingClientRect().bottom + window.scrollY });
  let started = false;
  window.addEventListener("message", async event => {
    if (event.source !== parent || started || !event.data || event.data.type !== "render" || !DRAW[event.data.kind]) return;
    started = true;
    document.documentElement.dataset.theme = event.data.theme === "dark" ? "dark" : "light";
    try {
      await DRAW[event.data.kind](event.data.data, event.data.theme, event.data.language);
      size();
    } catch (error) {
      out.className = "note";
      out.textContent = String((error && error.message) || error);
      tell({ type: "error", message: out.textContent });
    }
  });
  new ResizeObserver(size).observe(out);            // as it grows: a document's images load later
  tell({ type: "ready" });
})();
