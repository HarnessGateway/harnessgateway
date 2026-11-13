// The console's only way to the platform: the public and management APIs, with the key the user signed in with.

const KEY = "hg.key";

export const session = {
  get key() { return sessionStorage.getItem(KEY); },
  set key(value) { value ? sessionStorage.setItem(KEY, value) : sessionStorage.removeItem(KEY); },
};

async function refusal(response) {
  let message = `${response.status} ${response.statusText}`;
  try {
    message = (await response.json()).error.message || message;
  } catch { /* not JSON: keep the status */ }
  const error = new Error(message);
  error.status = response.status;
  return error;
}

export async function call(method, path, body, options = {}) {
  const key = options.key || session.key;
  const headers = key ? { Authorization: `Bearer ${key}` } : {};       // without a key, an edition's sign-in speaks
  let payload = body;
  if (body instanceof Blob) headers["Content-Type"] = "application/octet-stream";      // a file's bytes, as they are
  else if (body !== undefined && body !== null && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const response = await fetch(path, { method, headers, body: payload, signal: options.signal });
  if (!response.ok) throw await refusal(response);
  return options.raw ? response : response.json();
}

export const get = (path, options) => call("GET", path, undefined, options);
export const post = (path, body, options) => call("POST", path, body, options);
export const patch = (path, body) => call("PATCH", path, body);
export const del = (path) => call("DELETE", path);

export async function download(path, filename) {
  const response = await call("GET", path, undefined, { raw: true });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}

export async function text(path) {
  return (await call("GET", path, undefined, { raw: true })).text();
}

// Gives each server-sent event of a response to `onEvent` as it arrives: a request that is posted, or, with no
// body, one that is read (a turn already running, followed from its first event).
export async function stream(path, body, onEvent, signal) {
  const response = await call(body == null ? "GET" : "POST", path, body == null ? undefined : body, { raw: true, signal });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const data = buffer.slice(0, end).split("\n").filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6)).join("\n");
      buffer = buffer.slice(end + 2);
      if (data) onEvent(JSON.parse(data));
    }
  }
}
