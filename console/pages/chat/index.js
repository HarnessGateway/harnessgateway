// The chat: an example of an application built on the OpenAI Responses API and nothing else of the platform's.
// Its conversations are its own, kept in this browser (chats.js). Everything else it asks the way any application
// would: POST /v1/responses to say something, continuing from previous_response_id; GET /v1/responses/{id} to show
// a reply again; the Files API for what is attached; the Containers API for what the agent made. A session another
// application started is not listed here: it is under Sessions, and can be continued from there.
//
// Each chat is one session. Turns run in the background on the platform; this page follows their events while it
// is open, and follows them again from the start when it comes back. A message sent while a turn runs interrupts
// it and steers; a message can be taken back (rewind), and any reply can be continued in a chat of its own (branch).
import { reply, replyText, said, segmentsOf, turnOf } from "../../components/conversation.js";
import { confirm, fail } from "../../components/feedback.js";
import { all, get } from "../../lib/api.js";
import { add, clear, h, icon } from "../../lib/dom.js";
import { copyText } from "../../lib/files.js";
import { bytes } from "../../lib/format.js";
import { SHOWCASE, baseName, dirName, inWork, kindOf } from "../../lib/kinds.js";
import { keep, kept } from "../../lib/storage.js";
import { begin, branch, chats, find, head, remove, rewind, save, unfinished } from "./chats.js";
import { composer as makeComposer } from "./composer.js";
import { modelLine } from "./line.js";
import { badge, sidePanel } from "./panel.js";
import { arriving, cancel, follow, hear, start } from "./turns.js";
import { ensureView, filesOf, workLinks } from "./workspace.js";

const LIST = "hg.chat.list", TILES = 12;
const final = new Map();                         // response id → the response, once it is final: a reply is read once
let takenBack = "";                              // a rewound message's words, on their way to the composer drawn next

/** What is still to be set up before a message can run, as steps that link to where each is done. */
function setup(providers, harnesses, environments) {
  const step = (number, done, title, says, href) => h("a", { class: `step${done ? " done" : ""}`, href },
    h("span", { class: "num" }, done ? "✓" : number), h("div", {}, h("b", {}, title), h("span", { class: "muted small" }, says)));
  return h("div", { class: "start" }, h("h1", {}, "Set up, then chat"),
    h("p", { class: "muted" }, "A message runs on a harness, in an environment. Three things make that possible."),
    step(1, harnesses.length > 0, "Add a harness", "An agent CLI as a package: write one here, or import one.", "#/harnesses"),
    step(2, environments.some(found => found.harnesses.length), "Add an environment that carries it", "An image, limits, and what a sandbox may reach.", "#/environments"),
    step(3, providers.length > 0, "Add a provider", "Where models are served. A harness that calls no model needs none.", "#/providers"));
}

/** Open a session another application started: a chat that continues it from its latest reply. */
async function adopt(session) {
  const known = chats.find(chat => chat.session === session), started = known || await get(`/v1/sessions/${encodeURIComponent(session)}`);
  const chat = known || begin(`${String(started.model || "session").split("/")[1] || "session"} · ${session.slice(5, 13)}`, started.model || "", {
    session, from: started.head_response_id, note: "A session started elsewhere. The next message continues it; its turns so far are under Sessions." });
  location.replace(`#/chat/${chat.id}`);
}

export async function chat(main, params) {
  main.className = "full";
  const [environments, harnesses, providers] = await Promise.all([all("/v1/environments", "name"), all("/v1/harnesses", "name"), all("/v1/providers", "name")]);
  if (!environments.some(found => found.harnesses.length)) return void add(clear(main), setup(providers, harnesses, environments));
  if (params[0] === "open") return void await adopt(params[1]);

  let current = find(params[0]), left = false;
  const turns = [], nodes = [];                    // each exchange's reply as it stands, and where it is drawn
  const streams = new Set();                       // the turns this page is following
  main.leave = () => { left = true; for (const controller of streams) controller.abort(); };       // they run on; the page stops listening
  const line = modelLine({ environments, harnesses, providers });
  const panel = sidePanel({ session: () => current && current.session, refresh: () => listFiles() });
  const composer = makeComposer({ onSend: send, onStop: stop });
  const list = h("div", { class: "rows" }), log = h("div", { class: "log" }), sessionLabel = h("span", { class: "session mono" });
  const layout = h("div", { class: `chat${kept(LIST, true) ? "" : " nolist"}` });
  const running = () => turns.findLast(turn => turn && turn.live);
  const toggleList = () => { keep(LIST, layout.classList.toggle("nolist") === false); };
  const fresh = () => { location.hash = "#/chat"; };
  const listFiles = async () => { if (current && current.session) panel.setFiles(await filesOf(current.session)); };

  // ── the conversation ─────────────────────────────────────────────────────────────────────────────────────────
  const act = (glyph, label, title, onclick) => h("button", { class: "quiet", title, onclick }, icon(glyph), label);
  /** What is drawn under a reply: the files its turn made, and what may be done with it. */
  function under(index) {
    const turn = turns[index], exchange = current.exchanges[index], listed = new Map(panel.files().map(file => [file.path, file]));
    return [(exchange.files || []).length > 0 && h("div", { class: "tiles" }, exchange.files.map(path => h("button", { class: "tile", title: path, onclick: () => panel.openFile(path) },
        badge(path), h("span", {}, h("b", {}, baseName(path)), h("small", {}, dirName(path) || "work", listed.has(path) && ` · ${bytes(listed.get(path).bytes)}`))))),
      !turn.live && h("div", { class: "actions" },
        replyText(turn) && act("copy", "", "Copy", event => copied(event.currentTarget, replyText(turn))),
        turn.id && turn.status && act("branch", "Branch", "Branch from here: a new chat that continues from this reply; this one stays as it is",
          () => { location.hash = `#/chat/${branch(current, index).id}`; }))];
  }
  /** Copy words, and have the button say so for a moment. */
  const copied = async (button, words) => { await copyText(words); button.classList.add("done"); setTimeout(() => button.classList.remove("done"), 1200); };
  function drawTurn(index) {
    const turn = turns[index];
    if (turn.items) turn.segments = segmentsOf(turn.items, turn.arriving);
    turn.unfolded = turn.unfolded || new Set();
    reply(nodes[index], turn, { ...workLinks(current.session), open: panel.openFile, show: panel.show, below: under(index) });
  }
  const dirty = new Set();
  let frame = 0;
  /** Draw a reply again, at most once a frame, and stay at the end when the reader is there. */
  function redraw(index) {
    dirty.add(index);
    frame = frame || requestAnimationFrame(() => {
      const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
      frame = 0;
      for (const each of dirty) if (!left && nodes[each]) drawTurn(each);
      dirty.clear();
      if (atEnd) log.scrollTop = log.scrollHeight;
    });
  }
  /** One exchange's place in the log: what was said, and an empty reply to draw into. */
  function place(index) {
    const exchange = current.exchanges[index];
    nodes[index] = h("div", { class: "reply" });
    add(log, said(exchange.text, exchange.attachments, h("div", { class: "actions" },
        act("copy", "", "Copy", event => copied(event.currentTarget, exchange.text)),
        act("rewind", "Rewind", "Rewind: take this message back, edit it and send again; the conversation continues from before it, in a new branch", () => {
          if (running()) return fail(new Error("The agent is still working: stop it, then rewind."));
          takenBack = rewind(current, index);
          location.hash === `#/chat/${current.id}` ? window.dispatchEvent(new Event("hashchange")) : location.hash = `#/chat/${current.id}`;
        }))),
      exchange.note && h("div", { class: "notice" }, exchange.note), nodes[index]);
  }
  const notice = () => current && current.note && add(log, h("div", { class: "notice chat-note" }, current.note));

  // ── turns ────────────────────────────────────────────────────────────────────────────────────────────────────
  /** Read one turn's events into the conversation. `begin(onEvent, signal)` starts the turn, or follows one that
   *  is running. `before` is the session's files as they were when the turn was started here. */
  async function watch(index, turn, begin, before) {
    const chat = current, exchange = chat.exchanges[index], controller = new AbortController();
    turns[index] = turn;
    streams.add(controller);
    composer.setBusy(true);
    try {
      await begin(event => {
        if (hear(turn, event)) {
          Object.assign(exchange, { response: turn.id });
          Object.assign(chat, { session: turn.session || chat.session, note: "" });
          if (exchange.stopping) cancel(turn.id).catch(() => {});
          ensureView(chat.session).catch(() => {});      // so that images in the reply show
          save(); drawList();
          sessionLabel.textContent = chat.session;
          log.querySelector(".chat-note")?.remove();
        }
        redraw(index);
      }, controller.signal);
      if (!turn.id) Object.assign(turn, { live: false, status: "failed", error: "the turn did not start" });
    } catch (error) {
      if (error.name === "AbortError") return;           // the reader went elsewhere: the turn runs on
      if (!turn.id) Object.assign(turn, { live: false, status: "failed", error: error.message });
    } finally {
      streams.delete(controller);
    }
    if (left) return;
    if (turn.live) {                                     // the connection dropped: follow it again from its first event
      return void setTimeout(() => left || watch(index, Object.assign(arriving(turn.model), { id: turn.id }), (onEvent, signal) => follow(turn.id, onEvent, signal)), 2000);
    }
    exchange.status = turn.status;
    turn.interrupted = exchange.interrupted;
    save(); drawList();
    composer.setBusy(Boolean(running()));
    redraw(index);
    if (before && turn.status === "completed") line.remember();
    if (chat.session) await made(index, turn, before).catch(() => {});
  }
  /** After a turn: the files as it left them, tiles for what it made or changed, and what it built opened. */
  async function made(index, turn, before) {
    const exchange = current.exchanges[index], listed = await filesOf(current.session);
    if (left) return;
    panel.setFiles(listed);
    if (!before) return;                                 // a turn this page did not see start: what changed is not known
    const byPath = new Map(listed.files.map(file => [file.path, file])), told = JSON.stringify(turn.segments);
    const changed = new Set(turn.segments.flatMap(segment => segment.steps || []).filter(each => each.path && each.verb !== "Deleted").map(each => inWork(each.path)));
    for (const file of listed.files) { const was = before.get(file.path); if (!was || was.bytes !== file.bytes || was.at !== file.at) changed.add(file.path); }
    const rank = path => SHOWCASE.includes(kindOf(path)) ? SHOWCASE.indexOf(kindOf(path)) : SHOWCASE.length, named = path => told.includes(baseName(path)) ? 0 : 1;
    // the best kind first, then what the turn named, then the newest
    exchange.files = [...changed].filter(path => byPath.has(path)).sort((a, b) => rank(a) - rank(b) || named(a) - named(b) || byPath.get(b).at - byPath.get(a).at).slice(0, TILES);
    save();
    const built = exchange.files.find(path => SHOWCASE.includes(kindOf(path)));
    if (built && turn.status === "completed" && index === current.exchanges.length - 1) panel.openFile(built);
    redraw(index);
  }

  function send(text, attachments) {
    const wrong = line.wrong(), model = line.value(), interrupted = running();
    if (wrong) { fail(new Error(wrong)); return false; }
    if (interrupted && !interrupted.id) return false;    // still starting: a moment later it can be interrupted
    if (interrupted) {                                   // stop it: this message continues from where it stopped
      current.exchanges[turns.indexOf(interrupted)].interrupted = true;
      cancel(interrupted.id).catch(() => {});
    }
    const previous = interrupted ? interrupted.id : current && head(current);
    if (!current) {
      current = begin(text.slice(0, 60), model);
      history.replaceState(null, "", `#/chat/${current.id}`);
      clear(log);
    }
    const before = current.exchanges.length ? current.model.split("/")[1] : "", index = current.exchanges.length;
    current.exchanges.push({ text, attachments, response: null, note: before && before !== line.harness
      ? `Handed over to ${line.harness}: it has the same files, and is told where ${before} kept its notes.` : "" });
    Object.assign(current, { model, note: "" });
    save(); drawList();
    log.querySelector(".chat-note")?.remove();
    place(index);
    log.scrollTop = log.scrollHeight;
    watch(index, arriving(model), (onEvent, signal) => start({ model, text, attachments: attachments.map(file => file.id), previous }, onEvent, signal),
      new Map(panel.files().map(file => [file.path, file])));
  }
  function stop() {
    const turn = running();
    if (!turn) return;
    current.exchanges[turns.indexOf(turn)].stopping = true;      // before it has started: it is stopped as soon as it has an id
    if (turn.id) cancel(turn.id).catch(fail);
  }

  // ── the list of chats ────────────────────────────────────────────────────────────────────────────────────────
  function rename(chat, label) {
    const input = h("input", { class: "rename", value: chat.title, maxlength: 120 });
    let done = false;
    const end = keepIt => {
      if (done) return;
      done = true;
      if (keepIt && input.value.trim()) { chat.title = input.value.trim(); save(); }
      drawList();
    };
    input.onkeydown = event => { if (event.key === "Enter") end(true); else if (event.key === "Escape") end(false); };
    input.onblur = () => end(true);
    input.onclick = event => event.preventDefault();
    label.replaceWith(input);
    input.focus(); input.select();
  }
  function drawList() {
    add(clear(list), chats.map(chat => {
      const label = h("span", {}, chat.title), working = chat === current ? running() : unfinished(chat);
      const tool = (glyph, title, onclick) => h("button", { class: "quiet", title, onclick: event => { event.preventDefault(); onclick(); } }, icon(glyph));
      return h("a", { href: `#/chat/${chat.id}`, class: chat === current ? "on" : "", title: chat.title, ondblclick: () => rename(chat, label) }, label,
        working && h("i", { class: "working", title: "Working" }), tool("rename", "Rename", () => rename(chat, label)),
        tool("close", "Remove from this browser", () => confirm(`Remove "${chat.title}" from this browser? Work that is running is stopped. Its session stays under Sessions.`, () => {
          const last = chat.exchanges[chat.exchanges.length - 1];
          if (unfinished(chat)) cancel(last.response).catch(() => {});
          remove(chat);
          chat === current ? fresh() : drawList();
        }, "Remove")));
    }));
  }

  // ── the page ─────────────────────────────────────────────────────────────────────────────────────────────────
  const middle = h("div", { class: "middle", ondragover: event => event.preventDefault(),
    ondrop: event => { event.preventDefault(); if (event.dataTransfer && event.dataTransfer.files.length) composer.attach(event.dataTransfer.files); } },
    h("div", { class: "chat-head" },
      h("button", { class: "quiet square show-list", title: "Show chats", onclick: toggleList }, icon("side")),
      h("button", { class: "quiet square show-list", title: "New chat", onclick: fresh }, icon("plus")),
      line.node, sessionLabel, h("button", { class: "quiet square", title: "The session's files", onclick: () => panel.toggle() }, icon("panel"))),
    log, composer.node);
  add(clear(main), add(layout, h("div", { class: "list" }, h("div", { class: "list-head" },
      h("button", { onclick: fresh }, icon("plus"), "New chat"), h("button", { class: "quiet square", title: "Hide chats", onclick: toggleList }, icon("side"))), list),
    middle, panel.node));
  line.set((current || chats[0] || {}).model || `${environments.find(found => found.harnesses.length).name}/`);
  sessionLabel.textContent = (current && current.session) || "";
  drawList();
  if (takenBack) { composer.set(takenBack); takenBack = ""; } else composer.focus();
  for (const chat of chats) if (chat !== current && unfinished(chat)) {      // what became of work left running in another chat
    const last = chat.exchanges[chat.exchanges.length - 1];
    get(`/v1/responses/${last.response}`).then(response => response.status, () => "failed").then(status => {
      if (!["queued", "in_progress"].includes(status)) { last.status = status; save(); if (!left) drawList(); }
    });
  }

  if (!current || !current.exchanges.length) add(log, h("div", { class: "hello" }, h("h1", {}, "What should the agent do?"),
    h("p", { class: "muted" }, "Each chat is one session: its files and the agent's own memory carry from message to message, and every message runs in a fresh sandbox. What it makes opens on the right.")));
  if (current) {
    if (current.session) ensureView(current.session).then(listFiles).catch(() => {});
    await Promise.all(current.exchanges.map(async (exchange, index) => {     // this chat so far: each reply read again from the API
      const failed = error => ({ model: current.model, segments: [], status: "failed", error });
      if (!exchange.response) return void (turns[index] = failed("this message did not reach the platform"));
      const response = final.get(exchange.response) || await get(`/v1/responses/${exchange.response}`).catch(() => null);
      if (!response) turns[index] = failed("this response is no longer stored");
      else if (["queued", "in_progress"].includes(response.status)) turns[index] = null;       // followed, below
      else {
        final.set(response.id, response);
        turns[index] = { ...turnOf(response), interrupted: exchange.interrupted };
        if (exchange.status !== response.status) { exchange.status = response.status; save(); }
      }
    }));
    if (left) return;
    current.exchanges.forEach((exchange, index) => {
      place(index);
      if (turns[index]) drawTurn(index);
      else watch(index, Object.assign(arriving(current.model), { id: exchange.response }), (onEvent, signal) => follow(exchange.response, onEvent, signal));
    });
    notice();
    drawList();
    log.scrollTop = log.scrollHeight;
  }
}
