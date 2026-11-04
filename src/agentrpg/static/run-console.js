"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const colors = ["#243f6b", "#4f6d94", "#46518a", "#2f6883", "#656b83"];
  const collections = ["chat", "mail", "board", "events"];
  const initialURL = new URL(location.href);
  const requestedRun = initialURL.searchParams.get("run") || "";
  const requestedTurn = Number(initialURL.searchParams.get("turn") || 0);
  const state = {
    connected: false, connecting: false, runs: [], runsNext: null, run: "", overview: null, turns: [], turn: 0,
    owner: "", replay: null, channel: "all", inspect: "persona", playing: false,
    timer: null, generation: 0, controller: null, loading: false, logRows: [],
    mode: "replay", execution: null, boundaries: null, controlsLoading: false, controlsError: "", controlEpoch: 0,
    language: "en", translations: new Map(), pendingTurn: null, sliderTimer: null, detailRecord: null, detailOriginal: false,
    operationBusy: false, operations: new Map(), pendingOperation: null, lastIntervention: null,
    clock: null, clockKey: "", clockError: "", clockLoading: false, clockTab: "week",
    board: null, boardError: "", boardLoading: false, boardFilter: "all", jobPick: new Map(),
  };
  const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const han = /[\u3400-\u9fff]/;
  const translate = (value, language = state.language) => typeof value === "string" && language === "en"
    ? state.translations.get(value) || (han.test(value) ? "[English translation unavailable · Open original]" : value) : value;
  const absorbEnglish = result => {
    for (const [source, english] of Object.entries(result.english?.texts || {})) state.translations.set(source, english);
  };
  const displayValue = (value, language = state.language) => {
    if (typeof value === "string") return translate(value, language);
    if (Array.isArray(value)) return value.map(item => displayValue(item, language));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, displayValue(item, language)]));
    return value;
  };
  function soften(element) {
    if (reducedMotion()) return;
    element.getAnimations().forEach(animation => animation.cancel());
    element.animate([{opacity: .65, transform: "translateY(4px)"}, {opacity: 1, transform: "translateY(0)"}], {duration: 180, easing: "cubic-bezier(.2,.7,.2,1)"});
  }
  const node = (tag, content, className) => {
    const element = document.createElement(tag);
    if (content !== undefined && content !== null) element.textContent = translate(content);
    if (className) element.className = className;
    return element;
  };
  const plain = value => typeof value === "string" ? translate(value) : JSON.stringify(displayValue(value), null, 2);
  const preview = (value, length = 145) => {
    const text = String(translate(value) || "").replace(/\s+/g, " ").trim();
    return text.length > length ? text.slice(0, length - 1) + "…" : text;
  };
  const readable = value => String(value || "").replaceAll("_", " ");
  const padded = value => String(value).padStart(2, "0");
  const owners = () => [...(state.overview?.participants || [])].sort((left, right) => left.owner_id.localeCompare(right.owner_id, undefined, {numeric: true}));
  const ownerName = id => {
    const person = state.overview?.run?.scenario?.owners?.find(owner => owner.id === id);
    const perspective = state.replay?.perspectives?.find(owner => owner.owner_id === id);
    return translate(perspective?.view?.owner_name || person?.name || id || "Director");
  };
  const color = id => colors[Math.max(0, owners().findIndex(owner => owner.owner_id === id)) % colors.length];
  const agentName = id => translate(owners().find(owner => owner.owner_id === id)?.agent_name) || "Assistant for " + ownerName(id);
  const activeSummary = () => state.turns.find(turn => turn.window === state.turn);
  // A clocked run's window is a page of story time. Its name is a time in place of "Turn NN".
  const clocked = () => !!state.overview?.run?.scenario?.clock;
  const pageWord = () => clocked() ? "page" : "turn";
  const turnName = window => clocked()
    ? state.turns.find(turn => turn.window === window)?.at || "Page " + padded(window + 1)
    : "Turn " + padded(window + 1);
  // Page length comes from the clock section. A week of two-hour pages is the common case.
  const pageLength = () => {
    const minutes = Number(state.overview?.run?.scenario?.clock?.epoch_minutes) || 120;
    const hours = minutes / 60;
    return Number.isInteger(hours) ? (["", "one", "two", "three", "four", "six"][[0, 1, 2, 3, 4, 6].indexOf(hours)] || hours) + "-hour" : minutes + "-minute";
  };
  const CLOCKED_NOTE = "The run script drives this clocked run. Step and Continue are not available here.";
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
    const isClocked = clocked();
    $('live-step').disabled = busy || isClocked || !capability.can_execute;
    $('live-continue').disabled = busy || isClocked || !capability.can_execute;
    $('live-pause').disabled = busy || !capability.can_pause;
    $('send-prompt').disabled = busy || isClocked || !state.owner || !prompt || !capability.can_intervene || !Number.isInteger(next);
    $('send-prompt').textContent = !isClocked && Number.isInteger(next) ? 'Queue for Turn ' + padded(next + 1) : 'Queue prompt';
    $('execution-status').textContent = busy ? 'Sending request' : readable(status?.execution?.status || status?.status || (state.controlsLoading ? 'Loading' : 'Unavailable'));
    $('execution-status').className = 'badge ' + (status?.execution?.status || status?.status || '');
    $('execution-caption').textContent = isClocked
      ? (state.clock ? 'Story time now: ' + state.clock.now + '. Viewing: ' + turnName(state.turn) + '.' : 'Viewing: ' + turnName(state.turn) + '.')
      : status
      ? 'Live cursor: Turn ' + padded(status.current_window + 1) + ' · ' + readable(status.phase) + '. Viewing: Turn ' + padded(state.turn + 1) + '.'
      : (state.controlsError || (state.run ? 'Loading live run state…' : 'Select a run to use live controls.'));
    $('execution-reason').textContent = isClocked
      ? capability.reason || CLOCKED_NOTE
      : status?.execution?.error || capability.reason || (executionBusy() ? 'The live run continues while you inspect saved turns. Pause stops at a safe boundary.' : 'Step runs one full turn. Continue runs until completion or a pause.');
    const budget = status?.budget;
    $('execution-budget').textContent = budget ? '$' + Number(budget.available_usd).toFixed(2) + ' available · $' + Number(budget.spent_usd).toFixed(2) + ' spent · shared $' + Number(budget.limit_usd).toFixed(2) + ' limit' : '';
    const boundary = state.boundaries?.items?.find(item => item.window === state.turn);
    const initial = state.boundaries?.items?.find(item => item.window === 0);
    $('branch-turn').textContent = isClocked ? 'Branch before this page' : 'Branch before Turn ' + padded(state.turn + 1);
    $('branch-turn').disabled = busy || isClocked || !boundary?.available;
    $('restart-scenario').hidden = isClocked || !state.run || !!boundary?.available || !initial?.available || state.turn === 0;
    $('restart-scenario').disabled = busy;
    $('branch-reason').textContent = isClocked ? 'Branching from a clocked run is not implemented.' : boundary?.available
      ? (boundary.source === 'initial' ? 'This starts from the frozen initial scenario. The new run is paused.' : 'This restores the saved state before this turn. The new run is paused.')
      : boundary?.reason || (state.boundaries ? 'No complete state was saved before this turn.' : state.controlsError || 'Checking saved turn boundaries…');
    const parent = state.boundaries?.parent_run_id;
    $('parent-run-link').hidden = !parent;
    if (parent) {
      $('parent-run-link').href = '/console?run=' + encodeURIComponent(parent) + '&turn=' + state.boundaries.parent_window;
      $('parent-run-link').textContent = 'Branch of ' + parent.slice(0, 8) + ' · before Turn ' + padded(state.boundaries.parent_window + 1) + ' ↗';
    } else { $('parent-run-link').textContent = ''; $('parent-run-link').removeAttribute('href'); }
    $('prompt-help').textContent = isClocked ? 'The console replays a clocked run. Prompts are not available here.' : $('prompt-target').value === 'actor'
      ? 'The Actor receives this Director cue at the indicated turn. A branch applies it before the selected turn.'
      : 'The Agent receives this as a message from its owner at the indicated turn. A branch applies it before the selected turn.';
    $('run-select').disabled = busy;
    $('operator-prompt').disabled = busy || isClocked;
    $('prompt-target').disabled = busy || isClocked;
    $('retry-operation').disabled = busy;
    $('refresh-controls').disabled = busy || state.controlsLoading;
  }
  async function refreshControls() {
    if (!state.run || !state.connected || state.controlsLoading || state.operationBusy) return;
    const id = state.run; const epoch = state.controlEpoch;
    state.controlsLoading = true; renderControls();
    try {
      const base = '/runs/' + encodeURIComponent(id);
      const [execution, boundaries] = await Promise.all([controlAPI(base + '/execution'), clocked() ? null : controlAPI(base + '/boundaries')]);
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
    $("workspace").classList.toggle("is-loading", value);
    $("run-content").setAttribute("aria-busy", String(value));
    $("connection-status").textContent = value ? (state.replay ? "Showing " + turnName(state.turn) + " · Loading " + turnName(state.pendingTurn ?? state.turn) + "…" : "Loading saved turn…") : "Local · saved replay";
  }
  function navigation() {
    state.controller?.abort();
    state.controller = new AbortController();
    state.generation += 1;
    error("");
    clearTimeout(state.sliderTimer);
    return state.generation;
  }
  async function api(path, signal = state.controller?.signal) {
    let response;
    try {
      response = await fetch("/api/v1/observer" + path + (path.includes("?") ? "&" : "?") + "display=en", {
        cache: "no-store", redirect: "error",
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(120000)]) : AbortSignal.timeout(120000),
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
    pause(); state.pendingTurn = null; loading(false); renderTransport();
    if (exception.auth) {
      disconnect(exception.message);
    } else {
      error(exception.message);
      if (!state.overview) {
        $("run-name").textContent = "Run unavailable";
        $("run-status").textContent = "Unavailable";
        $("run-summary").textContent = "Choose another saved run or retry this request.";
        $("turn-heading").textContent = "Choose a turn";
        $("dialogue").replaceChildren(node("p", "Saved records could not be loaded.", "empty-state"));
        $("connection-status").textContent = "Run unavailable";
      }
    }
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
      const option = node("option", translate(run.title || run.name || "Untitled run") + " · " + run.id.slice(0, 8));
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
    absorbEnglish(result);
    state.runs = append ? [...state.runs, ...result.items.filter(run => !state.runs.some(old => old.id === run.id))] : result.items;
    state.runsNext = result.next_offset;
    renderRunOptions();
  }
  async function selectRun(id, preferredTurn = 0) {
    pause();
    const generation = navigation();
    state.controlEpoch += 1; state.controlsLoading = false; state.execution = null; state.boundaries = null;
    state.controlsError = ""; state.pendingOperation = null; state.lastIntervention = null; receipt(""); $("operator-prompt").value = "";
    state.pendingTurn = null; state.run = id; state.replay = null; state.overview = null; state.turns = []; state.owner = "";
    resetClock(); applyRunKind();
    $("dialogue").dataset.key = ""; $("owner-list").dataset.key = "";
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
      const [overview, turns] = await Promise.all([api(base + "?compact=true"), api(base + "/turns")]);
      if (!isCurrent(generation)) return;
      absorbEnglish(overview); state.overview = overview; state.turns = turns.items;
      state.owner = owners()[0]?.owner_id || "";
      $("run-name").textContent = translate(overview.run.scenario.title || overview.run.scenario.name || "Untitled run");
      $("run-status").textContent = readable(overview.run.status);
      $("run-status").className = "badge " + overview.run.status;
      runSummary();
      $("cast-count").textContent = padded(owners().length);
      applyRunKind(); loadClock();
      $("open-observer").href = "/runs";
      renderCast(); renderControls(); refreshControls();
      const target = state.turns.some(turn => turn.window === preferredTurn) ? preferredTurn : state.turns[0]?.window;
      if (target === undefined) {
        loading(false);
        $("dialogue").replaceChildren(node("p", "This run has no recorded " + pageWord() + "s yet. Refresh after the worker saves a " + pageWord() + ".", "empty-state"));
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
      absorbEnglish(page);
      if (!result) result = page;
      else {
        for (const key of [...collections, ...(clocked() ? ["jobs", "actions"] : [])]) {
          const ids = new Set(result[key].map(record => record.id));
          result[key].push(...page[key].filter(record => !ids.has(record.id)));
        }
        if (clocked()) {
          const seen = new Set(result.perspectives.map(view => view.job_id));
          result.perspectives.push(...page.perspectives.filter(view => !seen.has(view.job_id)));
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
    state.pendingTurn = window;
    loading(true);
    renderTransport();
    if (!state.replay) $("dialogue").replaceChildren(node("p", clocked() ? "Loading " + turnName(window) + "…" : "Loading turn " + (window + 1) + "…", "empty-state"));
    if ($("detail-dialog").open) $("detail-dialog").close();
    try {
      const result = await pages(window, generation);
      if (!isCurrent(generation) || !result) return;
      state.turn = window; state.pendingTurn = null; state.replay = result; state.jobPick.clear();
      setURL(); renderReplay(); loading(false); soften($("dialogue")); markClockPage();
    } catch (exception) { fail(exception, generation); }
  }
  function renderCast() {
    const castKey = state.language + owners().map(owner => owner.owner_id + ownerName(owner.owner_id) + agentName(owner.owner_id)).join("|");
    if ($("owner-list").dataset.key === castKey) {
      for (const button of $("owner-list").children) {
        const selected = button.dataset.owner === state.owner;
        button.classList.toggle("is-selected", selected); button.setAttribute("aria-pressed", String(selected));
      }
      return;
    }
    $("owner-list").dataset.key = castKey;
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
    const requestedIndex = state.turns.findIndex(turn => turn.window === (state.pendingTurn ?? state.turn));
    $("turn-now").textContent = index < 0 ? "00" : padded(state.turn + 1);
    $("turn-total").textContent = padded(state.turns.length);
    $("turn-heading").textContent = clocked() && index < 0 ? "Choose a page" : turnName(state.turn);
    $("turn-caption").textContent = clocked() ? "Saved records from this " + pageLength() + " page" : "Saved dialogue · window " + state.turn;
    $("turn-slider").min = "0"; $("turn-slider").max = String(Math.max(0, state.turns.length - 1));
    $("turn-slider").value = String(Math.max(0, requestedIndex));
    $("turn-slider").setAttribute("aria-valuetext", clocked()
      ? turnName(state.pendingTurn ?? state.turn) + ", page " + (Math.max(0, requestedIndex) + 1) + " of " + state.turns.length
      : "Turn " + ((state.pendingTurn ?? state.turn) + 1) + " of " + state.turns.length);
    $("turn-slider").disabled = !state.turns.length;
    $("previous-turn").disabled = requestedIndex <= 0;
    $("next-turn").disabled = requestedIndex < 0 || requestedIndex >= state.turns.length - 1;
    $("play").disabled = state.turns.length < 2;
    $("refresh-turn").disabled = !state.overview;
    $("copy-link").disabled = !state.overview || !state.turns.length;
    const markerKey = state.run + ":" + state.turns.map(turn => turn.window + "/" + (turn.jobs?.failed || 0)).join(",");
    if ($("turn-markers").dataset.key !== markerKey) {
      $("turn-markers").dataset.key = markerKey;
      $("turn-markers").replaceChildren();
      for (const turn of state.turns) {
      const marker = node("button", padded(turn.window + 1), "turn-marker" + (turn.window === state.turn ? " is-active" : ""));
      marker.type = "button"; marker.dataset.turn = turn.window;
      marker.title = (turn.at ? turn.at + " until " + turn.until : "Turn " + (turn.window + 1)) + " · " + (turn.counts?.events || 0) + " events";
      marker.setAttribute("aria-label", marker.title);
      if (turn.window === state.turn) marker.setAttribute("aria-current", "step");
      if (turn.jobs?.failed) marker.classList.add("has-error");
      marker.addEventListener("click", () => { $("live-follow").checked = false; selectTurn(turn.window); });
      $("turn-markers").append(marker);
      }
    }
    for (const marker of $("turn-markers").children) {
      const active = Number(marker.dataset.turn) === state.turn;
      marker.classList.toggle("is-active", active);
      marker.classList.toggle("is-pending", Number(marker.dataset.turn) === state.pendingTurn);
      if (active) marker.setAttribute("aria-current", "step"); else marker.removeAttribute("aria-current");
      if (active && !state.loading) {
        const strip = $("turn-markers");
        if (marker.offsetLeft < strip.scrollLeft || marker.offsetLeft + marker.offsetWidth > strip.scrollLeft + strip.clientWidth)
          strip.scrollTo({left: marker.offsetLeft - strip.clientWidth / 2, behavior: reducedMotion() ? "instant" : "smooth"});
      }
    }
    renderControls();
    $("play-label").textContent = state.playing ? "Pause" : "Play";
    $("play").setAttribute("aria-pressed", String(state.playing));
    $("play").querySelector("path").setAttribute("d", state.playing ? "M6 4h3v12H6zM12 4h3v12h-3z" : "m7 4 9 6-9 6z");
  }
  function renderDialogue() {
    if (!state.replay) return;
    const visible = $("focus-only").checked ? owners().filter(owner => owner.owner_id === state.owner) : owners();
    const key = state.run + ":" + state.turn + ":" + state.language + ":" + visible.map(owner => owner.owner_id).join(",") + ":" + state.replay.chat.map(message => message.id).join(",");
    if ($("dialogue").dataset.key === key) {
      for (const card of $("dialogue").children) card.classList.toggle("is-selected", card.dataset.owner === state.owner);
      return;
    }
    $("dialogue").dataset.key = key;
    $("dialogue").replaceChildren();
    $("dialogue").classList.toggle("is-focused", visible.length === 1);
    for (const owner of visible) {
      const id = owner.owner_id;
      const messages = state.replay.chat.filter(message => message.owner_id === id);
      const card = node("section", null, "dialogue-card" + (id === state.owner ? " is-selected" : ""));
      card.dataset.owner = id;
      card.style.setProperty("--cast-color", color(id));
      card.setAttribute("aria-label", ownerName(id) + " and assistant, " + (clocked() ? turnName(state.turn) : "turn " + (state.turn + 1)));
      const header = node("div", null, "dialogue-card-head");
      const name = node("div");
      name.append(node("h3", ownerName(id)), node("span", id + " / " + agentName(id), "mini-label"));
      header.append(node("span", ownerName(id).slice(0, 1), "cast-avatar"), name, node("span", "PRIVATE", "badge"));
      const scroll = node("div", null, "dialogue-scroll");
      scroll.dataset.owner = id; scroll.tabIndex = 0; scroll.setAttribute("aria-label", ownerName(id) + " conversation");
      if (!messages.length) scroll.append(node("p", "No dialogue was saved for this pair in this " + pageWord() + ".", "empty-note"));
      for (const message of messages) {
        const isAgent = message.source === "agent";
        const speech = node("article", null, "speech " + (isAgent ? "speech-agent" : "speech-owner"));
        speech.dataset.source = message.source;
        const meta = node("div", null, "speech-meta");
        meta.append(node("span", isAgent ? "◇  " + agentName(id) : ownerName(id)));
        if (message.at) meta.append(node("span", message.at, "speech-time"));
        const inspect = node("button", "↗");
        inspect.type = "button"; inspect.title = "Inspect saved message";
        inspect.setAttribute("aria-label", "Inspect " + (isAgent ? "agent" : "owner") + " message from " + ownerName(id));
        inspect.addEventListener("click", () => detail("Delivered owner chat", message, ownerName(id)));
        if (han.test(message.content)) {
          const original = node("button", state.language === "en" ? "Original" : "English", "source-button");
          original.type = "button"; original.title = "Compare translation with saved source";
          original.addEventListener("click", () => detail("Delivered owner chat", message, ownerName(id), state.language === "en"));
          meta.append(original);
        }
        meta.append(inspect);
        speech.append(meta, node("p", message.content, "speech-text")); scroll.append(speech);
      }
      if (messages.some(message => message.source === "owner") && !messages.some(message => message.source === "agent")) {
        const incomplete = state.replay.pagination.next_offset !== null;
        scroll.append(node("p", incomplete ? "More records remain to load." : "No agent reply was saved in this " + pageWord() + ".", "empty-note"));
      }
      const jumps = node("div", null, "dialogue-jumps");
      for (const [source, label] of [["owner", "Owner"], ["agent", "Agent"]]) {
        const count = messages.filter(message => message.source === source).length;
        const jump = node("button", label + " · " + (count || "No reply"));
        jump.type = "button"; jump.dataset.speaker = source; jump.disabled = !count;
        jump.setAttribute("aria-label", "Jump to " + ownerName(id) + (source === "agent" ? "'s agent reply" : "'s message"));
        jump.addEventListener("click", () => {
          const target = scroll.querySelector('[data-source="' + source + '"]');
          if (target) scroll.scrollTo({top: scroll.scrollTop + target.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 12, behavior: reducedMotion() ? "instant" : "smooth"});
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
  function pickJob(body, kind, list, id, word) {
    const key = kind + ":" + state.owner;
    const selected = list.find(item => id(item) === state.jobPick.get(key)) || list[0];
    if (list.length < 2) return selected;
    const picker = node("div", null, "job-picker");
    picker.setAttribute("role", "group"); picker.setAttribute("aria-label", word + " on this page");
    list.forEach((item, index) => {
      const button = node("button", (index + 1) + (item.at ? " · " + item.at : ""));
      button.type = "button"; button.setAttribute("aria-pressed", String(item === selected));
      button.classList.toggle("is-active", item === selected);
      button.title = word + " " + (index + 1) + " of " + list.length;
      button.addEventListener("click", () => { state.jobPick.set(key, id(item)); renderInspector(); });
      picker.append(button);
    });
    body.append(node("p", list.length + " " + word.toLowerCase() + "s on this page", "mini-label"), picker);
    return selected;
  }
  function withinJob(event, job, jobs) {
    if (jobs.length < 2 || !Number.isFinite(event.story_s) || job.start_s == null) return true;
    return event.story_s >= job.start_s && (job.end_s == null || event.story_s < job.end_s + 1);
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
    // One owner can have many jobs on a clocked page. A picker reaches each one in story order.
    const views = state.replay.perspectives.filter(item => item.owner_id === state.owner);
    const agentJobs = state.replay.jobs.filter(item => item.owner_id === state.owner && item.kind === "agent");
    const perspective = state.inspect === "agent" ? views[0] : pickJob(body, "actor", views, item => item.job_id, "Actor job");
    const view = perspective?.view;
    const job = state.inspect === "agent" ? pickJob(body, "agent", agentJobs, item => item.id, "Agent job") : agentJobs[0];
    if (state.inspect === "persona") {
      if (!view) { body.append(node("p", "No Actor input was saved for this " + pageWord() + ". Select another " + pageWord() + " to inspect its perspective.", "empty-state")); return; }
      body.append(node("p", clocked() ? "ACTOR VIEW · " + (perspective.at || turnName(state.turn)) : "ACTOR VIEW · START OF TURN " + padded(state.turn + 1), "mini-label"));
      if (view.clock) body.append(section("Clock view", Object.entries(view.clock).filter(([, value]) => value !== null && value !== "")
        .map(([key, value]) => readable(key) + ": " + plain(value)), true));
      body.append(section("Director cue", view.cue, true));
      if (view.goals?.length) body.append(section("Goals · " + view.goals.length, view.goals, true));
      if (view.beliefs?.length) body.append(section("Declared beliefs · " + view.beliefs.length, view.beliefs));
      if (view.known_facts?.length) body.append(section("Known facts · " + view.known_facts.length, view.known_facts));
      body.append(section("Initial request", view.request));
      if (view.acceptance_rules?.length) body.append(section("Decision boundaries", view.acceptance_rules));
      if (view.observations?.length) body.append(section("Observations · " + view.observations.length, view.observations));
    } else if (state.inspect === "agent") {
      if (!job) { body.append(node("p", "No agent activation was saved for this " + pageWord() + ".", "empty-state")); return; }
      const tools = state.replay.events.filter(event => event.kind === "assistant_tool_call" && event.owner_id === state.owner && withinJob(event, job, agentJobs));
      body.append(node("p", agentName(state.owner) + " · " + (clocked() ? job.at || turnName(state.turn) : "TURN " + padded(state.turn + 1)), "mini-label"));
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
      body.append(node("p", clocked() ? "These records were visible to this Actor when this job began. Later changes appear in the log." : "These records were visible to this Actor when the turn began. Changes made during the turn appear in its logs.", "muted"));
      if (!resources) { body.append(node("p", "No resource view was saved for this " + pageWord() + ".", "empty-state")); return; }
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
  function renderDetail() {
    const original = state.detailOriginal;
    $("detail-content").textContent = typeof state.detailRecord === "string"
      ? (original ? state.detailRecord : displayValue(state.detailRecord, "en"))
      : JSON.stringify(original ? state.detailRecord : displayValue(state.detailRecord, "en"), null, 2);
    $("detail-language").textContent = original ? "Show English" : "Show original";
    $("detail-language").setAttribute("aria-pressed", String(original));
    $("detail-reading-note").textContent = original ? "Original saved record · Unchanged" : "Local English translation · Check the original for exact wording";
  }
  function detail(title, record, meta = "", original = false) {
    state.detailRecord = record; state.detailOriginal = original;
    $("detail-title").textContent = translate(title);
    $("detail-meta").textContent = (clocked() ? turnName(state.turn) : "Turn " + (state.turn + 1)) + (meta ? " · " + meta : "") + " · Saved evidence";
    renderDetail();
    const dialog = $("detail-dialog");
    dialog.showModal(); soften(dialog);
  }
  async function closeDetail() {
    const dialog = $("detail-dialog");
    if (!dialog.open) return;
    if (!reducedMotion()) {
      try { await dialog.animate([{opacity: 1, transform: "translateY(0)"}, {opacity: 0, transform: "translateY(5px)"}], {duration: 130, easing: "ease-in"}).finished; } catch { /* Superseded by navigation. */ }
    }
    dialog.close();
  }
  function setLanguage(language) {
    state.language = language;
    $("language-toggle").textContent = language === "en" ? "English" : "Original";
    $("language-toggle").setAttribute("aria-pressed", String(language === "original"));
    $("language-note").textContent = language === "en" ? "Local English translation · Original records available" : "Original saved text · Interface in English";
    renderRunOptions();
    if (state.overview) $("run-name").textContent = translate(state.overview.run.scenario.title || state.overview.run.scenario.name || "Untitled run");
    if (state.replay) renderReplay();
    state.clockKey = ""; renderClock();
    if ($("detail-dialog").open) renderDetail();
  }
  function buildLogs() {
    const replay = state.replay;
    const records = [];
    const add = (category, title, description, record, related, problem = false, secondary = false) => {
      records.push({category, title, description: preview(description), record, related: related.filter(Boolean), problem, secondary,
        time: record.created_at || "", haystack: (title + " " + plain(record) + " " + JSON.stringify(record)).toLowerCase()});
    };
    if (clocked()) { buildClockLogs(replay, records, add); state.logRows = records; return; }
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
    for (const job of replay.jobs.filter(item => item.error)) {
      const reason = job.failure_kind === "budget" ? "Budget limit reached. "
        : job.failure_kind ? readable(job.failure_kind) + ". " : "";
      add("errors", "Job error · " + job.kind, reason + job.error, job, [job.owner_id], true);
    }
    records.sort((a, b) => a.time.localeCompare(b.time) || String(a.record.id).localeCompare(String(b.record.id)));
    state.logRows = records;
  }
  // These records already have their own row, so a clocked page does not list them twice.
  const DUPLICATE_EVENTS = new Set(["owner_message", "inbox_received", "board_posted", "board_batch"]);
  const RECORD_EVENT = /^(resource_|record_|world_|market_|wallet_|bill_|listing_|order_|site_|web_|transfer_|reservation_)/;
  function buildClockLogs(replay, records, add) {
    for (const message of replay.chat) add("chat", message.source === "agent" ? "Agent reply" : "Owner message", message.content, message, [message.owner_id]);
    for (const message of replay.mail) add("mail", message.subject || "Private mail", message.body, message, [message.sender_owner_id, message.recipient_owner_id]);
    for (const message of replay.board) {
      const direct = message.visibility === "direct";
      const people = direct ? message.participants.map(person => person.owner_id) : [message.owner_id];
      add("board", (direct ? "Direct · " : "") + (message.title || (direct ? "reply" : "Board reply")), message.body, message, people);
    }
    const toolOwners = new Set();
    for (const event of replay.events) {
      if (DUPLICATE_EVENTS.has(event.kind)) continue;
      const payload = event.payload || {};
      const isTool = event.kind === "assistant_tool_call";
      if (isTool) toolOwners.add(event.owner_id);
      const problem = /fail|invalid|reject|error|abandoned|declined|expired/.test(event.kind) || payload.result?.ok === false;
      add(isTool ? "tools" : RECORD_EVENT.test(event.kind) ? "records" : "director", isTool ? readable(payload.tool) : readable(event.kind),
        isTool ? (payload.result?.ok === false ? plain(payload.result.error) : preview(plain(payload.arguments || payload.args || {})))
          : payload.cue || payload.reason || payload.error || payload.title || payload.memo || payload.output?.text || payload.text || "Saved environment event",
        event, [event.owner_id], problem);
    }
    for (const job of replay.jobs) {
      const reason = job.failure_kind === "budget" ? "Budget limit reached. " : job.failure_kind ? readable(job.failure_kind) + ". " : "";
      add("jobs", (job.kind === "actor" ? "Actor job" : "Agent job") + " · " + readable(job.status),
        job.error ? reason + job.error : job.output?.text || job.output?.message || job.output?.status || "No saved text.",
        job, [job.owner_id], job.status === "failed" || !!job.error);
    }
    for (const action of replay.actions) {
      const minutes = Math.round((action.duration_s || 0) / 60);
      add("actions", action.label, readable(action.process) + " · " + (minutes ? minutes + " min" : (action.duration_s || 0) + " s") + " · " + readable(action.status), action, [action.owner_id], false,
        action.process === "agent" && toolOwners.has(action.owner_id));
    }
    for (const item of replay.perturbations || []) {
      add("director", "Perturbation · " + item.outcome, item.label || "Saved draw", item, [item.owner_id]);
    }
    records.sort((a, b) => (a.record.story_s ?? 0) - (b.record.story_s ?? 0) || String(a.record.id).localeCompare(String(b.record.id)));
  }
  function renderLogs() {
    const list = $("log-list"); list.replaceChildren();
    if (!state.replay) { $("log-count").textContent = "0 records"; return; }
    const search = $("log-search").value.trim().toLowerCase();
    const records = state.logRows.filter(item =>
      (state.channel === "all" ? !item.secondary : item.category === state.channel || (state.channel === "errors" && item.problem)) &&
      (!$("focus-only").checked || item.related.includes(state.owner)) && (!search || item.haystack.includes(search)));
    $("log-count").textContent = records.length + " records";
    for (const record of records) {
      const row = node("button", null, "log-row" + (record.problem ? " has-error" : ""));
      row.type = "button";
      const time = record.record.at || (record.time ? new Date(record.time).toLocaleTimeString([], {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—");
      row.append(node("span", time, "log-time"), node("span", record.category, "log-kind"));
      const description = node("span", null, "log-description");
      description.append(node("strong", record.title), node("span", record.description));
      row.append(description, node("span", record.related.map(ownerName).join(" → ") || "Director", "log-owner"));
      row.addEventListener("click", () => detail(record.title, record.record, record.related.map(ownerName).join(" → ")));
      list.append(row);
    }
    if (!records.length) list.append(node("p", "No records match this view in the selected " + pageWord() + ".", "empty-state"));
    if (state.replay.pagination.next_offset !== null) {
      const more = node("button", "Load more records from this " + pageWord(), "tool-button");
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
    $("turn-caption").textContent = clocked()
      ? "Until " + summary?.until + " · " + counted(summary?.counts?.owner_chat || 0, "owner message") + " · " + (summary?.counts?.agent_chat || 0) + " agent " + ((summary?.counts?.agent_chat || 0) === 1 ? "reply" : "replies") +
        " · " + counted(summary?.counts?.events || 0, "event") + " · " + counted(summary?.jobs?.total || 0, "job") + " · " + counted(summary?.counts?.board || 0, "board post")
      : (summary?.counts?.owner_chat || 0) + " owner messages · " +
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
      const [overview, turns] = await Promise.all([api(base + "?compact=true"), api(base + "/turns")]);
      if (!isCurrent(generation)) return;
      absorbEnglish(overview); state.overview = overview; state.turns = turns.items;
      $("run-status").textContent = readable(overview.run.status); $("run-status").className = "badge " + overview.run.status;
      runSummary(); loadClock();
      if (state.clockTab === "board") loadBoard();
      const target = $("live-follow").checked ? state.turns.at(-1)?.window : state.turn;
      if (target !== undefined) await selectTurn(target);
      else loading(false);
    } catch (exception) { fail(exception, generation); }
  }
  function disconnect(message) {
    pause(); navigation(); state.connected = false; state.overview = null; state.replay = null;
    resetClock(); applyRunKind();
    state.translations.clear(); state.detailRecord = null; state.pendingTurn = null;
    $("owner-list").dataset.key = ""; $("turn-markers").dataset.key = ""; $("dialogue").dataset.key = "";
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
  function runSummary() {
    const id = state.run.slice(0, 8);
    $("run-summary").textContent = clocked()
      ? owners().length + " owner–agent pairs · " + state.turns.length + " " + pageLength() + " pages · " + id
      : owners().length + " owner–agent pairs · " + state.turns.length + " recorded turns · " + id;
  }
  function resetClock() {
    state.clock = null; state.clockKey = ""; state.clockError = ""; state.clockLoading = false; state.clockTab = "week";
    state.board = null; state.boardError = ""; state.boardLoading = false; state.boardFilter = "all"; state.jobPick.clear();
    for (const id of ["clock-now", "clock-range"]) $(id).textContent = "";
    for (const id of ["clock-stats", "clock-sites", "clock-week", "clock-board", "clock-queue", "clock-perturbations"]) $(id).replaceChildren();
  }
  // Window runs keep their layout. Only a run with a clock section changes these labels.
  function applyRunKind() {
    const isClocked = clocked();
    document.body.classList.toggle("is-clocked", isClocked);
    $("clock-panel").hidden = !isClocked;
    $("turn-word").textContent = isClocked ? "PAGE" : "TURN";
    $("turn-note").textContent = isClocked ? "Replay · " + pageLength() + " pages" : "Replay · saved turns";
    $("previous-turn").title = isClocked ? "Previous page" : "Previous turn";
    $("next-turn").title = isClocked ? "Next page" : "Next turn";
    $("previous-turn").setAttribute("aria-label", $("previous-turn").title);
    $("next-turn").setAttribute("aria-label", $("next-turn").title);
    $("log-search").placeholder = isClocked ? "Search this page" : "Search this turn";
    for (const channel of ["jobs", "actions", "records"]) {
      $("channel-tabs").querySelector('[data-channel="' + channel + '"]').hidden = !isClocked;
    }
    if (!isClocked && ["jobs", "actions", "records"].includes(state.channel)) state.channel = "all";
    if (isClocked) renderClock();
  }
  async function loadClock() {
    if (!clocked() || state.clockLoading) return;
    const id = state.run;
    state.clockLoading = true;
    try {
      const result = await api("/runs/" + encodeURIComponent(id) + "/clock", null);
      if (id !== state.run) return;
      absorbEnglish(result); state.clock = result; state.clockError = "";
    } catch (exception) {
      if (id !== state.run) return;
      if (exception.auth) { disconnect(exception.message); return; }
      state.clockError = "The clock could not load. Select Refresh to try again.";
    } finally {
      if (id === state.run) state.clockLoading = false;
    }
    renderClock(); renderControls();
  }
  async function loadBoard() {
    if (!clocked() || state.boardLoading) return;
    const id = state.run;
    state.boardLoading = true; state.boardError = "";
    if (!state.board) renderBoard();
    try {
      let items = [], after = null, counts = null;
      for (let index = 0; index < 40; index += 1) {
        const result = await api("/runs/" + encodeURIComponent(id) + "/board?limit=500" + (after ? "&after=" + encodeURIComponent(after) : ""), null);
        if (id !== state.run) return;
        absorbEnglish(result); items = items.concat(result.items); counts = result.counts; after = result.next_after;
        if (!after) break;
      }
      state.board = {items, counts, truncated: !!after};
    } catch (exception) {
      if (id !== state.run) return;
      if (exception.auth) { disconnect(exception.message); return; }
      state.boardError = "The board could not load. Select Refresh to try again.";
    } finally {
      if (id === state.run) state.boardLoading = false;
    }
    renderBoard();
  }
  const counted = (total, word) => total + " " + word + (total === 1 ? "" : "s");
  const money = value => {
    const amount = Number(value || 0);
    return "$" + amount.toFixed(amount !== 0 && amount < 1 ? 4 : 2);
  };
  const percent = value => (value * 100).toFixed(value > 0 && value < 0.1 ? 1 : 0) + "%";
  function clockStat(label, value, title) {
    const item = node("div", null, "clock-stat");
    item.append(node("dt", label), node("dd", value));
    if (title) item.title = title;
    return item;
  }
  function renderClockStats() {
    const clock = state.clock;
    const stats = $("clock-stats"); stats.replaceChildren();
    $("clock-now").textContent = clock ? clock.now : "—";
    $("clock-range").textContent = clock ? clock.start + " to " + clock.horizon : "";
    const sites = $("clock-sites"); sites.replaceChildren();
    if (!clock) return;
    const measured = clock.interference;
    stats.append(clockStat("Drama", readable(clock.drama)));
    stats.append(clockStat("Interference", measured.drawn
      ? measured.interfered + " of " + measured.drawn + " actions · " + percent(measured.rate) + (measured.target_rate !== null ? " (set " + percent(measured.target_rate) + ")" : "")
      : "No draws yet", "Saved draws: " + measured.mild + " mild, " + measured.extreme + " extreme. Fired: " + measured.fired + ". With an effect: " + measured.with_effect));
    stats.append(clockStat("Spend", money(clock.spend.spent_usd) + (Number(clock.spend.reserved_usd) ? " + " + money(clock.spend.reserved_usd) + " reserved" : ""), clock.spend.attempts + " model calls saved for this run"));
    for (const assistant of clock.assistants) {
      const bills = assistant.bills.map(bill => (bill.bill_id || "bill") + " " + readable(bill.status) + (bill.due_at ? ", due " + bill.due_at : "")).join("; ");
      stats.append(clockStat(translate(assistant.owner_name) + "'s assistant", assistant.status === "expired"
        ? "Expired" + (assistant.expired_at ? " " + assistant.expired_at : "") : "Active" + (bills ? " · " + bills : ""), bills));
    }
    sites.append(node("span", "Sites", "clock-sites-label"));
    if (!clock.sites.length) sites.append(node("span", "No sites have been built yet.", "muted"));
    for (const site of clock.sites) {
      const link = node("a", site.name);
      link.href = site.url; link.target = "_blank"; link.rel = "noopener noreferrer";
      link.title = readable(site.kind) + " · " + site.source + " · " + site.at + (site.tagline ? " · " + translate(site.tagline) : "");
      sites.append(link);
    }
  }
  const CLOCK_TABS = {week: "clock-week", board: "clock-board", queue: "clock-queue", perturbations: "clock-perturbations"};
  function renderClockTabs() {
    for (const button of $("clock-tabs").querySelectorAll("button")) {
      const selected = button.dataset.clockTab === state.clockTab;
      button.classList.toggle("is-active", selected); button.setAttribute("aria-pressed", String(selected));
    }
    for (const [tab, id] of Object.entries(CLOCK_TABS)) $(id).hidden = tab !== state.clockTab;
  }
  function trackPercent(seconds) {
    return Math.min(100, Math.max(0, seconds / state.clock.horizon_s * 100));
  }
  function place(element, start, duration = null) {
    element.style.left = trackPercent(start) + "%";
    if (duration !== null) element.style.width = (trackPercent(start + duration) - trackPercent(start)) + "%";
    return element;
  }
  function markClockPage() {
    if (!state.clock) return;
    for (const band of $("clock-week").querySelectorAll(".clock-page")) {
      place(band, state.turn * state.clock.epoch_s, state.clock.epoch_s);
    }
  }
  async function openPage(window) {
    $("live-follow").checked = false;
    if (state.turns.some(turn => turn.window === window) && window !== state.turn) await selectTurn(window);
  }
  async function openPerturbation(item) {
    if (Number.isFinite(item.fire_s)) await openPage(Math.min(Math.floor(item.fire_s / state.clock.epoch_s), state.clock.max_windows - 1));
    detail("Perturbation · " + item.outcome, item, ownerName(item.owner_id));
  }
  function perturbationMark(item) {
    const mark = node("button", null, "clock-pert " + item.outcome + (item.fired ? "" : " is-planned"));
    mark.type = "button";
    mark.title = item.outcome + " perturbation · " + (item.at || "no time") + " · " + translate(item.label || "action") + " · u " + item.u.toFixed(4) + " v " + item.v.toFixed(4);
    mark.setAttribute("aria-label", mark.title);
    place(mark, item.fire_s);
    mark.addEventListener("click", event => { event.stopPropagation(); openPerturbation(item); });
    return mark;
  }
  function clockLane(label, sub, blocks, marks, process) {
    const clock = state.clock;
    const lane = node("div", null, "clock-lane");
    const title = node("div", null, "clock-lane-label");
    title.append(node("span", label, "clock-lane-name"), node("span", sub, "clock-lane-state"));
    title.title = sub;
    const track = node("div", null, "clock-track");
    track.setAttribute("role", "group"); track.setAttribute("aria-label", label + ": " + sub);
    track.append(place(node("span", null, "clock-page"), state.turn * clock.epoch_s, clock.epoch_s));
    for (const day of clock.days.slice(1)) track.append(place(node("span", null, "clock-dayline"), day.start_s));
    for (const block of blocks) {
      const bar = node("span", null, "clock-block " + block.process + " is-" + block.status);
      bar.title = blockTitle(block);
      track.append(place(bar, block.start_s, block.duration_s));
    }
    if (process) track.append(place(node("span", null, "clock-bar"), process.bar_s));
    track.append(place(node("span", null, "clock-now"), clock.now_s));
    for (const item of marks) track.append(perturbationMark(item));
    lane.append(title, track);
    return lane;
  }
  function blockTitle(block) {
    const end = block.start_s + block.duration_s;
    const name = translate(block.label) + (block.count > 1 ? " and " + (block.count - 1) + " more" : "");
    return name + " · " + Math.max(1, Math.round((end - block.start_s) / 60)) + " min · " + readable(block.status);
  }
  function renderWeek() {
    const host = $("clock-week"); host.replaceChildren();
    const clock = state.clock;
    if (!clock) {
      host.append(node("p", state.clockError || "Loading the clock…", "empty-note" + (state.clockError ? " has-error" : "")));
      return;
    }
    if (state.clockError) host.append(node("p", state.clockError, "empty-note has-error"));
    if (!clock.started || !clock.processes.length) {
      host.append(node("p", "The clock has not started. The run script starts it. Refresh after it saves its first events.", "empty-note"));
      return;
    }
    const axis = node("div", null, "clock-lane clock-axis");
    const ruler = node("div", null, "clock-ruler");
    ruler.setAttribute("aria-hidden", "true");
    // A short final day has no room for its name inside the track.
    for (const day of clock.days) ruler.append(place(node("span", 1 - day.start_s / clock.horizon_s < .05 ? "" : day.label, "clock-day"), day.start_s));
    axis.append(node("span", "Week", "clock-lane-name"), ruler);
    const lanes = node("div", null, "clock-lanes");
    const blocksFor = (owner, kind) => clock.blocks.filter(block => block.process === kind && block.owner_id === owner);
    const ranked = [...clock.processes].sort((left, right) =>
      left.owner_id.localeCompare(right.owner_id, undefined, {numeric: true}) || left.kind.localeCompare(right.kind));
    for (const process of ranked) {
      const owner = process.owner_id;
      const action = process.current_action;
      const sub = readable(process.state) + (action ? " · " + translate(action.label) + " until " + action.ends : process.state === "waiting" && process.until ? " until " + process.until : "");
      const marks = process.kind === "actor" ? clock.perturbations.filter(item => item.owner_id === owner && Number.isFinite(item.fire_s)) : [];
      const name = process.kind === "actor" ? translate(process.owner_name) : translate(process.owner_name) + "'s assistant";
      lanes.append(clockLane(name, sub, blocksFor(owner, process.kind), marks, process));
    }
    const directorBlocks = clock.blocks.filter(block => block.process === "director");
    if (directorBlocks.length) lanes.append(clockLane("Director", counted(directorBlocks.length, "intervention"), directorBlocks, [], null));
    lanes.addEventListener("click", event => {
      const track = event.target.closest(".clock-track");
      if (!track) return;
      const box = track.getBoundingClientRect();
      if (!box.width) return;
      const seconds = (event.clientX - box.left) / box.width * clock.horizon_s;
      openPage(Math.min(Math.max(0, Math.floor(seconds / clock.epoch_s)), clock.max_windows - 1));
    });
    const legend = node("ul", null, "clock-legend");
    for (const [name, label] of [["clock-block actor", "Person"], ["clock-block agent", "Assistant"], ["clock-block director", "Director"], ["clock-pert-key mild", "Mild"], ["clock-pert-key extreme", "Extreme"], ["clock-bar-key", "Busy until"], ["clock-now-key", "Now"], ["clock-page-key", "Selected page"]]) {
      const item = node("li"); item.append(node("span", null, name), node("span", label)); legend.append(item);
    }
    host.append(axis, lanes, legend);
  }
  function renderBoard() {
    const host = $("clock-board"); host.replaceChildren();
    const refreshBoard = node("button", "Refresh", "quiet-button");
    refreshBoard.type = "button"; refreshBoard.addEventListener("click", loadBoard);
    const bar = node("div", null, "board-filters");
    bar.setAttribute("role", "group"); bar.setAttribute("aria-label", "Filter the board");
    const counts = state.board?.counts || {public: 0, direct: 0};
    for (const [filter, label] of [["all", "All " + (counts.public + counts.direct)], ["public", "Public " + counts.public], ["direct", "Direct " + counts.direct]]) {
      const button = node("button", label);
      button.type = "button"; button.dataset.boardFilter = filter;
      button.classList.toggle("is-active", state.boardFilter === filter);
      button.setAttribute("aria-pressed", String(state.boardFilter === filter));
      button.addEventListener("click", () => { state.boardFilter = filter; renderBoard(); });
      bar.append(button);
    }
    bar.append(refreshBoard);
    host.append(bar, node("p", "Research read. Assistants see only the public board and their own direct messages. Owners and the Director never see it.", "control-note"));
    if (state.boardError) host.append(node("p", state.boardError, "empty-note has-error"));
    if (!state.board) { if (!state.boardError) host.append(node("p", "Loading the board…", "empty-note")); return; }
    if (state.board.truncated) host.append(node("p", "This view stopped at its load limit. Use the board API to read later posts.", "empty-note"));
    const threads = new Map();
    for (const post of state.board.items) {
      if (state.boardFilter !== "all" && post.visibility !== state.boardFilter) continue;
      if (!threads.has(post.thread_id)) threads.set(post.thread_id, []);
      threads.get(post.thread_id).push(post);
    }
    if (!threads.size) {
      host.append(node("p", state.boardFilter === "direct" ? "No direct messages were saved in this run." : state.boardFilter === "public" ? "No public posts were saved in this run." : "No board posts were saved in this run.", "empty-note"));
      return;
    }
    const list = node("div", null, "board-list");
    for (const posts of threads.values()) {
      const first = posts[0];
      const direct = first.visibility === "direct";
      const article = node("article", null, "board-thread" + (direct ? " is-direct" : ""));
      const head = node("header", null, "board-thread-head");
      head.append(node("span", direct ? "DIRECT" : "PUBLIC", "badge " + (direct ? "board-direct" : "board-public")));
      head.append(node("h3", first.title || (direct ? "Direct thread" : "Earlier thread")));
      head.append(node("span", first.at, "board-time"));
      const people = direct ? first.participants.map(person => person.name) : [...new Set(posts.map(post => post.author_name))];
      article.append(head, node("p", (direct ? "Between: " : "Took part: ") + people.map(translate).join(", "), "board-people"));
      for (const post of posts) {
        const item = node("div", null, "board-post");
        const meta = node("div", null, "speech-meta");
        meta.append(node("span", post.author_name), node("span", post.at, "speech-time"));
        const inspect = node("button", "↗");
        inspect.type = "button"; inspect.title = "Inspect saved post"; inspect.setAttribute("aria-label", "Inspect saved post by " + translate(post.author_name));
        inspect.addEventListener("click", () => detail(direct ? "Direct message" : "Board post", post, translate(post.author_name)));
        meta.append(inspect);
        item.append(meta, node("p", post.body, "board-body"));
        article.append(item);
      }
      list.append(article);
    }
    host.append(list);
  }
  function renderQueue() {
    const host = $("clock-queue"); host.replaceChildren();
    const clock = state.clock;
    if (!clock) { host.append(node("p", state.clockError || "Loading the clock…", "empty-note")); return; }
    host.append(node("p", "The next " + clock.queue.length + " queued events in the order the clock will run them.", "control-note"));
    if (!clock.queue.length) { host.append(node("p", "Nothing is queued.", "empty-note")); return; }
    const list = node("div", null, "clock-list");
    for (const item of clock.queue) {
      const row = node("button", null, "log-row");
      row.type = "button";
      const summary = item.payload?.cue || item.payload?.description || item.payload?.reason || item.key || "";
      const description = node("span", null, "log-description");
      description.append(node("strong", readable(item.kind)), node("span", preview(summary)));
      row.append(node("span", item.at, "log-time"), node("span", "queued", "log-kind"), description, node("span", item.owner_id ? ownerName(item.owner_id) : "All", "log-owner"));
      row.addEventListener("click", () => detail("Queued · " + readable(item.kind), item, item.at));
      list.append(row);
    }
    host.append(list);
  }
  function renderPerturbations() {
    const host = $("clock-perturbations"); host.replaceChildren();
    const clock = state.clock;
    if (!clock) { host.append(node("p", state.clockError || "Loading the clock…", "empty-note")); return; }
    const measured = clock.interference;
    host.append(node("p", measured.drawn
      ? "Saved draws: " + measured.drawn + ". Drawn to interfere: " + measured.interfered + " (" + percent(measured.rate) + "). Mild: " + measured.mild + ". Extreme: " + measured.extreme + ". Fired: " + measured.fired + ". With an effect: " + measured.with_effect + "."
      : "No draws were saved yet.", "control-note"));
    if (!clock.perturbations.length) { host.append(node("p", clock.drama === "off" ? "Drama is off, so nothing will interfere." : "Nothing has interfered so far.", "empty-note")); return; }
    const list = node("div", null, "clock-list");
    for (const item of clock.perturbations) {
      const row = node("button", null, "log-row");
      row.type = "button";
      const description = node("span", null, "log-description");
      description.append(node("strong", item.label || "Action"), node("span", "u " + item.u.toFixed(4) + " · v " + item.v.toFixed(4) + " · " + readable(item.level) + (item.fired ? "" : " · not yet fired")));
      row.append(node("span", item.at || "—", "log-time"), node("span", item.outcome, "log-kind pert-" + item.outcome), description, node("span", ownerName(item.owner_id), "log-owner"));
      row.addEventListener("click", () => openPerturbation(item));
      list.append(row);
    }
    host.append(list);
  }
  function renderClock() {
    if (!clocked()) return;
    const key = state.clockError + "|" + JSON.stringify(state.clock);
    renderClockTabs();
    if (key === state.clockKey) return;
    state.clockKey = key;
    const scroller = $("clock-week").querySelector(".clock-lanes");
    const top = scroller ? scroller.scrollTop : 0;
    renderClockStats(); renderWeek(); renderQueue(); renderPerturbations();
    const lanes = $("clock-week").querySelector(".clock-lanes");
    if (lanes) lanes.scrollTop = top;
    if (state.board || state.boardError) renderBoard();
  }
  $("clock-tabs").addEventListener("click", event => {
    const button = event.target.closest("button[data-clock-tab]");
    if (!button) return;
    state.clockTab = button.dataset.clockTab;
    renderClockTabs();
    if (state.clockTab === "board") loadBoard();
  });
  $("clock-collapse").addEventListener("click", () => {
    const collapsed = $("clock-panel").classList.toggle("is-collapsed");
    $("clock-collapse").textContent = collapsed ? "Show clock" : "Hide clock";
    $("clock-collapse").setAttribute("aria-expanded", String(!collapsed));
  });
  $("clock-refresh").addEventListener("click", () => { loadClock(); if (state.clockTab === "board") loadBoard(); });
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
  $("toggle-log").addEventListener("click", () => {
    const collapsed = $("workspace").classList.toggle("logs-collapsed");
    $("toggle-log").textContent = collapsed ? "Expand log" : "Collapse log";
    $("toggle-log").setAttribute("aria-expanded", String(!collapsed));
    $("log-list").hidden = collapsed; $("channel-tabs").hidden = collapsed;
    $("log-search").closest(".log-search-wrap").hidden = collapsed;
  });
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
    const index = state.turns.findIndex(turn => turn.window === (state.pendingTurn ?? state.turn));
    if (index > 0) selectTurn(state.turns[index - 1].window);
  });
  $("next-turn").addEventListener("click", () => {
    $("live-follow").checked = false;
    const index = state.turns.findIndex(turn => turn.window === (state.pendingTurn ?? state.turn));
    if (index >= 0 && index < state.turns.length - 1) selectTurn(state.turns[index + 1].window);
  });
  function scrub(event, immediate = false) {
    $("live-follow").checked = false;
    pause(); clearTimeout(state.sliderTimer);
    const target = state.turns[Number(event.target.value)];
    if (!target) return;
    $("turn-slider").setAttribute("aria-valuetext", "Preview Turn " + (target.window + 1));
    $("turn-now").textContent = padded(target.window + 1);
    const commit = () => { if (target.window !== state.turn || state.loading) selectTurn(target.window); else renderTransport(); };
    if (immediate) commit(); else state.sliderTimer = setTimeout(commit, 100);
  }
  $("turn-slider").addEventListener("input", event => scrub(event));
  $("turn-slider").addEventListener("change", event => scrub(event, true));
  $("play").addEventListener("click", play);
  $("speed").addEventListener("change", schedule);
  $("live-follow").addEventListener("change", () => { if ($("live-follow").checked) { pause(); refresh(); } });
  $("close-detail").addEventListener("click", closeDetail);
  $("detail-dialog").addEventListener("cancel", event => { event.preventDefault(); closeDetail(); });
  $("detail-language").addEventListener("click", () => { state.detailOriginal = !state.detailOriginal; renderDetail(); });
  $("language-toggle").addEventListener("click", () => setLanguage(state.language === "en" ? "original" : "en"));
  // Animate disclosure height without replacing native keyboard semantics.
  document.addEventListener("click", event => {
    const summary = event.target.closest("summary");
    if (!summary || reducedMotion()) return;
    const box = summary.parentElement;
    event.preventDefault();
    const start = box.getBoundingClientRect().height;
    const opening = !(box.dataset.expanded ? box.dataset.expanded === "true" : box.open);
    box.getAnimations().forEach(animation => animation.cancel());
    box.dataset.expanded = String(opening);
    box.open = true;
    const end = opening ? box.getBoundingClientRect().height : summary.getBoundingClientRect().height;
    box.style.overflow = "hidden";
    const animation = box.animate([{height: start + "px"}, {height: end + "px"}], {duration: 180, easing: "cubic-bezier(.2,.7,.2,1)"});
    animation.onfinish = () => { box.open = opening; delete box.dataset.expanded; box.style.overflow = ""; };
    animation.oncancel = () => { box.style.overflow = ""; };
  });
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
      if (clocked() && !["completed", "incomplete", "invalid"].includes(state.clock?.status)) loadClock();
    }
  }, 5000);
  connect();
})();
