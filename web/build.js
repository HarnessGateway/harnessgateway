// Build: conversations (a chat with a harness, the workspace beside it), the rodeo, sessions.

VIEWS.Conversations = async function conversations(main, args) {
  const [harnesses, providers, sessions] = await Promise.all([api("GET", "/v1/harnesses"), api("GET", "/v1/providers"), api("GET", "/v1/sessions")]);
  if (!harnesses.data.length) return h("div", {}, head("Conversations", "Talk to a harness the way you would in a coding agent's web view."), empty("No harnesses yet. Fork one from the catalog first."));
  let session = args.session ? sessions.data.find(s => s.id === args.session) : null;
  const list = h("div", { class: "list" });
  const thread = h("div", { class: "thread" });
  const pane = h("div", { class: "pane card" });
  const harnessSel = h("select", {}, ...harnesses.data.map(x => option(x.name, x.name, x.name === (args.harness || session?.harness))));
  const pick = (name) => harnesses.data.find(x => x.name === name) || {};
  const providerSel = h("select", {}, option("", "package default"), ...providers.data.map(p => option(p.name)));
  const modelIn = h("input", { placeholder: "model (package default)", style: "width:170px" });
  // a package without a default provider needs one named: preselect the organization's first
  const defaultProvider = () => { const d = pick(harnessSel.value).defaults || {}; providerSel.value = d.provider || (providers.data[0]?.name || ""); modelIn.placeholder = d.model ? `model (${d.model})` : "model (package default)"; };
  harnessSel.addEventListener("change", defaultProvider); defaultProvider();
  const instructions = h("input", { placeholder: "instructions for this turn (optional)" });
  const effort = h("select", {}, option("", "effort: default"), ...["low", "medium", "high", "xhigh", "max"].map(e => option(e)));
  const box = h("textarea", { placeholder: "What should the harness do?" });
  const send = h("button", { class: "act" }, "Send");
  const steerBtn = h("button", { class: "quiet", hidden: true }, "Steer");
  const cancelBtn = h("button", { class: "danger", hidden: true }, "Cancel");
  const note = h("div");
  let previous = null, running = null, sid = session?.id || "";

  const drawList = async () => {
    const all = (await api("GET", "/v1/sessions")).data;
    list.replaceChildren(h("button", { class: !sid ? "on" : "", on: { click: () => shell("Conversations", { harness: harnessSel.value }) } }, "+ New conversation"),
      ...all.map(s => h("button", { class: s.id === sid ? "on" : "", on: { click: () => shell("Conversations", { session: s.id }) } }, h("span", {}, s.title || "(untitled)"), h("span", { class: "muted" }, `${s.harness} · ${s.responses.length} turn${s.responses.length === 1 ? "" : "s"} · ${ago(s.created_at)}`))));
  };
  const userBubble = (text) => thread.append(h("div", { class: "bubble user" }, text));
  const renderResponse = (r) => { for (const item of r.output || []) thread.append(itemNode(item)); if (r.status !== "completed") thread.append(h("div", { class: "notice warn" }, r.status, r.error ? `: ${r.error.message}` : "", r.metadata?.incomplete_reason ? ` (${r.metadata.incomplete_reason})` : "")); };
  const scroll = () => { thread.lastElementChild?.scrollIntoView({ block: "end" }); };

  // the workspace pane: files after the last turn, the trace, the raw log
  let paneTab = "files";
  const drawPane = async () => {
    if (!sid) return pane.replaceChildren(h("h2", {}, "Workspace"), h("p", { class: "muted" }, "The files the harness leaves after each turn appear here, with the trace of every request and response."));
    const tabs = h("div", { class: "tabs" }, ...["files", "trace", "log"].map(t => h("button", { class: t === paneTab ? "on" : "", on: { click: () => { paneTab = t; drawPane(); } } }, t)));
    pane.replaceChildren(h("div", { class: "row between" }, h("h2", {}, "Workspace"), h("a", { class: "btn ghost small", href: `/v1/sessions/${sid}/archive`, download: `${sid}.tar.gz` }, "download")), tabs);
    if (paneTab === "files") {
      const files = (await api("GET", `/v1/sessions/${sid}/files`)).data;
      const viewer = h("pre", { class: "light", style: "display:none" });
      const tree = h("div", { class: "tree" }, ...files.map(f => h("button", { on: { click: async (e) => { for (const b of tree.querySelectorAll("button")) b.classList.remove("on"); e.currentTarget.classList.add("on"); viewer.style.display = ""; viewer.textContent = await (await api("GET", `/v1/sessions/${sid}/files/${f.path}`, null, true)).text(); } } }, f.path, h("span", {}, bytes(f.size)))));
      pane.append(files.length ? tree : empty("no files yet"), viewer);
    } else if (paneTab === "trace") {
      const trace = (await api("GET", `/v1/sessions/${sid}/trace`)).data;
      pane.append(h("div", { class: "trace" }, ...trace.map(t => h("div", { class: "t" }, h("span", { class: "muted" }, new Date(t.at * 1000).toLocaleTimeString()), tag(t.kind, { request: "blue", response: "ok", interrupt: "orange", cancel: "bad" }[t.kind] || ""),
        h("details", {}, h("summary", { class: "mono", style: "cursor:pointer" }, t.kind === "request" ? (typeof t.body.input === "string" ? t.body.input : JSON.stringify(t.body.input)).slice(0, 80) : t.kind === "response" ? `${t.body.status} · ${(t.body.output || []).length} items` : JSON.stringify(t.body).slice(0, 80)), h("pre", { class: "light" }, JSON.stringify(t.body, null, 1)))))));
    } else {
      const s = await api("GET", `/v1/sessions/${sid}`);
      const last = s.responses[s.responses.length - 1];
      const d = last ? await api("GET", `/v1/responses/${last}/detail`) : null;
      pane.append(d ? h("div", {}, h("p", { class: "muted" }, `last turn: exit ${d.exit_code ?? "?"} · worker ${d.worker || "?"}`), h("h3", {}, "request.sh log"), h("pre", {}, d.log || "(nothing)"), h("h3", {}, "stderr"), h("pre", {}, d.stderr || "(nothing)")) : empty("no turn yet"));
    }
  };

  const openSession = async () => {
    thread.replaceChildren();
    const s = await api("GET", `/v1/sessions/${sid}`);
    session = s;
    for (const r of s.turns) { userBubble(r.text || ""); renderResponse(r); previous = r.id; if (["queued", "in_progress"].includes(r.status)) follow(r); }
    harnessSel.value = s.harness; harnessSel.disabled = true;
    drawPane(); scroll();
  };
  // follow a turn that is already running (opened from the list): re-stream it
  const follow = async (r) => {
    running = r; steerBtn.hidden = cancelBtn.hidden = false; send.disabled = true;
    const live = h("div", { class: "bubble assistant streaming" }); thread.append(live);
    try {
      const res = await api("GET", `/v1/responses/${r.id}?stream=true`, null, true);
      await consume(res, live);
    } catch (e) { note.replaceChildren(fail(e)); }
    finally { running = null; steerBtn.hidden = cancelBtn.hidden = true; send.disabled = false; }
  };
  const consume = async (res, live) => {
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "", text = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        const line = chunk.split("\n").find(l => l.startsWith("data: ")); if (!line) continue;
        const e = JSON.parse(line.slice(6));
        if (e.type === "response.output_text.delta") { text += e.delta; live.textContent = text; scroll(); }
        else if (e.type === "response.output_item.done" && e.item.type !== "message") { live.before(itemNode(e.item)); scroll(); }
        else if (e.type === "response.output_item.done" && e.item.type === "message") { live.before(itemNode(e.item)); text = ""; live.textContent = ""; }
        else if (e.response && ["completed", "failed", "incomplete", "cancelled"].includes(e.response.status)) {
          live.remove(); previous = e.response.id; sid = e.response.metadata.session_id;
          if (e.response.status !== "completed") thread.append(h("div", { class: "notice warn" }, e.response.status, e.response.error ? `: ${e.response.error.message}` : ""));
          drawList(); drawPane(); scroll();
        }
      }
    }
    live.remove();
  };
  const submit = async () => {
    const text = box.value.trim(); if (!text || running) return;
    note.replaceChildren(); box.value = "";
    const model = [harnessSel.value, providerSel.value, modelIn.value.trim()].filter(Boolean);
    const body = { model: model.join("/"), input: text, stream: true };
    if (previous) body.previous_response_id = previous;
    if (instructions.value.trim()) body.instructions = instructions.value.trim();
    if (effort.value) body.reasoning = { effort: effort.value };
    userBubble(text);
    const live = h("div", { class: "bubble assistant streaming" }, h("span", { class: "cursor" })); thread.append(live); scroll();
    steerBtn.hidden = cancelBtn.hidden = false; send.disabled = true; harnessSel.disabled = true; running = { pending: true };
    try {
      const res = await api("POST", "/v1/responses", body, true);
      if (!res.ok) { const t = await res.text(); let d; try { d = JSON.parse(t).detail; } catch { d = t; } throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
      sid = res.headers.get("x-session-id") || sid;
      running = { id: "" };
      await consume(res, live);
    } catch (e) { live.remove(); note.replaceChildren(fail(e)); if (!previous) harnessSel.disabled = false; }
    finally { running = null; steerBtn.hidden = cancelBtn.hidden = true; send.disabled = false; }
  };
  steerBtn.addEventListener("click", async () => { const t = box.value.trim(); if (!t) return note.replaceChildren(h("span", { class: "muted" }, "type the steer in the box, then press Steer")); try { await api("POST", `/v1/sessions/${sid}/interrupt`, { input: t }); box.value = ""; thread.append(h("div", { class: "notice" }, "steer sent: ", t)); } catch (e) { note.replaceChildren(fail(e)); } });
  cancelBtn.addEventListener("click", async () => { try { const s = await api("GET", `/v1/sessions/${sid}`); const rid = s.responses[s.responses.length - 1]; await api("POST", `/v1/responses/${rid}/cancel`); } catch (e) { note.replaceChildren(fail(e)); } });
  box.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); });
  send.addEventListener("click", submit);
  await drawList();
  if (sid) await openSession(); else drawPane();
  return h("div", {}, head("Conversations", "A session is a conversation with one harness in one workspace. Each turn is a Responses request; the files the agent leaves are beside it."),
    h("div", { class: "convo" }, list,
      h("div", { style: "min-width:0" }, h("div", { class: "row", style: "margin-bottom:10px" }, field("Harness", harnessSel), field("Provider", providerSel), field("Model", modelIn), field("Effort", effort)), field("Instructions", instructions),
        h("div", { class: "card", style: "margin-top:12px" }, thread, h("div", { class: "composer" }, box, h("div", { class: "stack" }, send, steerBtn, cancelBtn)), note, h("p", { class: "muted", style: "margin:6px 0 0" }, "⌘/Ctrl+Enter sends. While a turn runs, the box steers it.")))
      , pane));
};

VIEWS.Rodeo = async function rodeo() {
  const [harnesses, providers, rodeos] = await Promise.all([api("GET", "/v1/harnesses"), api("GET", "/v1/providers"), api("GET", "/v1/rodeos")]);
  const task = h("textarea", { placeholder: "One task for every harness in the chute. For example: Create a file named hello.txt containing the word hello, then reply with the one word: done" });
  const picks = h("div", { class: "picks" }, ...harnesses.data.map(x => h("label", {}, h("input", { type: "checkbox", value: x.name }), x.name)));
  const providerSel = h("select", {}, option("", "each package's default provider"), ...providers.data.map(p => option(p.name)));
  const modelIn = h("input", { placeholder: "model (default)", style: "width:160px" });
  const arena = h("div", { class: "rodeo" });
  const out = h("div");
  const run = async () => {
    out.replaceChildren();
    const chosen = [...picks.querySelectorAll("input:checked")].map(i => i.value);
    if (chosen.length < 2) return out.replaceChildren(h("span", { class: "err" }, "pick two to six harnesses"));
    if (!task.value.trim()) return out.replaceChildren(h("span", { class: "err" }, "write the task"));
    arena.replaceChildren();
    const results = await Promise.all(chosen.map(async (name) => {
      const col = h("div", { class: "col" }, h("b", {}, name), h("span", { class: "muted" }, "running…"));
      const live = h("div", { class: "live" }, h("span", { class: "cursor" })); col.append(live); arena.append(col);
      const model = [name, providerSel.value, modelIn.value.trim()].filter(Boolean).join("/");
      try {
        let text = "";
        const final = await streamTurn({ model, input: task.value }, (e) => {
          if (e.type === "response.output_text.delta") { text += e.delta; live.replaceChildren(text, h("span", { class: "cursor" })); }
          else if (e.type === "response.output_item.done" && e.item.type !== "message") live.append("\n[" + e.item.type + (e.item.action?.commands ? " " + e.item.action.commands.join(" ") : e.item.operation?.path ? " " + e.item.operation.path : "") + "]");
        });
        live.replaceChildren(outputText(final) || text || "(no text)");
        col.children[1].replaceChildren(statusTag(final.status), " ", h("span", { class: "muted" }, `${final.usage?.total_tokens ?? "?"} tokens`));
        return { name, final };
      } catch (e) { col.children[1].replaceChildren(fail(e)); live.remove(); return { name, final: null }; }
    }));
    const ok = results.filter(r => r.final);
    if (ok.length < 2) return out.replaceChildren(h("span", { class: "err" }, "fewer than two harnesses answered; no rodeo recorded"));
    const rodeoRec = await api("POST", "/v1/rodeos", { task: task.value, entries: ok.map(r => ({ response: r.final.id })) });
    const pick = h("div", { class: "row" }, h("b", {}, "Pick the winner:"), ...ok.map(r => h("button", { class: "quiet", on: { click: async (e) => { await api("PATCH", `/v1/rodeos/${rodeoRec.id}`, { winner: r.name }); for (const c of arena.children) c.classList.toggle("winner", c.firstChild.textContent === r.name); e.target.classList.add("on"); board.replaceChildren(...leaderboard((await api("GET", "/v1/rodeos")).leaderboard)); } } }, r.name)));
    out.replaceChildren(pick);
  };
  const leaderboard = (rows) => rows.length ? [h("div", { class: "bars" }, ...rows.map(r => h("div", { class: "r" }, h("span", { class: "n" }, r.harness), h("div", { class: "b", style: `width:${Math.max(4, 100 * r.wins / Math.max(1, Math.max(...rows.map(x => x.wins))))}%` }), h("span", { class: "v" }, `${r.wins}/${r.runs}`))))] : [empty("No rodeos yet.")];
  const board = h("div", {}, ...leaderboard(rodeos.leaderboard));
  const past = rodeos.data.slice(0, 20).map(r => h("tr", {}, h("td", {}, r.task.slice(0, 70)), h("td", {}, r.entries.map(e => e.harness).join(", ")), h("td", {}, r.winner ? tag(r.winner, "ok") : tag("undecided")), h("td", { class: "muted" }, ago(r.created))));
  return h("div", {}, head("Rodeo", "One task, several harnesses, out of the chute side by side. Pick the winner; the leaderboard remembers which harness earns your work."),
    h("div", { class: "card" }, h("div", { class: "grid-2" }, h("div", {}, field("The task", task)), h("div", { class: "stack" }, h("div", {}, h("label", {}, "Harnesses (2 to 6)"), picks), h("div", { class: "row" }, field("Provider", providerSel), field("Model", modelIn)), h("div", { class: "row end" }, out, h("button", { class: "act", on: { click: run } }, "Run the rodeo"))))),
    arena,
    h("div", { class: "grid-2" }, h("div", { class: "card" }, h("h2", {}, "Leaderboard"), board), h("div", { class: "card" }, h("h2", {}, "Past rodeos"), past.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Task"), h("th", {}, "Harnesses"), h("th", {}, "Winner"), h("th"))), h("tbody", {}, ...past))) : empty("None yet."))));
};

VIEWS.Sessions = async function sessions() {
  const list = await api("GET", "/v1/sessions");
  const rows = list.data.map(s => h("tr", {}, h("td", {}, h("a", { href: "#", on: { click: (e) => { e.preventDefault(); shell("Conversations", { session: s.id }); } } }, s.title || "(untitled)")), h("td", {}, s.harness, s.team ? " " : "", s.team ? tag(s.team, "orange") : null), h("td", {}, s.member), h("td", {}, statusTag(s.status)), h("td", {}, s.responses.length), h("td", {}, s.stored ? (s.workspace ? tag("workspace", "ok") : tag("empty")) : tag("stateless")), h("td", { class: "muted" }, ago(s.created_at)),
    h("td", {}, h("div", { class: "row end" }, h("code", { class: "mono" }, s.id.slice(0, 13)), copy(s.id), s.status === "idle" ? confirmBtn("delete", "ghost small", async () => { await api("DELETE", `/v1/sessions/${s.id}`); shell("Sessions"); }) : null))));
  return h("div", {}, head("Sessions", "Every conversation, with its turns, trace and workspace. Continue one with previous_response_id from any client, or open it here."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Title"), h("th", {}, "Harness"), h("th", {}, "Member"), h("th", {}, "Status"), h("th", {}, "Turns"), h("th", {}, "Kept"), h("th", {}, "When"), h("th"))), h("tbody", {}, ...rows)) : empty("No sessions yet."))));
};
