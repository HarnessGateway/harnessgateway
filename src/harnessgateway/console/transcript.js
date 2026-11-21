// A conversation as a reader sees it, drawn from Responses payloads alone: what a request's input said, and a
// response's output items in the order the agent made them: its words, its thinking, and each step it took with
// what came of it.
import { add, clear, h } from "./ui.js";

const textOf = item => (item.content || item.summary || []).map(part => part.text || "").join("");
const SHOWN = 20000;                             // the most of one step's input or output that is drawn

/** What a request's input said, as text: a string as it is, or the text of a list's user messages. */
export const inputText = input => typeof input === "string" ? input : (input || []).filter(item => !item.role || item.role === "user")
  .flatMap(item => typeof item.content === "string" ? [item.content] : (item.content || []).map(part => part.text || part.filename || part.file_id || "")).join("\n");

/** An agent's words as nodes: a fenced block as code, a `span` as code, the rest as it is. */
const prose = text => text.split(/```[^\n]*\n([\s\S]*?)```\n?/).map((part, at) => at % 2 ? h("pre", {}, part.replace(/\n$/, ""))
  : part.replace(/^\n+|\n+$/g, "").split(/`([^`\n]+)`/).map((piece, inside) => inside % 2 ? h("code", {}, piece) : piece));

/** One output item as a step the agent took ({label, body}), or as what came of one ({output}); both carry the
 *  call's id, which is how the two are put together. */
function step(item) {
  if (item.type === "shell_call") return { id: item.call_id, label: `$ ${((item.action || {}).commands || []).join(" && ")}` };
  if (item.type === "apply_patch_call") return { id: item.call_id, label: `${(item.operation || {}).type || "patch"} ${(item.operation || {}).path || ""}`, body: (item.operation || {}).diff };
  if (item.type === "mcp_call") return { id: item.id, label: `${item.server_label}.${item.name}`, body: item.arguments, output: item.output };
  if (item.type.endsWith("_call")) return { id: item.call_id || item.id, label: item.name || item.type.replace(/_call$/, ""), body: item.arguments || item.input };
  if (item.type.endsWith("_output")) return { id: item.call_id, output: Array.isArray(item.output) ? item.output.map(part => (part.stdout || "") + (part.stderr || "")).join("") : item.output };
  return null;
}
const fold = (label, ...bodies) => bodies.some(Boolean)
  ? h("details", {}, h("summary", {}, label), bodies.filter(Boolean).map(body => h("pre", {}, String(body).slice(0, SHOWN))))
  : h("div", { class: "step" }, label);

/** Draw a reply as it stands into `node`. `turn` is {model, items, status, error}, and while it streams also
 *  {streamed, thinking}: the words and the thinking that have arrived for an item that is not finished. */
export function reply(node, turn) {
  const parts = [], steps = new Map();
  for (const item of turn.items) {
    if (item.type === "message") parts.push(h("div", { class: "text" }, prose(textOf(item))));
    else if (item.type === "reasoning") parts.push(fold("Thinking", textOf(item)));
    else {
      const found = step(item);
      if (found && found.label) { steps.set(found.id, found); parts.push(found); }
      else if (found && steps.has(found.id)) steps.get(found.id).output = found.output;
    }
  }
  const waiting = turn.status === "queued" || turn.status === "in_progress";
  return add(clear(node), h("div", { class: "who" }, turn.model),
    parts.map(part => part instanceof Node ? part : fold(part.label, part.body, part.output)),
    turn.thinking && fold("Thinking", turn.thinking), turn.streamed && h("div", { class: "text" }, prose(turn.streamed)),
    turn.status !== "completed" && h("div", { class: "state" }, waiting ? "Working…" : turn.status, turn.error && h("span", { class: "bad" }, ` · ${turn.error}`)));
}

/** A response object as `reply` draws it. */
export const turnOf = response => ({ id: response.id, model: response.model, items: response.output, status: response.status,
                                     error: response.error ? response.error.message : "" });
export const said = words => h("div", { class: "said" }, h("div", {}, words));
export const replyNode = turn => reply(h("div", { class: "reply" }), turn);
