// The console's only way to the platform: its public API, with the key the person signed in with. Everything the
// console shows, any other client of the API can read the same way.

const KEY = "hg.key";
export const session = {
  get key() { return sessionStorage.getItem(KEY); },
  set key(value) { value ? sessionStorage.setItem(KEY, value) : sessionStorage.removeItem(KEY); },
};

/** One call. `body` is sent as JSON, or as it is when it is a Blob. A refusal is thrown as an Error whose message
 *  is the platform's own, with the HTTP status on it. `raw` gives the Response instead of its JSON. */
export async function call(method, path, body, { raw = false, signal } = {}) {
  const sent = body === undefined || body instanceof Blob ? body : JSON.stringify(body);
  const response = await fetch(path, { method, body: sent, signal, headers: { Authorization: `Bearer ${session.key}` } });
  if (!response.ok) {
    const refusal = await response.json().catch(() => ({}));
    const error = new Error((refusal.error || {}).message || `${response.status} ${response.statusText}`);
    error.status = response.status;
    if (response.status === 401) window.dispatchEvent(new Event("hg-signed-out"));
    throw error;
  }
  return raw ? response : response.json();
}
export const get = (path, options) => call("GET", path, undefined, options);
export const put = (path, body) => call("PUT", path, body);
export const post = (path, body) => call("POST", path, body);
export const del = path => call("DELETE", path);

/** Every item of a paged list. */
export async function all(path, key = "id") {
  const items = [];
  for (let after = "", more = true; more;) {
    const page = await get(`${path}${path.includes("?") ? "&" : "?"}limit=100${after && `&after=${encodeURIComponent(after)}`}`);
    items.push(...page.data);
    more = page.has_more;
    after = page.data.length ? page.data[page.data.length - 1][key] : "";
  }
  return items;
}

/** A Responses stream: `onEvent` is given each event as it arrives, until the stream ends. */
export async function stream(method, path, body, onEvent, signal) {
  const response = await call(method, path, body, { raw: true, signal });
  const reader = response.body.getReader(), decoder = new TextDecoder();
  for (let held = "";;) {
    const { done, value } = await reader.read();
    if (done) return;
    held += decoder.decode(value, { stream: true });
    const frames = held.split("\n\n");
    held = frames.pop();
    for (const frame of frames) {
      const data = frame.split("\n").find(line => line.startsWith("data: "));
      if (data) onEvent(JSON.parse(data.slice(6)));
    }
  }
}

/** The id a session's work is read under in the Containers API. */
export const containerOf = sessionId => `cntr_${sessionId.slice(5)}`;
