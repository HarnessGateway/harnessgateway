// A code editor, as small as one can be.
import { h } from "../lib/dom.js";

/** A code editor: line numbers, a Tab key that indents, and nothing else. `onInput(text)` hears every change. */
export function code({ value = "", placeholder = "", onInput }) {
  const gutter = h("div", { class: "gutter" });
  const area = h("textarea", { spellcheck: "false", wrap: "off", placeholder, autocapitalize: "off", autocomplete: "off" });
  area.value = value;
  const number = () => { gutter.textContent = area.value.split("\n").map((_, line) => line + 1).join("\n"); };
  area.addEventListener("input", () => { number(); onInput(area.value); });
  area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
  area.addEventListener("keydown", event => {
    if (event.key !== "Tab" || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    area.setRangeText("    ", area.selectionStart, area.selectionEnd, "end");
    area.dispatchEvent(new Event("input"));
  });
  number();
  return h("div", { class: "code" }, gutter, area);
}
