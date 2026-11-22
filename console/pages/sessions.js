// Sessions: every session of the organization, whichever application started it, each shown as the conversation
// its requests and responses make. One call gives a session's turns with both payloads.
import { inputText, replyNode, said, turnOf } from "../components/conversation.js";
import { confirm } from "../components/feedback.js";
import { mono, pill, STATUS, top } from "../components/layout.js";
import { del, get } from "../lib/api.js";
import { add, clear, h, icon } from "../lib/dom.js";
import { ago, bytes } from "../lib/format.js";

async function session(main, id) {
  const [summary, turns] = await Promise.all([get(`/v1/sessions/${id}`), get(`/v1/sessions/${id}/turns`)]);
  add(clear(main), h("div", { class: "column" }, h("a", { class: "crumb", href: "#/sessions" }, icon("back"), "Sessions"),
    h("div", { class: "head", style: "margin-bottom:28px" },
      h("div", {}, h("h1", { class: "mono", style: "font-size:17px" }, id),
        h("div", { class: "sub" }, `${summary.turns} turn${summary.turns === 1 ? "" : "s"} · ${bytes(summary.bytes)} saved · last active ${ago(summary.last_active_at)}`,
          summary.forked_from_response_id && ` · forked from ${summary.forked_from_response_id}`)),
      h("div", { class: "acts" }, summary.head_response_id && h("a", { href: `#/chat/open/${id}` }, h("button", {}, "Continue in chat")),
        h("button", { class: "quiet danger", onclick: () => confirm(`Delete ${id}, with its turns, its snapshots and its files?`,
        async () => { await del(`/v1/sessions/${id}`); location.hash = "#/sessions"; }) }, "Delete"))),
    turns.data.map(turn => {
      const raw = h("div", { class: "raw", hidden: true }, turn.log && [h("h2", {}, "Log"), h("pre", {}, turn.log)],
        h("h2", {}, "Request"), h("pre", {}, JSON.stringify(turn.request, null, 2)), h("h2", {}, "Response"), h("pre", {}, JSON.stringify(turn.response, null, 2)));
      return [h("div", { class: "turn" }, h("b", {}, `Turn ${turn.number}`), pill(turn.status, STATUS[turn.status]), mono(turn.id), h("span", { class: "muted small" }, ago(turn.created_at)),
        !turn.deleted && h("div", { class: "acts" }, h("button", { class: "quiet", onclick: () => { raw.hidden = !raw.hidden; } }, "Payloads"))),
        turn.deleted ? h("p", { class: "muted raw" }, "This response was deleted: its request and its output are gone.")
          : [said(inputText(turn.request.input)), replyNode(turnOf(turn.response)), raw]];
    })));
}

export async function sessions(main, params) {
  if (params[0]) return session(main, params[0]);
  const page = await get("/v1/sessions?limit=100");
  add(clear(main), top("Sessions", "Every session, whichever application started it, most recently active first. Open one to read its conversation."),
    page.data.length ? h("div", { class: "panel listing" }, page.data.map(found => h("a", { href: `#/sessions/${found.id}` },
      h("div", { style: "min-width:0" }, h("div", { class: "mono" }, found.model || "—"), h("div", { class: "muted small mono" }, found.id)),
      pill(found.status || "empty", STATUS[found.status]), h("span", { class: "muted small" }, `${found.turns} turn${found.turns === 1 ? "" : "s"} · ${bytes(found.bytes)}`),
      h("span", { class: "muted small" }, ago(found.last_active_at)))))
      : h("div", { class: "empty" }, "No sessions yet. Send a request to POST /v1/responses, or start a chat."),
    page.has_more && h("p", { class: "muted small", style: "margin-top:12px" }, "Showing the 100 most recently active."));
}
