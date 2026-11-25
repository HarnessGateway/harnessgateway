// Where a message is written: the text, the files given with it, and one button that sends, or stops the turn
// that is running. A message sent while a turn runs interrupts it and steers: the page decides what that means.
import { fail } from "../../components/feedback.js";
import { add, clear, h, icon } from "../../lib/dom.js";
import { bytes } from "../../lib/format.js";
import { upload } from "./turns.js";

const IMAGE = /^image\/(png|jpeg|gif|webp)$/;     // the pictures a model is shown: any other file is given as a file

/** `onSend(text, attachments)` returns false when the message was not taken; `onStop()` stops the running turn. */
export function composer({ onSend, onStop }) {
  const pending = [];                              // files for the next message: {name, bytes, image, id | uploading | error}
  const tray = h("div", { class: "pending" }), picker = h("input", { type: "file", multiple: true, hidden: true });
  const input = h("textarea", { rows: 1 }), button = h("button", { class: "primary square" });
  let busy = false;

  const grow = () => { input.style.height = ""; input.style.height = `${Math.min(input.scrollHeight, 220)}px`; };
  const drawTray = () => add(clear(tray), pending.map(file => h("span", { class: "tile" },
    h("span", {}, h("b", {}, file.name), h("small", { class: file.error ? "bad" : null }, file.uploading ? "Uploading…" : file.error || bytes(file.bytes))),
    h("button", { class: "quiet", title: "Remove", onclick: () => { pending.splice(pending.indexOf(file), 1); drawTray(); } }, icon("close")))));
  function attach(files) {
    for (const file of files) {
      const held = { name: file.name, bytes: file.size, image: IMAGE.test(file.type), uploading: true };
      pending.push(held);
      upload(file).then(stored => { held.id = stored.id; }, error => { held.error = error.message; }).finally(() => { held.uploading = false; drawTray(); });
    }
    drawTray();
  }
  function send() {
    const text = input.value.trim();
    if (!text) return;
    if (pending.some(file => file.uploading)) return fail(new Error("A file is still uploading."));
    const given = pending.filter(file => file.id).map(file => ({ id: file.id, name: file.name, image: file.image }));
    if (onSend(text, given) === false) return;
    input.value = "";
    pending.length = 0;
    grow(); drawTray();
  }
  function setBusy(now) {
    busy = now;
    add(clear(button), icon(busy ? "stop" : "send"));
    button.title = busy ? "Stop" : "Send";
    input.placeholder = busy ? "Send a message to interrupt and steer" : "Message the agent…";
  }
  input.oninput = grow;
  input.onkeydown = event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } };
  input.onpaste = event => { if (event.clipboardData && event.clipboardData.files.length) { event.preventDefault(); attach(event.clipboardData.files); } };
  button.onclick = () => busy ? onStop() : send();
  picker.onchange = () => { attach(picker.files); picker.value = ""; };
  setBusy(false);

  return {
    node: h("div", { class: "composer" }, tray, h("div", { class: "row" },
      h("button", { class: "quiet square", title: "Attach files", onclick: () => picker.click() }, icon("clip")), picker, input, button)),
    attach, setBusy,
    focus: () => input.focus(),
    /** Put words in, to be edited and sent: a message that was taken back. */
    set(text) { input.value = text; grow(); input.focus(); },
  };
}
