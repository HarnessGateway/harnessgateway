// A conversation as a reader follows one, drawn from Responses payloads alone: what a request's input said, and a
// response's output as the agent's words, its thinking, and the steps it took, a run of steps folded into one line
// that says what they were. The chat and a session's page both draw one.
import { add, clear, h, icon } from "../lib/dom.js";
import { inWork } from "../lib/kinds.js";
import { markdown } from "./markdown.js";
import { codeBlock, enhance } from "./rich.js";

const SHOWN = 20000;                             // the most of one step's detail or output that is drawn
const VERBS = { create_file: "Created", update_file: "Edited", delete_file: "Deleted" };

/** What a request's input said, as text: a string as it is, or the text of a list's user messages. */
export const inputText = input => typeof input === "string" ? input : (input || []).filter(item => !item.role || item.role === "user")
  .flatMap(item => typeof item.content === "string" ? [item.content] : (item.content || []).map(part => part.text || "")).filter(Boolean).join("\n");

/** One output item as a step the agent took ({kind, label, …}), or as what came of one ({output}); both carry the
 *  call's id, which is how the two are put together. */
function step(item) {
  const id = item.call_id || item.id;
  if (item.type === "shell_call") return { id, kind: "terminal", label: ((item.action || {}).commands || []).join(" && ") };
  if (item.type === "shell_call_output") {
    const results = item.output || [];
    return { id, output: results.map(result => (result.stdout || "") + (result.stderr || "")).join(""),
             exitCode: results.map(result => (result.outcome || {}).exit_code).find(Boolean) || 0 };
  }
  if (item.type === "apply_patch_call") {
    const operation = item.operation || {}, verb = VERBS[operation.type] || "Changed";
    return { id, kind: "file", verb, path: operation.path || "", label: `${verb} ${operation.path || ""}`, detail: operation.diff || "" };
  }
  if (item.type === "mcp_call") return { id, kind: "tool", label: `${item.server_label}.${item.name}`, detail: item.arguments || "",
                                         output: item.output || (item.error ? `failed: ${item.error.content || item.error.message || ""}` : "") };
  if (item.type === "web_search_call") {
    const action = item.action || {};
    return { id, kind: "search", label: action.url ? `Opened ${action.url}` : `Searched the web${action.query ? ` for ${action.query}` : ""}`,
             detail: (action.sources || []).map(source => source.url).join("\n") };
  }
  if (item.type.endsWith("_call")) return { id, kind: "tool", label: item.name || item.type.replace(/_call$/, ""), detail: item.arguments || item.input || "" };
  if (item.type.endsWith("_output")) return { id, output: typeof item.output === "string" ? item.output : JSON.stringify(item.output ?? "") };
  return null;
}

/** A response's output items as segments: {t: "text" | "thinking", text} and {t: "steps", steps}. `arriving` is
 *  what has come so far of an item that is not finished: {thinking, text}. */
export function segmentsOf(items, arriving = {}) {
  const segments = [], steps = new Map();
  for (const item of items) {
    const text = (item.content || item.summary || []).map(part => part.text || "").join("");
    if (item.type === "message") segments.push({ t: "text", text });
    else if (item.type === "reasoning") { if (text) segments.push({ t: "thinking", id: item.id, text }); }
    else {
      const found = step(item), last = segments[segments.length - 1];
      if (found && found.label !== undefined) {
        steps.set(found.id, found);
        if (last && last.t === "steps") last.steps.push(found); else segments.push({ t: "steps", steps: [found] });
      } else if (found && steps.has(found.id)) Object.assign(steps.get(found.id), { output: found.output, exitCode: found.exitCode });
    }
  }
  if (arriving.thinking) segments.push({ t: "thinking", text: arriving.thinking, arriving: true });
  if (arriving.text) segments.push({ t: "text", text: arriving.text, arriving: true });
  return segments;
}

/** A run of steps in a few words: "Ran 2 commands, edited 1 file". */
function summary(steps) {
  const count = kind => steps.filter(each => each.kind === kind).length, many = (n, what) => `${n} ${what}${n === 1 ? "" : "s"}`;
  const files = steps.filter(each => each.kind === "file"), parts = [];
  if (count("terminal")) parts.push(`ran ${many(count("terminal"), "command")}`);
  if (files.length) parts.push(`${files.every(each => each.verb === "Created") ? "created" : "edited"} ${many(files.length, "file")}`);
  if (count("tool")) parts.push(`used ${many(count("tool"), "tool")}`);
  if (count("search")) parts.push("searched the web");
  const said = parts.join(", ") || many(steps.length, "step");
  return said[0].toUpperCase() + said.slice(1);
}

/** The reply's words alone, as they are copied. */
export const replyText = turn => turn.segments.filter(segment => segment.t === "text").map(segment => segment.text).join("\n\n").trim();

/**
 * Draw a reply as it stands into `node`. `turn` is {model, segments, status, error} and, in a chat, {live, thought,
 * unfolded}: whether it is still arriving, how long each finished thought took, and which folds the reader opened.
 * `o` is how a reader acts on it: link(url), image(url) and open(path) as the markdown takes them, show(view) for a
 * page or a diagram from a code block, and `below`, what is drawn under the reply.
 */
export function reply(node, turn, o = {}) {
  const live = Boolean(turn.live), unfolded = turn.unfolded || new Set(), last = turn.segments[turn.segments.length - 1];
  const fold = (index, label, working, body) => h("details", { open: unfolded.has(index),
    ontoggle: event => { event.target.open ? unfolded.add(index) : unfolded.delete(index); } },
    h("summary", {}, h("span", { class: working ? "shimmer" : null }, label), icon("chevron")), h("div", { class: "body" }, body));
  add(clear(node), h("div", { class: "who" }, turn.model), turn.segments.map((segment, index) => {
    if (segment.t === "thinking") {
      const seconds = turn.thought && turn.thought[segment.id];
      return fold(index, segment.arriving ? "Thinking" : seconds ? `Thought for ${seconds}s` : "Thought", live && segment === last && segment.arriving, segment.text);
    }
    if (segment.t === "steps") return fold(index, summary(segment.steps), live && segment === last, segment.steps.map(each => h("div", { class: "step" },
      h("div", { class: "label" }, icon(each.kind === "search" ? "search" : each.kind),
        each.path && o.open ? h("span", {}, `${each.verb} `, h("a", { href: "#", onclick: event => { event.preventDefault(); o.open(inWork(each.path)); } }, each.path))
          : h("span", {}, each.label)),
      (each.output || each.detail) && h("pre", {}, String(each.output || each.detail).slice(0, SHOWN)),
      each.exitCode ? h("div", { class: "bad small" }, `exit ${each.exitCode}`) : null)));
    return h("div", { class: "text" }, markdown(segment.text, { ...o, breaks: true, code: (code, language, closed) => codeBlock(code, language, closed, o.show) }));
  }),
  turn.error ? h("div", { class: "state bad" }, turn.error)
    : turn.status && turn.status !== "completed" && !live && h("div", { class: "state" }, turn.status === "cancelled" ? (turn.interrupted ? "Interrupted" : "Stopped") : turn.status),
  live && (!last || last.t === "text") && h("span", { class: "working", title: "Working" }),
  o.below);
  enhance(node, { diagrams: !live });
  return node;
}

/** A response object as `reply` draws it. */
export const turnOf = response => ({ id: response.id, model: response.model, segments: segmentsOf(response.output || []), status: response.status,
  error: response.error ? `${(response.metadata || {}).hg_code || response.error.code || "failed"}: ${response.error.message || ""}` : "" });
/** What someone said, with the files they gave and what may be done with it. */
export const said = (words, attachments = [], below = null) => h("div", { class: "said" }, h("div", { class: "bubble" }, words),
  attachments.length > 0 && h("div", { class: "attached" }, attachments.map(file => h("span", { class: "pill" }, file.name))), below);
export const replyNode = (turn, o) => reply(h("div", { class: "reply" }), turn, o);
