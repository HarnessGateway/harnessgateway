// What the chat asks of the Responses API: start a turn, follow one, stop one, and give a file to the next one.
// A turn runs on the platform whether or not this page keeps watching, so a page that was closed, or lost its
// connection, follows the turn again from its first event.
import { post, stream } from "../../lib/api.js";

const FINAL = new Set(["response.completed", "response.failed", "response.incomplete"]);
const BUSY_TRIES = 60, BUSY_WAIT_MS = 1000;      // a message sent while the turn before it is still ending
const sleep = ms => new Promise(done => setTimeout(done, ms));

/** A turn as the page holds one while it arrives: the items that are whole, and what has come of the one that is not. */
export const arriving = model => ({ id: null, session: null, model, items: [], arriving: { thinking: "", text: "" }, thought: {}, live: true, status: null, error: "" });

/** Take one stream event into a turn. Returns true when it is the one that names the response. */
export function hear(turn, event) {
  if (event.type === "response.created") {
    Object.assign(turn, { id: event.response.id, model: event.response.model, session: (event.response.metadata || {}).hg_session || null });
    return true;
  }
  if (event.type === "response.output_text.delta") turn.arriving.text += event.delta || "";
  else if (event.type === "response.reasoning_summary_text.delta" || event.type === "response.reasoning_text.delta") {
    turn.thinkingSince = turn.thinkingSince || Date.now();
    turn.arriving.thinking += event.delta || "";
  } else if (event.type === "response.output_item.done") {       // the item whole: what arrived of it in pieces is done with
    turn.items.push(event.item);
    if (event.item.type === "message") turn.arriving.text = "";
    if (event.item.type === "reasoning") {
      if (turn.thinkingSince) turn.thought[event.item.id] = Math.max(1, Math.round((Date.now() - turn.thinkingSince) / 1000));
      turn.thinkingSince = 0;
      turn.arriving.thinking = "";
    }
  } else if (FINAL.has(event.type)) {
    const response = event.response, said = response.metadata || {};
    Object.assign(turn, { live: false, status: response.status, items: response.output, arriving: { thinking: "", text: "" },
      error: response.status === "failed" && response.error ? `${said.hg_code || response.error.code || "failed"}: ${response.error.message || ""}` : "" });
  }
  return false;
}

/** Start a turn and read its events. `attachments` are uploaded files ({id, image}): a picture is sent as an image,
 *  which the model is shown, and anything else as a file, which the agent opens. `previous` is the response it
 *  continues from. When that one is still ending (it was just interrupted), the platform says so, and this waits. */
export async function start({ model, text, attachments = [], previous }, onEvent, signal) {
  const given = file => ({ type: file.image ? "input_image" : "input_file", file_id: file.id });
  const input = attachments.length ? [{ role: "user", content: [{ type: "input_text", text }, ...attachments.map(given)] }] : text;
  const body = { model, input, stream: true, background: true, ...(previous && { previous_response_id: previous }) };
  for (let attempt = 0; ; attempt++) {
    try {
      return await stream("POST", "/v1/responses", body, onEvent, signal);
    } catch (error) {
      if (error.status !== 409 || !/in progress/.test(error.message) || attempt >= BUSY_TRIES) throw error;
      await sleep(BUSY_WAIT_MS);
    }
  }
}
/** Read a turn that is running, or over, from its first event. */
export const follow = (id, onEvent, signal) => stream("GET", `/v1/responses/${encodeURIComponent(id)}?stream=true&starting_after=-1`, undefined, onEvent, signal);
export const cancel = id => post(`/v1/responses/${encodeURIComponent(id)}/cancel`);

/** A file for the next message: kept by the Files API, and found by the harness in its sandbox. */
export async function upload(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("purpose", "user_data");
  const stored = await post("/v1/files", form);
  return { id: stored.id, name: stored.filename || file.name };
}
