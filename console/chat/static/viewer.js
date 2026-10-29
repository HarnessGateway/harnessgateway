// The viewer: renders what the page sends it (a Mermaid diagram, a Word document, a spreadsheet) in a frame that is
// sandboxed into an origin of its own, so the libraries that parse these never run in the page. The page sends one
// {type: "render", kind, data, theme}; this frame answers ready, then its height as it changes, or an error.
(() => {
  "use strict";
  const CDN = "https://cdn.jsdelivr.net/npm";
  const LIBS = {
    mermaid: { src: `${CDN}/mermaid@11.4.1/dist/mermaid.min.js`, integrity: "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I", global: "mermaid" },
    docx: { src: `${CDN}/mammoth@1.8.0/mammoth.browser.min.js`, integrity: "sha384-/cXAMbzovUIKbBERjPmR3SnPTh8siWr5lsvFYj1Uq4XP0yaJUZJmsh0YXyGv5P0y", global: "mammoth" },
    sheet: { src: "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js", integrity: "sha384-EnyY0/GSHQGSxSgMwaIPzSESbqoOLSexfnSMN2AP+39Ckmn92stwABZynq1JyzdT", global: "XLSX" },
  };
  const SHEET_ROWS = 5000;                           // rows of a sheet drawn; the rest is in the download
  const out = document.getElementById("out");
  const tell = message => parent.postMessage(message, "*");

  function load(lib) {
    return new Promise((ok, fail) => {
      const script = Object.assign(document.createElement("script"), { src: lib.src, integrity: lib.integrity, crossOrigin: "anonymous" });
      script.onload = () => ok(window[lib.global]);
      script.onerror = () => fail(new Error("the renderer could not load (offline?)"));
      document.head.appendChild(script);
    });
  }

  const RENDER = {
    async mermaid(source, theme) {
      const mermaid = await load(LIBS.mermaid);
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: theme === "dark" ? "dark" : "default" });
      const { svg } = await mermaid.render("diagram", String(source));
      out.className = "diagram";
      out.innerHTML = svg;
    },
    async docx(data) {
      const mammoth = await load(LIBS.docx);
      const result = await mammoth.convertToHtml({ arrayBuffer: data });
      out.className = "paper";
      out.innerHTML = `<div class="docx">${result.value}</div>`;
    },
    async sheet(data) {
      const XLSX = await load(LIBS.sheet);
      const book = XLSX.read(data, { type: "array", sheetRows: SHEET_ROWS + 1, cellDates: true });
      const tabs = document.createElement("div"), sheet = document.createElement("div");
      tabs.className = "tabs"; sheet.className = "sheet";
      const show = name => {
        sheet.innerHTML = XLSX.utils.sheet_to_html(book.Sheets[name], { header: "", footer: "" });
        const ref = book.Sheets[name]["!fullref"] || book.Sheets[name]["!ref"];
        const rows = ref ? XLSX.utils.decode_range(ref).e.r + 1 : 0;
        if (rows > SHEET_ROWS) sheet.insertAdjacentHTML("beforeend", `<div class="limit">Showing ${SHEET_ROWS} of ${rows} rows.</div>`);
        for (const b of tabs.children) b.classList.toggle("on", b.textContent === name);
      };
      for (const name of book.SheetNames) {
        const b = document.createElement("button");
        b.textContent = name;
        b.addEventListener("click", () => show(name));
        tabs.appendChild(b);
      }
      out.className = "";
      out.replaceChildren(...(book.SheetNames.length > 1 ? [tabs] : []), sheet);
      sheet.style.setProperty("--top", (book.SheetNames.length > 1 ? tabs.offsetHeight : 0) + "px");
      show(book.SheetNames[0]);
    },
  };

  let started = false;
  window.addEventListener("message", async e => {
    if (e.source !== parent || started || !e.data || e.data.type !== "render" || !RENDER[e.data.kind]) return;
    started = true;
    document.documentElement.dataset.theme = e.data.theme === "dark" ? "dark" : "light";
    try {
      await RENDER[e.data.kind](e.data.data, e.data.theme);
      size();
    } catch (err) {
      out.className = "note";
      out.textContent = String((err && err.message) || err);
      tell({ type: "error", message: String((err && err.message) || err) });
    }
  });
  const size = () => tell({ type: "size", height: out.getBoundingClientRect().bottom + window.scrollY });
  new ResizeObserver(size).observe(out);                 // as it grows (images in a document load later)
  tell({ type: "ready" });
})();
