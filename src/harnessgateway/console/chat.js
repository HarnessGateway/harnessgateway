// The chat: an example of an application built on the OpenAI Responses API and nothing else of the platform's.
// Its conversations are its own, kept in this browser: a title, the model line last used, and the id of each
// response it was given. Everything else it asks the way any application would: POST /v1/responses to say
// something (continuing from previous_response_id), GET /v1/responses/{id} to show a reply again, and the
// Containers API to list what the agent made. A session another application started is not here: it is under
// Sessions. Changing the model line changes only the request's `model`: the next turn runs somewhere else, with
// something else, on the same files.
import { all, containerOf, get, post, stream } from "./api.js";
import { reply, replyNode, said, turnOf } from "./transcript.js";
import { add, bytes, clear, confirm, fail, h, icon } from "./ui.js";

const CHATS = "hg.chats", MODELS = "hg.models", KEPT = 200;
const LAST = new Set(["response.completed", "response.failed", "response.incomplete"]);
const read = (key, empty) => { try { return JSON.parse(localStorage.getItem(key)) || empty; } catch { return empty; } };
const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* full or blocked: it works until the page closes */ } };

/** What is still to be set up before a message can run, as steps that link to where each is done. */
function start(providers, harnesses, environments) {
  const step = (number, done, title, says, href) => h("a", { class: `step${done ? " done" : ""}`, href },
    h("span", { class: "num" }, done ? "✓" : number), h("div", {}, h("b", {}, title), h("span", { class: "muted small" }, says)));
  return h("div", { class: "start" }, h("h1", {}, "Set up, then chat"),
    h("p", { class: "muted" }, "A message runs on a harness, in an environment. Three things make that possible."),
    step(1, harnesses.length > 0, "Add a harness", "An agent CLI as a package: write one here, or import one.", "#/harnesses"),
    step(2, environments.some(e => e.harnesses.length), "Add an environment that carries it", "An image, limits, and what a sandbox may reach.", "#/environments"),
    step(3, providers.length > 0, "Add a provider", "Where models are served. A harness that calls no model needs none.", "#/providers"));
}

export async function chat(main, params) {
  main.className = "full";
  const [environments, harnesses, providers] = await Promise.all([all("/v1/environments", "name"), all("/v1/harnesses", "name"), all("/v1/providers", "name")]);
  if (!environments.some(found => found.harnesses.length)) return add(clear(main), start(providers, harnesses, environments));
  const list = h("div", { class: "list" }), log = h("div", { class: "log" }), made = h("div", { class: "made" });
  const environmentPick = h("select", { title: "Environment" }), harnessPick = h("select", { title: "Harness" }), providerPick = h("select", { title: "Provider" });
  const modelPick = h("input", { title: "Model", placeholder: "model" }), calls = h("span", { style: "display:contents" }, "/", providerPick, "/", modelPick);
  const input = h("textarea", { rows: 1, placeholder: "Message the agent…" });
  const send = h("button", { class: "primary square", title: "Send" }, icon("send")), stop = h("button", { class: "square", title: "Stop", hidden: true }, icon("stop"));
  // This browser's chats, newest first: {id, title, model, session, exchanges: [{text, response}]}.
  const chats = read(CHATS, []), used = read(MODELS, {});
  let current = chats.find(found => found.id === params[0]) || null, running = null;
  const keep = () => write(CHATS, chats.slice(0, KEPT));
  const head = () => current && current.exchanges.map(exchange => exchange.response).filter(Boolean).pop();

  // the model line: four parts, each offered only what can follow the one before it
  const harness = () => harnesses.find(found => found.name === harnessPick.value);
  const alone = () => !harness() || !harness().manifest.apis.length;          // it calls no model: none/none
  const line = () => [environmentPick.value, harnessPick.value, alone() ? "none" : providerPick.value, alone() ? "none" : modelPick.value.trim()].join("/");
  const offer = (select, names, wanted) => add(clear(select), names.map(name => h("option", { value: name, selected: name === wanted }, name)));
  const offerProviders = (wanted, model) => {
    offer(providerPick, harness() ? providers.filter(found => harness().manifest.apis.some(api => api in found.apis)).map(found => found.name) : [], wanted);
    calls.hidden = alone();
    modelPick.value = model || used[providerPick.value] || "";
  };
  const offerHarnesses = (wanted, ...rest) => {
    offer(harnessPick, (environments.find(found => found.name === environmentPick.value) || { harnesses: [] }).harnesses, wanted);
    offerProviders(...rest);
  };
  const pick = model => {
    const [environment, harnessName, provider, ...rest] = String(model || "").split("/");
    offer(environmentPick, environments.map(found => found.name), environment);
    offerHarnesses(harnessName, provider, rest.join("/") === "none" ? "" : rest.join("/"));
  };
  environmentPick.onchange = () => offerHarnesses(); harnessPick.onchange = () => offerProviders(); providerPick.onchange = () => offerProviders(providerPick.value);

  async function showMade() {
    clear(made).append(h("h2", {}, "Files"));
    if (!current || !current.session) return add(made, h("p", { class: "muted small" }, "What the agent writes in /work shows here."));
    const container = containerOf(current.session), files = await all(`/v1/containers/${container}/files`).catch(() => []);
    const open = async file => {
      const content = await (await get(`/v1/containers/${container}/files/${file.id}/content`, { raw: true })).blob();
      const link = h("a", { href: URL.createObjectURL(content), download: file.path.split("/").pop() });
      link.click();
      URL.revokeObjectURL(link.href);
    };
    add(made, files.length ? files.map(file => h("div", { class: "file" }, h("button", { class: "link", onclick: () => open(file).catch(fail) }, file.path.replace(/^\/work\//, "")),
      h("span", {}, bytes(file.bytes)))) : h("p", { class: "muted small" }, "Nothing yet."));
  }

  function showList() {
    add(clear(list), h("button", { onclick: () => { location.hash = "#/chat"; } }, icon("plus"), "New chat"),
      chats.map(found => h("a", { href: `#/chat/${found.id}`, class: found === current ? "on" : "", title: found.title }, h("span", {}, found.title),
        h("button", { class: "quiet", title: "Remove from this browser", onclick: event => {
          event.preventDefault();
          confirm(`Remove "${found.title}" from this browser? Its session stays under Sessions.`, () => {
            chats.splice(chats.indexOf(found), 1); keep();
            found === current ? location.hash = "#/chat" : showList();
          }, "Remove");
        } }, icon("close")))));
  }

  /** Draw one turn as its events arrive: one that is started here, or one that is already running. */
  async function follow(exchange, turn, begin) {
    const node = log.appendChild(h("div", { class: "reply" }));
    const draw = () => { reply(node, turn); log.scrollTop = log.scrollHeight; };
    running = new AbortController();
    send.hidden = true; stop.hidden = false;
    draw();
    try {
      await begin(event => {
        if (event.type === "response.created") {
          exchange.response = turn.id = event.response.id;
          turn.model = current.model = event.response.model;
          current.session = event.response.metadata.hg_session;
          keep();
        } else if (event.type === "response.output_text.delta") turn.streamed = (turn.streamed || "") + event.delta;
        else if (event.type === "response.reasoning_summary_text.delta") turn.thinking = (turn.thinking || "") + event.delta;
        else if (event.type === "response.output_item.done") {      // the item whole: what arrived of it in pieces is done with
          turn.items.push(event.item);
          if (event.item.type === "message") turn.streamed = "";
          if (event.item.type === "reasoning") turn.thinking = "";
        }
        else if (LAST.has(event.type)) Object.assign(turn, turnOf(event.response));
        draw();
      }, running.signal);
    } catch (error) {
      if (error.name !== "AbortError") { turn.status = "failed"; turn.error = error.message; draw(); }
    } finally {
      running = null; send.hidden = false; stop.hidden = true;
      showMade().catch(() => {});
    }
  }

  async function submit() {
    const words = input.value.trim();
    if (!words || running) return;
    if (!alone() && !modelPick.value.trim()) return fail(new Error("Name the model: any model the provider serves."));
    input.value = ""; input.style.height = "";
    if (!alone()) { used[providerPick.value] = modelPick.value.trim(); write(MODELS, used); }
    const previous = head();
    if (!current) {
      current = { id: crypto.randomUUID().slice(0, 13), title: words.slice(0, 60), model: line(), session: null, exchanges: [] };
      chats.unshift(current);
      history.replaceState(null, "", `#/chat/${current.id}`);
    }
    const exchange = { text: words, response: null };
    current.exchanges.push(exchange);
    keep(); showList();
    log.querySelector(".empty")?.remove();
    log.append(said(words));
    const body = { model: line(), input: words, stream: true, background: true, ...(previous && { previous_response_id: previous }) };
    await follow(exchange, { model: line(), items: [], status: "in_progress" }, (onEvent, signal) => stream("POST", "/v1/responses", body, onEvent, signal));
  }
  send.onclick = submit;
  stop.onclick = () => head() && post(`/v1/responses/${head()}/cancel`).catch(fail);
  input.onkeydown = event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); } };
  input.oninput = () => { input.style.height = ""; input.style.height = `${Math.min(input.scrollHeight, 200)}px`; };

  add(clear(main), h("div", { class: "chat" }, list, h("div", { class: "middle" },
    h("div", { class: "line-bar" }, environmentPick, "/", harnessPick, calls), log, h("div", { class: "composer" }, input, stop, send)), made));
  pick((current || chats[0] || {}).model || `${environments.find(found => found.harnesses.length).name}/`);
  showList();
  await showMade();
  if (!current) add(log, h("div", { class: "empty", style: "max-width:520px;margin:14vh auto 0;border:0" },
    h("h1", { style: "margin-bottom:8px;color:var(--ink)" }, "What should the agent do?"), "Each chat is one session: its files and the agent's own memory carry from message to message, and every message runs in a fresh sandbox."));

  for (const exchange of current ? current.exchanges : []) {     // this chat so far: each reply read again from the API
    log.append(said(exchange.text));
    const response = exchange.response && await get(`/v1/responses/${exchange.response}`).catch(() => null);
    if (!response) {
      log.append(replyNode({ model: current.model, items: [], status: "failed",
                             error: exchange.response ? "this response is no longer stored" : "this message did not reach the platform" }));
    } else if (response.status === "queued" || response.status === "in_progress") {
      follow(exchange, { ...turnOf(response), items: [] }, (onEvent, signal) => stream("GET", `/v1/responses/${response.id}?stream=true&starting_after=0`, undefined, onEvent, signal));
    } else log.append(replyNode(turnOf(response)));
  }
  log.scrollTop = log.scrollHeight;
  input.focus();
}
