"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const colors = ["#91d9c3", "#e6bd82", "#b3a5e8", "#88bfdc", "#d99ca9"];
  const collections = ["chat", "mail", "board", "events"];
  const initialURL = new URL(location.href);
  const requestedRun = initialURL.searchParams.get("run") || "";
  const requestedTurn = Number(initialURL.searchParams.get("turn") || 0);
  const state = {
    connected: false, connecting: false, runs: [], runsNext: null, run: "", overview: null, turns: [], turn: 0,
    owner: "", replay: null, channel: "all", inspect: "persona", playing: false,
    timer: null, generation: 0, controller: null, loading: false, logRows: [],
    mode: "replay", execution: null, boundaries: null, controlsLoading: false, controlsError: "", controlEpoch: 0,
    operationBusy: false, operations: new Map(), pendingOperation: null, lastIntervention: null,
  };
  const node = (tag, content, className) => {
    const element = document.createElement(tag);
    if (content !== undefined && content !== null) element.textContent = content;
    if (className) element.className = className;
    return element;
  };
  const plain = value => typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const preview = (value, length = 145) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > length ? text.slice(0, length - 1) + "…" : text;
  };
  const readable = value => String(value || "").replaceAll("_", " ");
  const padded = value => String(value).padStart(2, "0");
  const owners = () => [...(state.overview?.participants || [])].sort((left, right) => left.owner_id.localeCompare(right.owner_id, undefined, {numeric: true}));
  const ownerName = id => {
    const person = state.overview?.run?.scenario?.owners?.find(owner => owner.id === id);
    const perspective = state.replay?.perspectives?.find(owner => owner.owner_id === id);
    return perspective?.view?.owner_name || person?.name || id || "Director";
  };
  const color = id => colors[Math.max(0, owners().findIndex(owner => owner.owner_id === id)) % colors.length];
  const agentName = id => owners().find(owner => owner.owner_id === id)?.agent_name || "Assistant for " + ownerName(id);
  const activeSummary = () => state.turns.find(turn => turn.window === state.turn);
  const isCurrent = generation => generation === state.generation;

  async function controlAPI(path, body) {
    let response;
    try {
      response = await fetch('/api/v1/console' + path, {
        method: body ? 'POST' : 'GET', headers: body ? {'Content-Type': 'application/json'} : {},
        ...(body ? {body: JSON.stringify(body)} : {}), cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(body ? 45000 : 15000),
      });
    } catch {
      const exception = new Error(body ? 'The result is not yet known. Retry this same request to check its saved result.' : 'Live controls could not load. Check the local service, then refresh controls.');
      exception.uncertain = !!body; throw exception;
    }
    let result;
    try { result = await response.json(); } catch { result = {}; }
    if (!response.ok) {
      const detail = typeof result.detail === 'string' ? result.detail : result.detail?.message || result.error?.message;
      const exception = new Error(detail || (response.status === 404 ? 'Live controls are unavailable for this run.' : 'This operation was rejected. Refresh controls and check the run state.'));
      exception.auth = [401, 403].includes(response.status); exception.uncertain = response.status >= 500;
      throw exception;
    }
    return result;
  }
  const executionBusy = () => ['queued', 'running'].includes(state.execution?.execution?.status);
  function receipt(message, problem = false, retry = false) {
    $('operation-feedback').hidden = !message;
    $('operation-feedback').classList.toggle('has-error', problem);
    $('operation-receipt').textContent = message;
    $('retry-operation').hidden = !retry;
  }
  function setMode(mode) {
    state.mode = mode;
    $('workspace').classList.toggle('is-live', mode === 'live');
    $('live-controls').hidden = mode !== 'live';
    $('view-mode-label').textContent = mode === 'live' ? 'LIVE / REPLAY' : 'REPLAY';
    for (const value of ['replay', 'live']) {
      $('mode-' + value).classList.toggle('is-active', value === mode);
      $('mode-' + value).setAttribute('aria-pressed', String(value === mode));
    }
    if (mode === 'live') { pause(); refreshControls(); }
  }
  function renderControls() {
    const status = state.execution;
    const capability = status?.capabilities || {};
    if (status) { $('run-status').textContent = readable(status.status); $('run-status').className = 'badge ' + status.status; }
    if (!state.loading && state.mode === 'live') $('connection-status').textContent = executionBusy() ? 'Local · live run active' : 'Local · live controls';
    const busy = state.operationBusy;
    const prompt = $('operator-prompt').value.trim();
    const next = status?.next_intervention_window;
    $('prompt-owner').textContent = state.owner ? ownerName(state.owner) : 'selected person';
    $('live-step').disabled = busy || !capability.can_execute;
    $('live-continue').disabled = busy || !capability.can_execute;
    $('live-pause').disabled = busy || !capability.can_pause;
    $('send-prompt').disabled = busy || !state.owner || !prompt || !capability.can_intervene || !Number.isInteger(next);
    $('send-prompt').textContent = Number.isInteger(next) ? 'Queue for Turn ' + padded(next + 1) : 'Queue prompt';
    $('execution-status').textContent = busy ? 'Sending request' : readable(status?.execution?.status || status?.status || (state.controlsLoading ? 'Loading' : 'Unavailable'));
    $('execution-status').className = 'badge ' + (status?.execution?.status || status?.status || '');
    $('execution-caption').textContent = status
      ? 'Live cursor: Turn ' + padded(status.current_window + 1) + ' · ' + readable(status.phase) + '. Viewing: Turn ' + padded(state.turn + 1) + '.'
      : (state.controlsError || (state.run ? 'Loading live run state…' : 'Select a run to use live controls.'));
    $('execution-reason').textContent = status?.execution?.error || capability.reason || (executionBusy() ? 'The live run continues while you inspect saved turns. Pause stops at a safe boundary.' : 'Step runs one full turn. Continue runs until completion or a pause.');
    const budget = status?.budget;
    $('execution-budget').textContent = budget ? '$' + Number(budget.available_usd).toFixed(2) + ' available · $' + Number(budget.spent_usd).toFixed(2) + ' spent · shared $' + Number(budget.limit_usd).toFixed(2) + ' limit' : '';
    const boundary = state.boundaries?.items?.find(item => item.window === state.turn);
    const initial = state.boundaries?.items?.find(item => item.window === 0);
    $('branch-turn').textContent = 'Branch before Turn ' + padded(state.turn + 1);
    $('branch-turn').disabled = busy || !boundary?.available;
    $('restart-scenario').hidden = !state.run || !!boundary?.available || !initial?.available || state.turn === 0;
    $('restart-scenario').disabled = busy;
    $('branch-reason').textContent = boundary?.available
      ? (boundary.source === 'initial' ? 'This starts from the frozen initial scenario. The new run is paused.' : 'This restores the saved state before this turn. The new run is paused.')
      : boundary?.reason || (state.boundaries ? 'No complete state was saved before this turn.' : state.controlsError || 'Checking saved turn boundaries…');
    const parent = state.boundaries?.parent_run_id;
    $('parent-run-link').hidden = !parent;
    if (parent) {
      $('parent-run-link').href = '/console?run=' + encodeURIComponent(parent) + '&turn=' + state.boundaries.parent_window;
      $('parent-run-link').textContent = 'Branch of ' + parent.slice(0, 8) + ' · before Turn ' + padded(state.boundaries.parent_window + 1) + ' ↗';
    } else { $('parent-run-link').textContent = ''; $('parent-run-link').removeAttribute('href'); }
    $('prompt-help').textContent = $('prompt-target').value === 'actor'
      ? 'The Actor receives this Director cue at the indicated turn. A branch applies it before the selected turn.'
      : 'The Agent receives this as a message from its owner at the indicated turn. A branch applies it before the selected turn.';
    $('run-select').disabled = busy;
    $('operator-prompt').disabled = busy;
    $('prompt-target').disabled = busy;
    $('retry-operation').disabled = busy;
    $('refresh-controls').disabled = busy || state.controlsLoading;
  }
  async function refreshControls() {
    if (!state.run || !state.connected || state.controlsLoading || state.operationBusy) return;
    const id = state.run; const epoch = state.controlEpoch;
    state.controlsLoading = true; renderControls();
    try {
      const base = '/runs/' + encodeURIComponent(id);
      const [execution, boundaries] = await Promise.all([controlAPI(base + '/execution'), controlAPI(base + '/boundaries')]);
      if (id !== state.run || epoch !== state.controlEpoch) return;
      state.execution = execution; state.boundaries = boundaries; state.controlsError = '';
      const applied = execution.interventions?.find(item => item.id === state.lastIntervention && item.status === 'applied');
      if (applied && !state.pendingOperation) {
        receipt('Prompt applied for ' + ownerName(applied.owner_id) + ' · Turn ' + padded(applied.effective_window + 1) + '. Inspect that turn’s logs for the response.');
        state.lastIntervention = null;
      }
      if (state.mode === 'live' && !state.pendingOperation && !$('operation-receipt').textContent && execution.interventions?.some(item => item.status === 'pending')) {
        const pending = execution.interventions.filter(item => item.status === 'pending');
        receipt(pending.length + ' prompt' + (pending.length === 1 ? '' : 's') + ' queued. Next: Turn ' + padded(Math.min(...pending.map(item => item.effective_window)) + 1) + '.');
      }
    } catch (exception) {
      if (id !== state.run || epoch !== state.controlEpoch) return;
      if (exception.auth) { disconnect('Local access was denied. Open this page on the computer that runs agentRPG, then select Retry.'); return; }
      state.execution = null; state.boundaries = null; state.controlsError = exception.message;
      if (state.mode === 'live' && !state.pendingOperation) receipt(exception.message, true);
    } finally {
      if (id === state.run && epoch === state.controlEpoch) { state.controlsLoading = false; renderControls(); }
    }
  }
  async function operation(path, payload, kind, retry = null) {
    if (state.operationBusy || !state.run) return;
    const id = state.run;
    const signature = id + path + JSON.stringify(payload);
    const request = retry || state.operations.get(signature) || {path, body: {...payload, request_id: crypto.randomUUID()}, kind, signature, run: id};
    state.controlEpoch += 1; state.controlsLoading = false;
    state.operations.set(signature, request); state.pendingOperation = request; state.lastIntervention = null;
    state.operationBusy = true; receipt('Sending request…'); renderControls();
    try {
      const result = await controlAPI(request.path, request.body);
      state.operations.delete(request.signature);
      if (state.run !== id) return;
      state.pendingOperation = null;
      if (kind === 'branch') {
        state.operationBusy = false;
        $('operator-prompt').value = '';
        await loadRuns();
        await selectRun(result.run_id, result.window);
        setMode('live');
        receipt('Created a paused branch before Turn ' + padded(result.window + 1) + '. Select Step one turn or Continue to run it.');
      } else if (kind === 'prompt') {
        state.lastIntervention = result.id;
        $('operator-prompt').value = '';
        receipt('Prompt queued for ' + ownerName(result.owner_id) + ' · ' + (result.target === 'actor' ? 'Actor cue' : 'Agent owner message') + ' · Turn ' + padded(result.effective_window + 1) + '. It will appear in the logs when applied.');
      } else {
        state.execution = result;
        receipt(kind === 'pause' ? 'Pause requested. Active work stops at the next safe boundary.' : (payload.mode === 'step' ? 'One full turn requested.' : 'Live execution requested. Use Pause run to stop at a safe boundary.'));
      }
    } catch (exception) {
      if (state.run !== id) return;
      if (exception.auth) { disconnect('Local access was denied. Open this page on the computer that runs agentRPG, then select Retry.'); return; }
      if (!exception.uncertain) { state.operations.delete(request.signature); state.pendingOperation = null; }
      receipt(exception.message, true, !!exception.uncertain);
    } finally {
      state.operationBusy = false; renderControls();
      if (state.run === id) refreshControls();
    }
  }
  function requestExecution(mode) {
    if (state.turn >= (state.execution?.current_window ?? Infinity)) $('live-follow').checked = true;
    operation('/runs/' + encodeURIComponent(state.run) + '/execution', {mode}, 'execute');
  }
  function branch(window) {
    const prompt = $('operator-prompt').value.trim();
    const payload = {window, target: $('prompt-target').value};
    if (prompt) Object.assign(payload, {owner_id: state.owner, prompt});
    operation('/runs/' + encodeURIComponent(state.run) + '/branches', payload, 'branch');
  }

  function error(message) {
    $("error-message").textContent = message;
    $("error-banner").hidden = !message;
  }
  function loading(value) {
    state.loading = value;
    $("run-content").setAttribute("aria-busy", String(value));
    $("connection-status").textContent = value ? "Loading saved turn…" : "Local · saved replay";
  }
  function navigation() {
    state.controller?.abort();
    state.controller = new AbortController();
    state.generation += 1;
    error("");
    return state.generation;
  }
  async function api(path, signal = state.controller?.signal) {
    let response;
    try {
      response = await fetch("/api/v1/observer" + path, {
        cache: "no-store", redirect: "error",
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      });
    } catch (exception) {
      if (signal?.aborted) throw exception;
      throw new Error("The local service did not respond. Check the server, then select Retry.");
    }
    if (!response.ok) {
      const exception = new Error(response.status === 401 || response.status === 403
        ? "Local access was denied. Open this page on the computer that runs agentRPG, then select Retry."
        : response.status === 404 ? "This run or turn is not available. Refresh the saved runs."
          : "This view could not load. Select Retry to try again.");
      exception.auth = response.status === 401 || response.status === 403;
      throw exception;
    }
    return response.json();
  }
  function fail(exception, generation) {
    if (!isCurrent(generation) || exception.name === "AbortError") return;
    pause(); loading(false);
    if (exception.auth) {
      disconnect(exception.message);
    } else error(exception.message);
  }
  function setURL() {
    const url = new URL(location.href);
    url.search = ""; url.hash = "";
    if (state.run) {
      url.searchParams.set("run", state.run);
      url.searchParams.set("turn", String(state.turn));
    }
    history.replaceState(null, "", url);
  }
  function renderRunOptions() {
    $("run-select").replaceChildren();
    for (const run of state.runs) {
      const option = node("option", (run.title || run.name || "Untitled run") + " · " + run.id.slice(0, 8));
      option.value = run.id;
      $("run-select").append(option);
    }
    if (state.run && !state.runs.some(run => run.id === state.run)) {
      const option = node("option", "Selected run · " + state.run.slice(0, 8));
      option.value = state.run; $("run-select").append(option);
    }
    $("run-select").value = state.run;
    $("more-runs").hidden = state.runsNext === null;
    $("empty-runs").hidden = state.runs.length > 0 || !!state.run;
    $("run-content").hidden = !state.run;
  }
  async function loadRuns(append = false) {
    const generation = state.generation;
    const result = await api("/runs?limit=100&offset=" + (append ? state.runsNext || 0 : 0));
    if (!isCurrent(generation)) return;
    state.runs = append ? [...state.runs, ...result.items.filter(run => !state.runs.some(old => old.id === run.id))] : result.items;
    state.runsNext = result.next_offset;
    renderRunOptions();
  }
  async function selectRun(id, preferredTurn = 0) {
    pause();
    const generation = navigation();
    state.controlEpoch += 1; state.controlsLoading = false; state.execution = null; state.boundaries = null;
    state.controlsError = ""; state.pendingOperation = null; state.lastIntervention = null; receipt(""); $("operator-prompt").value = "";
    state.run = id; state.replay = null; state.overview = null; state.turns = []; state.owner = "";
    $("dialogue").replaceChildren(node("p", "Loading this run…", "empty-state"));
    $("inspector-body").replaceChildren(); $("log-list").replaceChildren(); $("owner-list").replaceChildren();
    $("turn-markers").replaceChildren();
    $("run-name").textContent = id ? "Loading run…" : "Select a run";
    $("run-status").textContent = id ? "Loading" : "No run selected";
    $("run-status").className = "badge";
    $("run-summary").textContent = id ? "Run " + id.slice(0, 8) : "Choose a saved run to inspect its turns.";
    $("inspector-title").textContent = "Select a person";
    $("cast-count").textContent = "0"; $("log-count").textContent = "0 records";
    state.turn = 0; renderTransport();
    if ($("detail-dialog").open) $("detail-dialog").close();
    $("run-content").hidden = !id;
    renderRunOptions();
    if (!id) { loading(false); return; }
    loading(true);
    try {
      const base = "/runs/" + encodeURIComponent(id);
      const [overview, turns] = await Promise.all([api(base), api(base + "/turns")]);
      if (!isCurrent(generation)) return;
      state.overview = overview; state.turns = turns.items;
      state.owner = owners()[0]?.owner_id || "";
      $("run-name").textContent = overview.run.scenario.title || overview.run.scenario.name || "Untitled run";
      $("run-status").textContent = readable(overview.run.status);
      $("run-status").className = "badge " + overview.run.status;
      $("run-summary").textContent = owners().length + " owner–agent pairs · " + state.turns.length + " recorded turns · " + id.slice(0, 8);
      $("cast-count").textContent = padded(owners().length);
      $("open-observer").href = "/runs";
      renderCast(); renderControls(); refreshControls();
      const target = state.turns.some(turn => turn.window === preferredTurn) ? preferredTurn : state.turns[0]?.window;
      if (target === undefined) {
        loading(false);
        $("dialogue").replaceChildren(node("p", "This run has no recorded turns yet. Refresh after the worker saves a turn.", "empty-state"));
        renderTransport(); return;
      }
      await selectTurn(target);
    } catch (exception) { fail(exception, generation); }
  }
  async function pages(window, generation, previous = null) {
    let offset = previous?.pagination?.next_offset || 0;
    let result = previous;
    for (let index = 0; index < 20; index += 1) {
      const page = await api("/runs/" + encodeURIComponent(state.run) + "/turns/" + window + "?limit=100&offset=" + offset);
      if (!isCurrent(generation)) return null;
      if (!result) result = page;
      else {
        for (const key of collections) {
          const ids = new Set(result[key].map(record => record.id));
          result[key].push(...page[key].filter(record => !ids.has(record.id)));
        }
        result.pagination = page.pagination;
      }
      const next = page.pagination.next_offset;
      if (next === null) return result;
      if (!Number.isInteger(next) || next <= offset) throw new Error("The saved page order is invalid. Refresh this turn.");
      offset = next;
    }
    return result;
  }
  async function selectTurn(window, keepPlaying = false) {
    if (!state.turns.some(turn => turn.window === window)) return;
    if (!keepPlaying) pause();
    const generation = navigation();
    state.turn = window; state.replay = null;
    loading(true); setURL(); renderTransport();
    $("dialogue").replaceChildren(node("p", "Loading turn " + (window + 1) + "…", "empty-state"));
    $("log-list").replaceChildren(); $("inspector-body").replaceChildren();
    if ($("detail-dialog").open) $("detail-dialog").close();
    try {
      const result = await pages(window, generation);
      if (!isCurrent(generation) || !result) return;
      state.replay = result;
      renderReplay(); loading(false);
    } catch (exception) { fail(exception, generation); }
  }
  function renderCast() {
    $("owner-list").replaceChildren();
    for (const owner of owners()) {
      const id = owner.owner_id;
      const button = node("button", null, "cast-button" + (id === state.owner ? " is-selected" : ""));
      button.type = "button"; button.dataset.owner = id;
      button.style.setProperty("--cast-color", color(id));
      button.setAttribute("aria-pressed", String(id === state.owner));
      const avatar = node("span", ownerName(id).slice(0, 1).toUpperCase(), "cast-avatar");
      avatar.setAttribute("aria-hidden", "true");
      const copy = node("span", null, "cast-copy");
      copy.append(node("span", ownerName(id), "cast-name"), node("span", id, "cast-meta"));
      button.append(avatar, copy, node("span", agentName(id), "cast-agent"));
      button.addEventListener("click", () => {
        state.owner = id; renderCast(); renderDialogue(); renderInspector(); renderLogs(); renderControls();
      });
      $("owner-list").append(button);
    }
  }
  function renderTransport() {
    const index = state.turns.findIndex(turn => turn.window === state.turn);
    $("turn-now").textContent = index < 0 ? "00" : padded(state.turn + 1);
    $("turn-total").textContent = padded(state.turns.length);
    $("turn-heading").textContent = "Turn " + padded(state.turn + 1);
    $("turn-caption").textContent = "Saved dialogue · window " + state.turn;
    $("turn-slider").min = "0"; $("turn-slider").max = String(Math.max(0, state.turns.length - 1));
    $("turn-slider").value = String(Math.max(0, index));
    $("turn-slider").setAttribute("aria-valuetext", "Turn " + (state.turn + 1) + " of " + state.turns.length);
    $("turn-slider").disabled = !state.turns.length;
    $("previous-turn").disabled = index <= 0;
    $("next-turn").disabled = index < 0 || index >= state.turns.length - 1;
    $("play").disabled = state.turns.length < 2;
    $("refresh-turn").disabled = !state.overview;
    $("copy-link").disabled = !state.overview || !state.turns.length;
    $("turn-markers").replaceChildren();
    for (const turn of state.turns) {
      const marker = node("button", padded(turn.window + 1), "turn-marker" + (turn.window === state.turn ? " is-active" : ""));
      marker.type = "button"; marker.dataset.turn = turn.window;
      marker.title = "Turn " + (turn.window + 1) + " · " + (turn.counts?.events || 0) + " events";
      marker.setAttribute("aria-label", marker.title);
      if (turn.window === state.turn) marker.setAttribute("aria-current", "step");
      if (turn.jobs?.failed) marker.classList.add("has-error");
      marker.addEventListener("click", () => { $("live-follow").checked = false; selectTurn(turn.window); });
      $("turn-markers").append(marker);
    }
    renderControls();
    $("play-label").textContent = state.playing ? "Pause" : "Play";
    $("play").setAttribute("aria-pressed", String(state.playing));
    $("play").querySelector("path").setAttribute("d", state.playing ? "M6 4h3v12H6zM12 4h3v12h-3z" : "m7 4 9 6-9 6z");
  }
  function renderDialogue() {
    $("dialogue").replaceChildren();
    if (!state.replay) return;
    const visible = $("focus-only").checked ? owners().filter(owner => owner.owner_id === state.owner) : owners();
    $("dialogue").classList.toggle("is-focused", visible.length === 1);
    for (const owner of visible) {
      const id = owner.owner_id;
      const messages = state.replay.chat.filter(message => message.owner_id === id);
      const card = node("section", null, "dialogue-card" + (id === state.owner ? " is-selected" : ""));
      card.style.setProperty("--cast-color", color(id));
      card.setAttribute("aria-label", ownerName(id) + " and assistant, turn " + (state.turn + 1));
      const header = node("div", null, "dialogue-card-head");
      const name = node("div");
      name.append(node("h3", ownerName(id)), node("span", id + " / " + agentName(id), "mini-label"));
      header.append(node("span", ownerName(id).slice(0, 1), "cast-avatar"), name, node("span", "PRIVATE", "badge"));
      const scroll = node("div", null, "dialogue-scroll");
      scroll.tabIndex = 0; scroll.setAttribute("aria-label", ownerName(id) + " conversation");
      if (!messages.length) scroll.append(node("p", "No dialogue was saved for this pair in this turn.", "empty-note"));
      for (const message of messages) {
        const isAgent = message.source === "agent";
        const speech = node("article", null, "speech " + (isAgent ? "speech-agent" : "speech-owner"));
        speech.dataset.source = message.source;
        const meta = node("div", null, "speech-meta");
        meta.append(node("span", isAgent ? "◇  " + agentName(id) : ownerName(id)));
        const inspect = node("button", "↗");
        inspect.type = "button"; inspect.title = "Inspect saved message";
        inspect.setAttribute("aria-label", "Inspect " + (isAgent ? "agent" : "owner") + " message from " + ownerName(id));
        inspect.addEventListener("click", () => detail("Delivered owner chat", message, ownerName(id)));
        meta.append(inspect);
        speech.append(meta, node("p", message.content, "speech-text")); scroll.append(speech);
      }
      if (messages.some(message => message.source === "owner") && !messages.some(message => message.source === "agent")) {
        const incomplete = state.replay.pagination.next_offset !== null;
        scroll.append(node("p", incomplete ? "More records remain to load." : "No agent reply was saved in this turn.", "empty-note"));
      }
      const jumps = node("div", null, "dialogue-jumps");
      for (const [source, label] of [["owner", "Owner"], ["agent", "Agent"]]) {
        const count = messages.filter(message => message.source === source).length;
        const jump = node("button", label + " · " + (count || "No reply"));
        jump.type = "button"; jump.dataset.speaker = source; jump.disabled = !count;
        jump.setAttribute("aria-label", "Jump to " + ownerName(id) + (source === "agent" ? "'s agent reply" : "'s message"));
        jump.addEventListener("click", () => {
          const target = scroll.querySelector('[data-source="' + source + '"]');
          if (target) scroll.scrollTop += target.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 12;
        });
        jumps.append(jump);
      }
      card.append(header, jumps, scroll); $("dialogue").append(card);
    }
  }
  function section(title, content, open = false) {
    const box = node("details", null, "inspector-section");
    box.open = open; box.append(node("summary", title));
    if (Array.isArray(content)) {
      const list = node("ul", null, "detail-list");
      content.forEach(item => list.append(node("li", typeof item === "string" ? item : item.text || plain(item))));
      box.append(list);
    } else box.append(node("p", content || "No record saved.", "fact"));
    return box;
  }
  function renderInspector() {
    const body = $("inspector-body"); body.replaceChildren();
    $("inspector-title").textContent = ownerName(state.owner);
    $("inspector-title").style.setProperty("--cast-color", color(state.owner));
    for (const button of $("inspector-tabs").querySelectorAll("button")) {
      const selected = button.dataset.inspect === state.inspect;
      button.classList.toggle("is-active", selected); button.setAttribute("aria-pressed", String(selected));
    }
    if (!state.replay) return;
    const perspective = state.replay.perspectives.find(item => item.owner_id === state.owner);
    const view = perspective?.view;
    const job = state.replay.jobs.find(item => item.owner_id === state.owner && item.kind === "agent");
    if (state.inspect === "persona") {
      if (!view) { body.append(node("p", "No Actor input was saved for this turn. Select another turn to inspect its perspective.", "empty-state")); return; }
      body.append(node("p", "ACTOR VIEW · START OF TURN " + padded(state.turn + 1), "mini-label"));
      body.append(section("Director cue", view.cue, true));
      if (view.goals?.length) body.append(section("Goals · " + view.goals.length, view.goals, true));
      if (view.beliefs?.length) body.append(section("Declared beliefs · " + view.beliefs.length, view.beliefs));
      if (view.known_facts?.length) body.append(section("Known facts · " + view.known_facts.length, view.known_facts));
      body.append(section("Initial request", view.request));
      if (view.acceptance_rules?.length) body.append(section("Decision boundaries", view.acceptance_rules));
      if (view.observations?.length) body.append(section("Observations · " + view.observations.length, view.observations));
    } else if (state.inspect === "agent") {
      if (!job) { body.append(node("p", "No agent activation was saved for this turn.", "empty-state")); return; }
      const tools = state.replay.events.filter(event => event.kind === "assistant_tool_call" && event.owner_id === state.owner);
      body.append(node("p", agentName(state.owner) + " · TURN " + padded(state.turn + 1), "mini-label"));
      const stats = node("div", null, "agent-stats");
      for (const [label, value] of [["Job", readable(job.status)], ["Result", job.output?.status || "Not saved"], ["Tool calls", String(tools.length)], ["Attempts", String(job.attempts || 0)]]) {
        const item = node("div"); item.append(node("span", label, "muted"), node("strong", value)); stats.append(item);
      }
      body.append(stats);
      if (job.error) body.append(node("p", job.error, "empty-note has-error"));
      body.append(section("Saved activation result", job.output?.text || "No final reply text was saved.", true));
      body.append(node("p", "The conversation panels show delivered messages. This panel shows the saved activation result.", "muted"));
      if (tools.length) {
        const box = node("div", null, "inspector-section"); box.append(node("h3", "Tool calls"));
        for (const event of tools) {
          const button = node("button", readable(event.payload?.tool), "tool-button");
          button.addEventListener("click", () => detail("Agent tool · " + readable(event.payload?.tool), event, ownerName(state.owner)));
          box.append(button);
        }
        body.append(box);
      }
      const button = node("button", "Inspect activation input", "tool-button");
      button.addEventListener("click", () => detail("Saved agent input", job.input, ownerName(state.owner)));
      body.append(button);
    } else {
      const resources = perspective?.resources?.snapshot;
      body.append(node("p", "START OF ACTOR ACTIVATION", "mini-label"));
      body.append(node("p", "These records were visible to this Actor when the turn began. Changes made during the turn appear in its logs.", "muted"));
      if (!resources) { body.append(node("p", "No resource view was saved for this turn.", "empty-state")); return; }
      if (resources.truncated) body.append(node("p", "The saved Actor resource view was truncated.", "empty-note"));
      for (const resource of resources.items || []) {
        const card = node("details", null, "resource-card");
        card.append(node("summary", resource.title || resource.id));
        card.append(node("p", readable(resource.visibility) + " · version " + resource.version + (resource.agent_writable ? " · Agent can edit" : " · Read only"), "resource-meta"));
        card.append(node("p", resource.content || "No text content.", "fact"));
        if (resource.content_truncated) card.append(node("p", "This content was truncated in the saved view.", "empty-note"));
        body.append(card);
      }
      if (!resources.items?.length) body.append(node("p", "No resources were visible in this saved view.", "empty-state"));
    }
  }
  function detail(title, record, meta = "") {
    $("detail-title").textContent = title;
    $("detail-meta").textContent = "Turn " + (state.turn + 1) + (meta ? " · " + meta : "") + " · Saved evidence";
    $("detail-content").textContent = plain(record);
    $("detail-dialog").showModal();
  }
  function buildLogs() {
    const replay = state.replay;
    const records = [];
    const add = (category, title, description, record, related, problem = false) => {
      records.push({category, title, description: preview(description), record, related: related.filter(Boolean), problem,
        time: record.created_at || "", haystack: (title + " " + plain(record)).toLowerCase()});
    };
    for (const message of replay.chat) add("chat", message.source === "agent" ? "Agent reply" : "Owner message", message.content, message, [message.owner_id]);
    for (const message of replay.mail) add("mail", message.subject || "Private mail", message.body, message, [message.sender_owner_id, message.recipient_owner_id]);
    for (const message of replay.board) add("board", message.title || "Board reply", message.body, message, [message.owner_id]);
    for (const event of replay.events) {
      const payload = event.payload || {};
      const isTool = event.kind === "assistant_tool_call";
      const problem = /fail|invalid|reject|error/.test(event.kind) || payload.result?.ok === false;
      add(isTool ? "tools" : "director", isTool ? readable(payload.tool) : readable(event.kind),
        isTool ? (payload.result?.ok === false ? plain(payload.result.error) : preview(plain(payload.arguments || payload.args || {})))
          : payload.cue || payload.reason || payload.error || payload.output?.text || payload.text || "Saved environment event",
        event, [event.owner_id], problem);
    }
    for (const job of replay.jobs.filter(item => item.error)) add("errors", "Job error · " + job.kind, job.error, job, [job.owner_id], true);
    records.sort((a, b) => a.time.localeCompare(b.time) || String(a.record.id).localeCompare(String(b.record.id)));
    state.logRows = records;
  }
  function renderLogs() {
    const list = $("log-list"); list.replaceChildren();
    if (!state.replay) { $("log-count").textContent = "0 records"; return; }
    const search = $("log-search").value.trim().toLowerCase();
    const records = state.logRows.filter(item =>
      (state.channel === "all" || item.category === state.channel || (state.channel === "errors" && item.problem)) &&
      (!$("focus-only").checked || item.related.includes(state.owner)) && (!search || item.haystack.includes(search)));
    $("log-count").textContent = records.length + " records";
    for (const record of records) {
      const row = node("button", null, "log-row" + (record.problem ? " has-error" : ""));
      row.type = "button";
      const time = record.time ? new Date(record.time).toLocaleTimeString([], {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—";
      row.append(node("span", time, "log-time"), node("span", record.category, "log-kind"));
      const description = node("span", null, "log-description");
      description.append(node("strong", record.title), node("span", record.description));
      row.append(description, node("span", record.related.map(ownerName).join(" → ") || "Director", "log-owner"));
      row.addEventListener("click", () => detail(record.title, record.record, record.related.map(ownerName).join(" → ")));
      list.append(row);
    }
    if (!records.length) list.append(node("p", "No records match this view in the selected turn.", "empty-state"));
    if (state.replay.pagination.next_offset !== null) {
      const more = node("button", "Load more records from this turn", "tool-button");
      more.addEventListener("click", async () => {
        const generation = navigation(); more.disabled = true; loading(true);
        try {
          const result = await pages(state.turn, generation, state.replay);
          if (result && isCurrent(generation)) { state.replay = result; renderReplay(); loading(false); }
        } catch (exception) { fail(exception, generation); }
      });
      list.append(more);
    }
  }
  function renderReplay() {
    renderCast(); renderTransport(); renderDialogue(); renderInspector(); buildLogs(); renderLogs();
    const summary = activeSummary();
    $("turn-caption").textContent = (summary?.counts?.owner_chat || 0) + " owner messages · " +
      (summary?.counts?.agent_chat || 0) + " agent replies · " + (summary?.counts?.events || 0) + " events";
    if (state.replay.pagination.next_offset !== null) $("turn-caption").textContent += " · More records available below";
  }
  function pause() {
    state.playing = false; clearTimeout(state.timer); state.timer = null;
    $("play-label").textContent = "Play"; $("play").setAttribute("aria-pressed", "false");
    $("play").querySelector("path").setAttribute("d", "m7 4 9 6-9 6z");
  }
  function schedule() {
    clearTimeout(state.timer);
    if (!state.playing) return;
    state.timer = setTimeout(async () => {
      if (!state.playing || document.hidden) { pause(); return; }
      const index = state.turns.findIndex(turn => turn.window === state.turn);
      if (index >= state.turns.length - 1) { pause(); return; }
      await selectTurn(state.turns[index + 1].window, true);
      if (state.playing) schedule();
    }, Number($("speed").value));
  }
  async function play() {
    if (state.playing) { pause(); return; }
    if (state.turns.length < 2 || state.loading) return;
    $("live-follow").checked = false;
    if (state.turn === state.turns.at(-1).window) await selectTurn(state.turns[0].window);
    if (!state.replay) return;
    state.playing = true; renderTransport(); schedule();
  }
  async function refresh() {
    if (!state.run || state.loading || state.playing) return;
    const generation = navigation(); loading(true);
    try {
      const base = "/runs/" + encodeURIComponent(state.run);
      const [overview, turns] = await Promise.all([api(base), api(base + "/turns")]);
      if (!isCurrent(generation)) return;
      state.overview = overview; state.turns = turns.items;
      $("run-status").textContent = readable(overview.run.status); $("run-status").className = "badge " + overview.run.status;
      $("run-summary").textContent = owners().length + " owner–agent pairs · " + state.turns.length + " recorded turns · " + state.run.slice(0, 8);
      const target = $("live-follow").checked ? state.turns.at(-1)?.window : state.turn;
      if (target !== undefined) await selectTurn(target);
      else loading(false);
    } catch (exception) { fail(exception, generation); }
  }
  function disconnect(message) {
    pause(); navigation(); state.connected = false; state.overview = null; state.replay = null;
    state.runs = []; state.turns = []; state.logRows = []; state.run = ""; state.owner = "";
    state.controlEpoch += 1; state.controlsLoading = false; state.controlsError = ""; state.execution = null; state.boundaries = null;
    state.operationBusy = false; state.operations.clear(); state.pendingOperation = null; state.lastIntervention = null; $("operator-prompt").value = "";
    receipt(""); setMode("replay"); renderControls();
    state.runsNext = null; state.turn = 0; state.channel = "all"; state.inspect = "persona";
    $("live-follow").checked = false; $("log-search").value = ""; $("focus-only").checked = false;
    for (const id of ["dialogue", "owner-list", "inspector-body", "log-list", "run-select", "detail-content", "detail-title", "detail-meta", "turn-markers"]) $(id).replaceChildren();
    for (const [id, value] of [["run-name", "Select a run"], ["run-summary", "Choose a saved run to inspect its turns."],
      ["run-status", "No run selected"], ["inspector-title", "Select a person"], ["turn-heading", "Choose a turn"],
      ["turn-caption", "Saved owner and agent messages appear here."], ["turn-now", "—"], ["turn-total", "—"],
      ["cast-count", "0"], ["log-count", "0 records"]]) $(id).textContent = value;
    for (const id of ["previous-turn", "next-turn", "play", "refresh-turn", "copy-link", "turn-slider"]) $(id).disabled = true;
    for (const button of $("channel-tabs").querySelectorAll("button")) {
      const selected = button.dataset.channel === "all";
      button.classList.toggle("is-active", selected); button.setAttribute("aria-pressed", String(selected));
    }
    if ($("detail-dialog").open) $("detail-dialog").close();
    $("run-content").hidden = true; $("empty-runs").hidden = true;
    $("more-runs").hidden = true; loading(false); error(message);
    $("connection-status").textContent = "Local service unavailable";
  }
  async function connect() {
    if (state.connecting) return;
    state.connecting = true;
    const generation = navigation();
    const url = new URL(location.href);
    const run = url.searchParams.get("run") || requestedRun;
    const turn = Number(url.searchParams.get("turn") || requestedTurn);
    loading(true); $("connection-status").textContent = "Loading saved runs…";
    $("retry-button").disabled = true;
    try {
      await loadRuns();
      if (!isCurrent(generation)) return;
      state.connected = true;
      await selectRun(run || state.runs[0]?.id || "", Number.isInteger(turn) ? turn : 0);
    } catch (exception) {
      if (isCurrent(generation)) disconnect(exception.message);
    } finally {
      state.connecting = false; $("retry-button").disabled = false;
    }
  }
  $("mode-replay").addEventListener("click", () => setMode("replay"));
  $("mode-live").addEventListener("click", () => setMode("live"));
  $("live-step").addEventListener("click", () => requestExecution("step"));
  $("live-continue").addEventListener("click", () => requestExecution("continue"));
  $("live-pause").addEventListener("click", () => operation('/runs/' + encodeURIComponent(state.run) + '/execution/pause', {}, 'pause'));
  $("operator-prompt").addEventListener("input", () => {
    state.pendingOperation = null; $("retry-operation").hidden = true; renderControls();
  });
  $("prompt-target").addEventListener("change", renderControls);
  $("prompt-form").addEventListener("submit", event => {
    event.preventDefault();
    if ($("send-prompt").disabled) return;
    operation('/runs/' + encodeURIComponent(state.run) + '/interventions', {
      owner_id: state.owner, target: $("prompt-target").value, prompt: $("operator-prompt").value.trim(),
      expected_window: state.execution.current_window,
    }, 'prompt');
  });
  $("branch-turn").addEventListener("click", () => branch(state.turn));
  $("restart-scenario").addEventListener("click", () => branch(0));
  $("retry-operation").addEventListener("click", () => {
    const request = state.pendingOperation;
    if (request?.run === state.run) operation(request.path, Object.fromEntries(Object.entries(request.body).filter(([key]) => key !== 'request_id')), request.kind, request);
  });
  $("refresh-controls").addEventListener("click", refreshControls);
  $("run-select").addEventListener("change", event => { $("live-follow").checked = false; selectRun(event.target.value); });
  $("refresh-runs").addEventListener("click", async () => {
    if (!state.connected) { connect(); return; }
    const generation = state.generation;
    try {
      await loadRuns();
      if (!isCurrent(generation)) return;
      if (!state.run && state.runs.length) await selectRun(state.runs[0].id);
    } catch (exception) { fail(exception, generation); }
  });
  $("more-runs").addEventListener("click", async () => {
    const generation = state.generation;
    try { await loadRuns(true); } catch (exception) { fail(exception, generation); }
  });
  $("refresh-turn").addEventListener("click", refresh);
  $("retry-button").addEventListener("click", () => {
    if (!state.connected) connect();
    else if (state.overview) refresh();
    else selectRun(state.run || state.runs[0]?.id || "");
  });
  $("focus-only").addEventListener("change", () => { renderDialogue(); renderLogs(); });
  $("inspector-tabs").addEventListener("click", event => {
    const button = event.target.closest("button[data-inspect]");
    if (button) { state.inspect = button.dataset.inspect; renderInspector(); }
  });
  $("channel-tabs").addEventListener("click", event => {
    const button = event.target.closest("button[data-channel]");
    if (!button) return;
    state.channel = button.dataset.channel;
    for (const item of $("channel-tabs").querySelectorAll("button")) {
      item.classList.toggle("is-active", item === button); item.setAttribute("aria-pressed", String(item === button));
    }
    renderLogs();
  });
  $("log-search").addEventListener("input", renderLogs);
  $("previous-turn").addEventListener("click", () => {
    $("live-follow").checked = false;
    const index = state.turns.findIndex(turn => turn.window === state.turn);
    if (index > 0) selectTurn(state.turns[index - 1].window);
  });
  $("next-turn").addEventListener("click", () => {
    $("live-follow").checked = false;
    const index = state.turns.findIndex(turn => turn.window === state.turn);
    if (index >= 0 && index < state.turns.length - 1) selectTurn(state.turns[index + 1].window);
  });
  $("turn-slider").addEventListener("input", event => {
    $("live-follow").checked = false;
    const target = state.turns[Number(event.target.value)]; if (target) selectTurn(target.window);
  });
  $("play").addEventListener("click", play);
  $("speed").addEventListener("change", schedule);
  $("live-follow").addEventListener("change", () => { if ($("live-follow").checked) { pause(); refresh(); } });
  $("close-detail").addEventListener("click", () => $("detail-dialog").close());
  $("copy-link").addEventListener("click", async () => {
    const button = $("copy-link");
    try {
      await navigator.clipboard.writeText(location.href);
      button.title = "Turn link copied";
      button.setAttribute("aria-label", "Turn link copied");
      $("connection-status").textContent = "Turn link copied";
      setTimeout(() => {
        button.title = "Copy link to this turn";
        button.setAttribute("aria-label", "Copy link to this turn");
        if (!state.loading) $("connection-status").textContent = "Local · saved replay";
      }, 2000);
    } catch { error("The browser could not copy the link. Copy this page's address from the address bar."); }
  });
  document.addEventListener("keydown", event => {
    if (!state.replay || event.altKey || event.ctrlKey || event.metaKey ||
        event.target.closest("input,select,textarea,button,a,dialog,[contenteditable=true]")) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); $("previous-turn").click(); }
    if (event.key === "ArrowRight") { event.preventDefault(); $("next-turn").click(); }
    if (event.code === "Space") { event.preventDefault(); play(); }
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
  setInterval(() => {
    if (state.connected && state.run && !document.hidden) {
      if ($("live-follow").checked) refresh();
      if (state.mode === "live" || executionBusy()) refreshControls();
    }
  }, 5000);
  connect();
})();
