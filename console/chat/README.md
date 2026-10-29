# chatgui

A ChatGPT-style chat for HarnessGateway, and an example of an application built on the platform's public API (the
OpenAI Responses API, `/v1/containers` for workspace files, `/v1/files` for attachments). Each conversation is one
session: the agent's workspace, files and memory carry from message to message.

```bash
pip install httpx
HG_KEY=hg_... python console/chat/server.py            # --api http://localhost:8000 --host 127.0.0.1 --port 8800
```

Open http://localhost:8800. The same files are embedded as the Chat page of the console (`console/`): every
URL in the page is relative, so it works at `/` on its own and at `/chat/` under the console. To reach it from another
machine, add `--host 0.0.0.0`. The server holds the key; the page never sees it.

## What it renders

Replies stream as GitHub-flavoured markdown: tables, task lists, callouts, math (KaTeX), highlighted code with Copy,
Mermaid diagrams, and Preview for HTML and SVG snippets. Links to workspace files open them in the side panel.

The side panel lists the session's workspace files and renders what the agent made, whatever its size:

| Kind | Rendered as |
|---|---|
| HTML | the page, running, in a sandboxed frame (with its CSS, scripts and assets from the workspace) |
| Images, SVG | the image, click to zoom; SVG also as source |
| PDF, audio, video | the browser's viewer and players; media seeks (the server serves byte ranges) |
| Markdown | the document, with relative links and images resolved in the workspace; or its source |
| Jupyter notebooks | cells, outputs (text, images, HTML tables, errors) and math |
| CSV, TSV | a table, a thousand rows at a time |
| JSON, code, text | numbered lines, highlighted; the first 8 MB of a larger file, with a link to the rest |
| Word (.docx), spreadsheets (.xlsx, .xls, .ods), Mermaid (.mmd) | rendered in the viewer (below) |
| Archives, binaries | a download |

## Security

- What the agent made is untrusted. Workspace files are served with `Content-Security-Policy: sandbox`, so a page the
  agent built runs in an origin of its own: it cannot read the chat or call this server as the chat.
- Markdown is escaped before it is rendered; HTML inside it passes a DOM-based allow-list of tags and attributes.
  Links to other sites open in a new tab; images load only from the workspace or `data:` URIs, never from a host on
  the web, so a document cannot send anything anywhere by being shown.
- Word documents, spreadsheets and diagrams are parsed by their libraries in the viewer (`/viewer`), a sandboxed frame
  that receives only the bytes of the file it shows.
- The page's own Content-Security-Policy allows scripts from this server and from `cdn.jsdelivr.net` only. Libraries
  (highlight.js, KaTeX, Mermaid, mammoth, SheetJS) are pinned and checked by integrity hashes, and load on first use.
  Offline, everything still reads: code unhighlighted, math as TeX, diagrams and documents as source or a download.
- The server accepts JSON requests from its own page only (Content-Type and Origin are checked).

## Layout

| File | |
|---|---|
| `server.py` | The HTTP server: the page and its files, a relay of turns (server-sent events), workspace files, attachments |
| `static/app.js` | Conversations, streaming, the composer, the harness / provider / model picker |
| `static/markdown.js` | The markdown renderer and the HTML allow-list |
| `static/rich.js` | Highlighting, math, code blocks and diagrams; the viewer frame |
| `static/panel.js`, `static/preview.js` | The side panel, and the preview of each kind of file |
| `static/viewer.html`, `static/viewer.js` | The sandboxed viewer for diagrams, Word documents and spreadsheets |
| `static/util.js`, `static/app.css`, `static/index.html` | Helpers, styles, markup |

Conversations are kept in the browser (localStorage); a chat is renamed from its row (the pencil, or a double
click), and the chat list collapses like the file panel (the state is remembered). Workspace files are read from the session's last save; after a
turn, the page waits for the save to land (`metadata.state` of `GET /v1/containers/{id}`), then opens what the turn
made.
