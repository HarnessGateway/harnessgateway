// Chat (spec §20): talk to any harness; choose what each message runs with; watch it work; continue, stop, fork.

import { download, get, post, stream } from "/api.js";
import { attempt, bytes, clear, field, h, put, status } from "/ui.js";

const TERMINAL = ["response.completed", "response.failed", "response.incomplete"];

// What the conversation is: the turns shown, and the response the next message continues from.
const chat = { turns: [], from: null, running: null };

async function choices(role) {
  const developer = role !== "runner";
  const [harnesses, environments, providers] = await Promise.all([
    developer ? get("/v1/harnesses?limit=100") : get("/v1/models"),
    developer ? get("/v1/environments?limit=100") : { data: [] },
    developer ? get("/v1/providers?limit=100") : { data: [] },
  ]);
  return {
    harnesses: harnesses.data.map((harness) => harness.name || harness.id),
    environments: environments.data.map((environment) => environment.name),
    providers: providers.data.map((provider) => provider.name),
  };
}

function modelString(environment, harness, provider, model) {
  let text = environment && environment !== "default" ? `${environment}:${harness}` : harness;
  if (provider) text += `/${provider}`;
  if (provider && model) text += `/${model}`;
  return text;
}

// One output item that is not text: a command, a file change, a tool call.
function tool(item) {
  if (item.type === "shell_call") return ["$ " + item.action.commands.join(" && "), null];
  if (item.type === "shell_call_output") {
    const out = item.output.map((part) => part.stdout + part.stderr).join("");
    const code = item.output[0]?.outcome?.exit_code;
    return [`exit ${code ?? "?"}`, out];
  }
  if (item.type === "apply_patch_call") {
    const operation = item.operation;
    return [`${operation.type.replace("_file", "")} ${operation.path}`, operation.diff || null];
  }
  if (item.type === "mcp_call") return [`${item.server_label}.${item.name}(${item.arguments})`, item.output || null];
  if (item.type === "custom_tool_call") return [`${item.name} ${item.input}`.slice(0, 200), null];
  if (item.type === "custom_tool_call_output") return ["result", typeof item.output === "string" ? item.output : null];
  return null;
}

function turnView(turn, onFork) {
  const answer = h("div", { class: "answer" });
  const foot = h("div", { class: "foot" });
  const blocks = {};                                    // item id -> the element its text streams into
  const view = h("div", { class: "turn" }, h("div", { class: "said" }, turn.input), answer, foot);

  const block = (id, kind) => {
    if (!blocks[id]) put(answer, blocks[id] = h("div", { class: kind }));
    return blocks[id];
  };
  const showFoot = () => {
    clear(foot);
    if (turn.status) put(foot, status(turn.status));
    put(foot, h("span", { class: "mono" }, turn.model));
    if (turn.id) put(foot, h("span", { class: "mono" }, turn.id));
    if (turn.reason) put(foot, h("span", {}, `reason: ${turn.reason}`));
    if (turn.error) put(foot, h("span", {}, turn.error));
    if (turn.id && turn.status && !["queued", "in_progress"].includes(turn.status)) {
      put(foot, h("button", { onclick: () => onFork(turn) }, "Continue from here"));
    }
  };

  view.apply = (event) => {
    if (event.response) {
      turn.id = event.response.id;
      turn.status = event.response.status;
      turn.session = event.response.metadata?.hg_session;
      turn.reason = event.response.metadata?.hg_reason;
      turn.error = event.response.error?.message;
    }
    if (event.type === "response.output_text.delta") put(block(event.item_id, "text"), event.delta);
    if (event.type === "response.reasoning_summary_text.delta") put(block(event.item_id, "thinking"), event.delta);
    if (event.type === "response.output_item.done") {
      const shown = tool(event.item);
      if (shown) {
        put(answer, h("details", { class: "tool" }, h("summary", {}, shown[0]), shown[1] ? h("pre", {}, shown[1]) : null));
      }
    }
    showFoot();
  };
  showFoot();
  return view;
}

async function filesPanel(place, sessionId) {
  clear(place);
  if (!sessionId) return;
  const container = "cntr_" + sessionId.replace(/^sess_/, "");
  const files = await get(`/v1/containers/${container}/files?limit=100`);
  put(place, h("h2", {}, "Files it made"),
    files.data.length ? h("div", {}, files.data.map((file) =>
      h("div", {}, h("a", { href: "#", onclick: (event) => {
        event.preventDefault();
        download(`/v1/containers/${container}/files/${file.id}/content`, file.path.split("/").pop());
      } }, file.path.replace(/^\/work\//, "")), h("span", { class: "muted small" }, `  ${bytes(file.bytes)}`)))) :
      h("div", { class: "muted" }, "None yet."));
}

export async function render(root, context) {
  if (context.args[0]) {                                // #/chat/<response id>: continue from that response
    chat.turns = [];
    chat.from = context.args[0];
    history.replaceState(null, "", "#/chat");
  }
  const options = await choices(context.about.role);
  const log = h("div", { class: "log" });
  const files = h("div", {});
  const problem = h("div", {});
  const from = h("div", { class: "muted small" });
  const select = (values, blank) => h("select", {},
    blank ? h("option", { value: "" }, blank) : null, values.map((value) => h("option", { value }, value)));
  const environment = select(options.environments.length ? options.environments : ["default"]);
  environment.value = "default";
  const harness = select(options.harnesses);
  const provider = select(options.providers, "the harness's default");
  const model = h("input", { placeholder: "the default model" });
  const input = h("textarea", { placeholder: "Message", id: "message" });
  const send = h("button", { class: "primary" }, "Send");
  const stop = h("button", { class: "danger", disabled: true }, "Stop");

  const showFrom = () => {
    put(clear(from), chat.from ? `The next message continues from ${chat.from}.` : "The next message starts a new session.");
  };
  const fork = (turn) => { chat.from = turn.id; showFrom(); input.focus(); };
  const busy = (running) => { send.disabled = running; stop.disabled = !running; };

  for (const turn of chat.turns) put(log, turnView(turn, fork));
  showFrom();

  send.onclick = () => attempt(problem, async () => {
    const text = input.value.trim();
    if (!text) return;
    if (!harness.value) throw new Error("Add a harness first: Harnesses, Add.");
    const turn = { input: text, model: modelString(environment.value, harness.value, provider.value, model.value.trim()) };
    chat.turns.push(turn);
    const view = turnView(turn, fork);
    put(log, view);
    log.scrollTop = log.scrollHeight;
    input.value = "";
    busy(true);
    chat.running = turn;
    try {
      await stream("/v1/responses", { model: turn.model, input: text, stream: true, previous_response_id: chat.from || undefined },
        (event) => {
          view.apply(event);
          log.scrollTop = log.scrollHeight;
          if (TERMINAL.includes(event.type)) chat.from = event.response.id;      // the next message continues it
        });
    } finally {
      busy(false);
      chat.running = null;
      showFrom();
      await filesPanel(files, turn.session).catch(() => {});
    }
  });
  stop.onclick = () => attempt(problem, async () => {
    if (chat.running?.id) await post(`/v1/responses/${chat.running.id}/cancel`);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) send.click();
  });

  put(root, h("div", { class: "chat" },
    h("div", { class: "row" }, h("h1", {}, "Chat"),
      h("button", { class: "fit", onclick: () => { chat.turns = []; chat.from = null; clear(log); clear(files); showFrom(); } },
        "New chat")),
    log, files,
    h("div", { class: "composer" },
      h("div", { class: "row" },
        field("Environment", environment), field("Harness", harness), field("Provider", provider), field("Model", model)),
      field("Message (Ctrl+Enter sends)", input), from, problem,
      h("div", { class: "row" }, h("span", {}), send, stop))));
  const last = chat.turns.at(-1);
  if (last?.session) filesPanel(files, last.session).catch(() => {});
}
