// The chat's own conversations, kept in this browser. A chat is a title, the model line last used, its session,
// and for each message what was said and the id of the response it was given: the reply itself is read from the
// API whenever it is shown. Nothing here is the platform's: any application built on the Responses API keeps
// something like it. Each person's chats are their own: in a browser several people use, and for someone who may read
// every session, the chat lists only what that person began here.
import { session } from "../../lib/api.js";
import { keep, kept } from "../../lib/storage.js";

const KEY = "hg.chats", KEPT = 200;
/** Newest first: {id, title, model, session, from, note, exchanges: [{text, attachments, note, response, status,
 *  interrupted, files}]}. `from` is the response a chat with no exchange of its own continues from. */
export const chats = [];
let whose = null;
/** Take up the chats of whoever is signed in, when that is no longer whose these are. */
export function own() {
  if (whose === session.who) return;
  whose = session.who;
  chats.splice(0, chats.length, ...kept(`${KEY}.${whose}`, []));
}
export const save = () => keep(`${KEY}.${whose}`, chats.slice(0, KEPT));
export const find = id => chats.find(chat => chat.id === id) || null;

export function begin(title, model, more = {}) {
  const chat = { id: crypto.randomUUID().slice(0, 13), title, model, session: null, from: null, note: "", exchanges: [], ...more };
  chats.unshift(chat);
  save();
  return chat;
}
export function remove(chat) {
  chats.splice(chats.indexOf(chat), 1);
  save();
}
/** The response the next message continues from: the chat's last reply, or the one it was opened at. */
export const head = chat => [...chat.exchanges].reverse().map(exchange => exchange.response).find(Boolean) || chat.from;
/** Whether the chat's last message is still being answered, as far as this browser has heard. */
export const unfinished = chat => { const last = chat.exchanges[chat.exchanges.length - 1]; return Boolean(last && last.response && !last.status); };

// Rewind and branch are the platform's fork: a message sent with previous_response_id naming an earlier reply
// continues from the state saved after that reply, in a new session, and the earlier branch keeps its own. Any
// client gets the same by naming an earlier response; the two differ only in what this page shows.

/** Take the message at `index` back: the chat ends before it, and the next message continues from the reply before
 *  it (or starts the chat over when there is none). Returns the message's words, to edit and send again. */
export function rewind(chat, index) {
  const [taken] = chat.exchanges.splice(index);
  chat.note = head(chat) ? "Rewound. The next message continues from the reply above, in a new branch; the earlier branch stays under Sessions." : "";
  if (!head(chat)) chat.session = null;
  save();
  return taken.text;
}
/** A new chat that continues from the reply at `index`; the chat it came from stays as it is. */
export const branch = (chat, index) => begin(`${chat.title} (branch)`, chat.model, {
  exchanges: structuredClone(chat.exchanges.slice(0, index + 1)),
  note: "A branch. The next message continues from the reply above, in a new session; the chat it came from is unchanged." });
