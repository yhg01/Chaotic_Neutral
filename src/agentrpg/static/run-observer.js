"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {connected: false, run: "", view: "timeline", offset: 0, next: null,
    runsNext: null, overview: null, busy: false, generation: 0, openRecords: new Set()};
  const money = value => "$" + Number(value || 0).toFixed(4);
  const text = (tag, content, className) => {
    const node = document.createElement(tag); node.textContent = content;
    if (className) node.className = className; return node;
  };
  const showError = message => { $("error-message").textContent = message; $("error").hidden = !message; };
  async function api(path) {
    let response;
    try {response = await fetch("/api/v1/observer" + path, {
      cache: "no-store", redirect: "error",
      signal: AbortSignal.timeout(15000)
    });} catch {throw new Error("The local API did not respond. Check the API and database, then select Retry.");}
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        const error = new Error("Local access was denied. Open this page on the computer that runs Chaotic Neutral, then select Retry.");
        error.auth = true; throw error;
      }
      throw new Error(response.status === 404 ? "This saved record is unavailable." : "The server could not load this view. Check that the local API and database are running.");
    }
    return response.json();
  }
  function clearRecords() { $("records").replaceChildren(); }
  function empty(message) { $("records").append(text("p", message, "empty")); }
  function pretty(value) { return JSON.stringify(value, null, 2); }
  function renderRecord(record) {
    const item = document.createElement("details"); item.className = "record";
    const key = state.view + ":" + (record.id || record.request_id || record.resource_id || record.owner_id || "");
    item.dataset.key = key; item.open = state.openRecords.has(key);
    const summary = document.createElement("summary");
    const title = record.subject || record.title || (record.payload && record.payload.tool) || record.kind || record.role || record.source || record.resource_id || record.owner_id || "Saved record";
    summary.append(text("span", title.replaceAll("_", " "), "record-title"));
    const parts = [record.owner_id || record.recipient_owner_id || record.author_name || record.seller_id,
      record.window !== undefined ? "Window " + record.window : null, record.status,
      record.acknowledged !== undefined ? (record.acknowledged ? "Read receipt saved" : "No read receipt") : null,
      record.created_at ? new Date(record.created_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : null].filter(Boolean);
    summary.append(text("span", parts.join(" · "), "record-meta")); item.append(summary);
    summary.setAttribute("aria-label", title.replaceAll("_", " ") + " · " + parts.join(" · "));
    const payload = record.payload || record.result || {};
    const preview = record.content || record.body || payload.content || payload.text ||
      (payload.output && payload.output.text) || record.error || payload.reason || "";
    if (preview) item.append(text("p", typeof preview === "string" ? preview : pretty(preview), "record-preview"));
    const details = text("pre", pretty(record)); details.hidden = !item.open; item.append(details);
    item.addEventListener("toggle", () => {details.hidden = !item.open;
      if (item.open) state.openRecords.add(key); else state.openRecords.delete(key);});
    $("records").append(item);
  }
  function setOptions(id, values, first, selected) {
    const select = $(id); select.replaceChildren();
    if (first) {const option = document.createElement("option"); option.value = ""; option.textContent = first; select.append(option);}
    for (const [value, label] of values) {const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option);}
    select.value = selected || "";
  }
  async function loadRuns(append = false) {
    const result = await api("/runs?limit=100&offset=" + (append ? state.runsNext : 0));
    const select = $("run-select"); if (!append) select.replaceChildren();
    for (const run of result.items) {
      const option = document.createElement("option"); option.value = run.id;
      option.textContent = (run.title || run.name || "Scenario") + " · " + run.participants + " pairs · " + run.status + " · " + run.id.slice(0, 8);
      select.append(option);
    }
    state.runsNext = result.next_offset; $("more-runs").hidden = state.runsNext === null;
    if (state.run && [...select.options].some(option => option.value === state.run)) select.value = state.run;
    else state.run = select.value;
    $("empty-runs").hidden = !!state.run; $("run-content").hidden = !state.run;
  }
  function renderOverview(data) {
    state.overview = data;
    const run = data.run, count = data.counts;
    $("run-title").textContent = run.scenario.title || run.scenario.name || "Scenario";
    $("run-id").textContent = run.id;
    $("run-status").textContent = run.status; $("run-status").className = "badge " + run.status;
    $("window").textContent = Math.min(run.current_window, run.max_windows) + " / " + run.max_windows;
    $("phase").textContent = "Phase: " + run.phase + " · " + data.participants.length + " pairs";
    $("event-count").textContent = count.events;
    $("messages-count").textContent = count.owner_messages + " owner messages · " + count.board_messages + " board messages";
    $("resource-count").textContent = count.mail_messages + " / " + count.resources;
    $("trade-count").textContent = count.trades + " committed transfers";
    const sums = data.campaigns.reduce((acc, value) => {
      for (const key of ["reported_usd", "reserved_usd", "uncertain_usd", "remaining_usd", "limit_usd"]) acc[key] += Number(value[key]); return acc;
    }, {reported_usd: 0, reserved_usd: 0, uncertain_usd: 0, remaining_usd: 0, limit_usd: 0});
    $("cost").textContent = data.campaigns.length ? money(sums.reported_usd) : "Not tracked";
    $("remaining").textContent = data.campaigns.length ? money(sums.remaining_usd) + " available across linked campaigns" : "This run has no campaign ledger entries.";
    $("cost-detail").replaceChildren(); $("cost-detail").hidden = !data.campaigns.length;
    for (const [label, key] of [["Campaign cap", "limit_usd"], ["Held for calls", "reserved_usd"], ["Uncertain charges held", "uncertain_usd"]]) {
      const part = text("span", label + ": "); part.append(text("strong", money(sums[key]))); $("cost-detail").append(part);
    }
    $("cost-detail").append(text("span", "Costs cover all runs and roles in each linked campaign."));
    const owner = $("owner-select").value;
    setOptions("owner-select", data.participants.map(p => [p.owner_id, p.owner_id + " · " + p.agent_name]), "All owners and Agents", owner);
    setOptions("kind", data.event_kinds.map(kind => [kind, kind.replaceAll("_", " ")]), "All events", $("kind").value);
    const failing = data.jobs.filter(job => job.error).map(job => job.owner_id + " " + job.kind + ": " + job.error);
    let notice = "";
    if (run.status === "paused") {
      if (run.result?.reason === "operator_pause") notice = "Paused by an operator. The simulation clock stays at this window until you resume the run.";
      else if (run.result?.reason === "window_limit") notice = "Run paused after the requested turns. Use the Run console to continue.";
      else if (failing.length) notice = "Run paused after a worker failure. Inspect the steps and errors view before you resume the run.";
      else notice = "Run paused. Use the Run console to inspect or continue it.";
    }
    if (["invalid", "incomplete"].includes(run.status)) notice = "Run " + run.status + ". Inspect the report and failed steps for saved evidence.";
    if (failing.length) notice += "\n" + failing.join("\n");
    $("run-notice").textContent = notice.trim(); $("run-notice").hidden = !notice.trim();
  }
  async function renderCosts(generation) {
    const campaigns = state.overview.campaigns;
    if (!campaigns.length) {empty("This run has no durable campaign costs. Historical usage may be in its saved report."); return;}
    for (const item of campaigns) {
      const campaign = await api("/campaigns/" + encodeURIComponent(item.id));
      if (generation !== state.generation) return;
      $("records").append(text("p", campaign.name + " · " + campaign.id, "section-note"));
      const grid = document.createElement("div"); grid.className = "cost-grid";
      for (const [label, value] of [["Reported", campaign.reported_usd], ["Outstanding reserve", campaign.reserved_usd], ["Uncertain reserve", campaign.uncertain_usd], ["Available", campaign.remaining_usd]]) {
        const metric = document.createElement("div"); metric.append(text("span", label), text("strong", money(value))); grid.append(metric);
      }
      $("records").append(grid, text("p", "Each HTTP retry has its own reservation. Unknown charges keep their full maximum. Values cover the whole campaign. Latest 50 attempts follow.", "section-note"));
      campaign.recent_attempts.forEach(renderRecord);
    }
  }
  async function loadRecords(generation) {
    $("kind-label").hidden = state.view !== "timeline";
    $("previous").hidden = state.offset === 0; $("next").hidden = true;
    clearRecords(); $("record-count").textContent = "";
    if (["plot", "report"].includes(state.view)) {
      const value = state.view === "plot" ? state.overview.run.scenario : state.overview.report;
      if (value) $("records").append(text("pre", pretty(value), "plain-json"));
      else empty("No evaluation report is saved yet. The runner saves it after execution.");
      return;
    }
    if (state.view === "costs") {await renderCosts(generation); return;}
    const params = new URLSearchParams({limit: "50", offset: String(state.offset)});
    if ($("owner-select").value) params.set("owner_id", $("owner-select").value);
    const timeline = ["timeline", "tools"].includes(state.view);
    const kind = state.view === "tools" ? "assistant_tool_call" : $("kind").value;
    if (timeline && kind) params.set("kind", kind);
    const path = timeline ? "/events" : "/records/" + state.view;
    const result = await api("/runs/" + encodeURIComponent(state.run) + path + "?" + params);
    if (generation !== state.generation) return;
    state.next = result.next_offset; $("next").hidden = state.next === null;
    $("record-count").textContent = result.items.length ? "Records " + (state.offset + 1) + "–" + (state.offset + result.items.length) : "No matching records";
    if (!result.items.length) empty(state.view === "tools" ? "No saved tool results match this view. Older runs can have tool details only in their local trace file." : "No saved records match this view yet.");
    result.items.forEach(renderRecord);
  }
  async function refresh({runs = false} = {}) {
    if (state.busy) return;
    state.busy = true; const generation = state.generation;
    $("loading").hidden = false; $("refresh").disabled = true; $("retry").disabled = true;
    $("workspace").setAttribute("aria-busy", "true"); $("connection").textContent = "Loading saved records…";
    try {
      if (runs || !state.run) await loadRuns();
      if (state.run) {
        const overview = await api("/runs/" + encodeURIComponent(state.run));
        if (generation !== state.generation) return;
        renderOverview(overview); await loadRecords(generation);
      }
      if (generation !== state.generation) return;
      state.connected = true; showError(""); $("connection").textContent = "Connected";
      $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
    } catch (error) {
      if (error.auth) clearPrivateData();
      showError(error.message); $("connection").textContent = "Local service unavailable";
    }
    finally {state.busy = false; $("loading").hidden = true; $("refresh").disabled = false; $("retry").disabled = false;
      $("workspace").setAttribute("aria-busy", "false");
      if (generation !== state.generation && state.connected) queueMicrotask(() => refresh());}
  }
  function changeView() {state.generation++; state.offset = 0; state.next = null; state.openRecords.clear(); if (!state.busy) refresh();}
  function clearPrivateData() {
    state.connected = false; state.run = ""; state.overview = null; state.generation++;
    state.openRecords.clear(); state.offset = 0; state.next = null; state.runsNext = null;
    clearRecords();
    for (const id of ["run-select", "owner-select", "kind", "cost-detail"]) $(id).replaceChildren();
    for (const id of ["run-title", "run-id", "run-status", "window", "phase", "event-count", "messages-count", "resource-count", "trade-count", "cost", "remaining", "run-notice", "record-count", "updated"]) $(id).textContent = "";
    for (const id of ["run-content", "empty-runs", "more-runs", "previous", "next"]) $(id).hidden = true;
  }
  $("retry").addEventListener("click", () => refresh({runs: true}));
  $("run-select").addEventListener("change", () => {state.run = $("run-select").value; changeView();});
  $("owner-select").addEventListener("change", changeView); $("kind").addEventListener("change", changeView);
  $("tabs").addEventListener("click", event => {
    const button = event.target.closest("button[data-view]"); if (!button) return;
    state.view = button.dataset.view;
    for (const item of $("tabs").querySelectorAll("button")) {item.classList.toggle("selected", item === button); if (item === button) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");}
    changeView();
  });
  $("refresh").addEventListener("click", () => refresh({runs: true}));
  $("more-runs").addEventListener("click", async () => {try {await loadRuns(true);} catch(error) {showError(error.message);}});
  $("next").addEventListener("click", () => {if (state.next !== null) {state.offset = state.next; refresh();}});
  $("previous").addEventListener("click", () => {state.offset = Math.max(0, state.offset - 50); refresh();});
  setInterval(() => {if (state.connected && $("live").checked && state.offset === 0 && !document.hidden) refresh();}, 5000);
  refresh({runs: true});
})();
